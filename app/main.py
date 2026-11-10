"""
Billard-System: Kamera -> Kugelerkennung -> 8-Ball-Regeln -> Beamer (HDMI) + Spiel-/Admin-Panel.

    python3 main.py                  # Beamer im Vollbild, Panels auf Port 8080
    python3 main.py --windowed       # zum Testen im Fenster
    python3 main.py --no-display     # nur Panels, keine HDMI-Ausgabe

Threads:
  Kamera     liest die Kamera (Pi-Kameramodul, USB oder Demo; cameras.py), nur das neueste Bild
  Erkennung  verarbeitet jedes neue Bild (pipeline.py)
  Queue      fragt die Lage des Queues ab, falls einer gekoppelt ist (cue_sensor.py)
  Web        Spiel-Panel (/) und Admin-Panel (/admin) auf Port 8080 (web.py)
  Video      baut am Ende einer aufgezeichneten Runde das Video (replay.py)
  Haupt      Anzeige mit pygame (display.py)
"""

import argparse
import os
import secrets
import socket
import sys
import threading
import time

import cv2

import config
import web
from ballnum import ball_number
from coral_detector import CoralDetector, CoralEngine
from cue_sensor import AlignChecker, CueSensor, clean_castcode
from geometry import Table, TABLE_SIZES
from notices import Notices
from pipeline import Pipeline
from planner import plan_shot
from qr import MAX_LEN as QR_MAX_LEN, qr_matrix, qr_png
from recorder import Recorder
from replay import RoundRecorder
from review_rounds import RoundManager
from sound import Sound
from scene import SceneRenderer, bar_balls, surface_to_bgr
from cameras import SOURCE_NAMES, SOURCES, make_reader

KINDS = ("cue", "eight", "solid", "stripe")
COLORS = ("gelb", "blau", "rot", "violett", "orange", "gruen", "weinrot", "schwarz", "")

KIND_BGR = {"cue": (255, 255, 255), "eight": (130, 130, 130), "solid": (0, 190, 255), "stripe": (255, 200, 0)}


def host_ip():
    """IP des Pi im Heimnetz (fuer den QR-Code zum Spiel-Panel)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


class App:
    def __init__(self, cue_port=5005, coral_engine=None):
        self.cfg = config.load()
        self._migrate_config()
        self.reader = make_reader(self.cfg["camera"], config.DATA_DIR).start()
        self.recorder = Recorder(os.path.join(config.DATA_DIR, "dataset"))
        self.coral = coral_engine or CoralEngine()          # Kugelerkennung nur ueber den KI-Chip
        table = self._table_for(self.reader)
        self.pipe = Pipeline(table, lambda r: CoralDetector(r, self.coral), recorder=self.recorder)
        self.pipe.auto_capture = bool(self.cfg.get("auto_capture"))
        self.pipe.on_capture = self._captured
        self.cue = CueSensor(self.cfg.get("cue_castcode", ""), self.cfg.get("cue_offset", 0.0),
                             self.cfg.get("cue_flip", False), discovery_port=cue_port,
                             address=self.cfg.get("cue_address"), on_found=self._cue_found,
                             enabled=self.cfg.get("cue_enabled", True)).start()
        self.align = AlignChecker(self.cfg.get("cue_tolerance", 2.0))
        self.rounds = RoundRecorder(os.path.join(config.DATA_DIR, "recordings"))
        self.sound = Sound()                                  # Ding ueber HDMI
        self.reviews = RoundManager(self.recorder, config.DATA_DIR, self.sound)   # Pruefrunden
        self.auth = None                                      # setzt web.start()
        self.panel_url = ""                                   # Adresse des Spiel-Panels (QR-Code)
        self.notes = Notices()                                # Meldungen oben rechts
        self._edges = {}
        self._disk = (0.0, True)
        self.proc_fps = 0.0
        # Grafik-Zustand
        self.scene_lock = threading.RLock()
        self.scene_version = 0
        self.suggestion = None
        self.seen_numbers = set()
        self._ref_id = -1
        self._web_renderer = SceneRenderer()
        self._graphic = (None, None)        # (Schluessel, JPEG)
        self._beamer = {"t": 0.0, "jpg": None, "size": None, "cam": {}, "renderer": None}
        self._beamer_lock = threading.Lock()
        self._snapshots = {}
        self._snap_id = 0
        self._stop = threading.Event()
        threading.Thread(target=self._process_loop, name="erkennung", daemon=True).start()

    # ---------------------------------------------------------------- Kamera
    def _migrate_config(self):
        """Fehlende Werte ergaenzen. Die ESP32-CAM gibt es nicht mehr -> Pi-Kameramodul."""
        cam = self.cfg.get("camera") or {}
        if cam.get("source") not in SOURCES:
            cam["source"] = "picam"
        cam.pop("url", None)
        cam.setdefault("device", "/dev/video0")
        cam.setdefault("demo_url", os.environ.get("DEMO_IMAGE_URL", ""))
        self.cfg["camera"] = cam
        self.cfg.pop("corners", None)
        by = self.cfg.get("corners_by_source") or {}
        by.pop("esp32", None)
        self.cfg["corners_by_source"] = by
        qr = dict(config.DEFAULTS["qr"])
        qr.update(self.cfg.get("qr") or {})
        self.cfg["qr"] = qr

    def _table_for(self, reader):
        """Tisch-Ecken der aktiven Kameraquelle (die Demo bringt ihre eigenen mit)."""
        corners = self.cfg["corners_by_source"].get(reader.calib_key) or reader.default_corners
        return Table(corners, TABLE_SIZES.get(self.cfg.get("table_size"), TABLE_SIZES["8ft"]))

    def _set_camera(self, p):
        cam = dict(self.cfg["camera"])
        src = p.get("source", cam["source"])
        if src not in SOURCES:
            raise ValueError("Kameraquelle")
        cam["source"] = src
        if "device" in p:
            dev = str(p["device"]).strip() or "/dev/video0"
            if not (dev.isdigit() or dev.startswith("/dev/video")):
                raise config.UserError("Gerät wie /dev/video0 angeben")
            cam["device"] = dev
        if "demo_url" in p:
            du = str(p["demo_url"]).strip()
            if du and not du.startswith(("http://", "https://")):
                raise config.UserError("Bildadresse muss mit http:// oder https:// beginnen")
            cam["demo_url"] = du
        new = make_reader(cam, config.DATA_DIR)    # Demo laedt hier ggf. das Bild (bis 15 s)
        old, self.reader = self.reader, new.start()
        old.stop()
        self.cfg["camera"] = cam
        config.save(self.cfg)
        self.pipe.set_table(self._table_for(new))
        with self.scene_lock:
            self.seen_numbers = set()
        msg = f"Kamera: {SOURCE_NAMES[src]}"
        if getattr(new, "info", ""):
            msg += f" - {new.info}"
        if not self.pipe.table.calibrated:
            msg += ". Jetzt unter „Tisch einrichten“ die Ecken antippen."
        return msg

    def camera_state(self):
        r = self.reader
        return dict(self.cfg["camera"], name=SOURCE_NAMES.get(self.cfg["camera"]["source"]),
                    info=getattr(r, "info", ""), connected=bool(r.connected), error=r.last_error)

    # ------------------------------------------------------------ Erkennung
    def _process_loop(self):
        last_id = -1
        n, t0 = 0, time.monotonic()
        while not self._stop.is_set():
            frame, fid, age = self.reader.latest()
            if frame is None or fid == last_id or age > 2.0:
                time.sleep(0.01)
                continue
            last_id = fid
            try:
                ev = self.pipe.process(frame)
                if isinstance(ev, tuple):
                    print(f"[spiel] {ev[1]}", flush=True)
                if self.pipe.monitor.ref_id != self._ref_id:
                    self.refresh_scene()
                if isinstance(ev, tuple):
                    self._record_step()
            except Exception as e:  # ein kaputtes Bild darf das System nicht stoppen
                print(f"[erkennung] Fehler: {type(e).__name__}: {e}", flush=True)
            n += 1
            if time.monotonic() - t0 >= 2.0:
                self.proc_fps = n / (time.monotonic() - t0)
                n, t0 = 0, time.monotonic()

    # ---------------------------------------------------------------- Grafik
    def refresh_scene(self):
        """Nach jedem Stoss / neuen ruhigen Tisch / Admin-Befehl: Kugeln, Game-Bar und
        Stossvorschlag neu bestimmen."""
        with self.pipe.lock:
            self._ref_id = self.pipe.monitor.ref_id
            balls = self.pipe.current_balls()
            game = self.pipe.game.to_dict()
            radius = self.pipe.table.ball_radius_px
            calibrated = self.pipe.table.calibrated
        nums = {ball_number(b.kind, b.color) for b in balls} - {None, 0}
        with self.scene_lock:
            self.seen_numbers |= nums
            if self.cfg.get("assist") and calibrated:
                self.suggestion = plan_shot(balls, radius, game)
            else:
                self.suggestion = None
            self.scene_version += 1

    def scene(self, live=True):
        """Alles, was die Grafik braucht (Beamer, iPad, Aufzeichnung)."""
        with self.pipe.lock:
            balls = self.pipe.current_balls()
            game = self.pipe.game.to_dict()
            radius = self.pipe.table.ball_radius_px
            status = self.pipe.status_text()
        if self.pipe.detector is None or self.pipe.not_ready:
            status = ""                 # steht als Meldung oben rechts
        with self.scene_lock:
            sug = self.suggestion
            seen = set(self.seen_numbers)
            cue = None
            if live and self.cfg.get("assist") and self.cue.enabled and self.cue.castcode:
                angle = self.cue.table_angle()
                target = sug.get("angle") if sug else None
                ok = self.align.update(angle, target)
                cue = {"angle": angle, "ok": ok, "err": self.align.err, "connected": self.cue.connected}
        sb = []
        for b in balls:
            sb.append({"x": float(b.x), "y": float(b.y), "kind": b.kind, "color": b.color,
                       "number": ball_number(b.kind, b.color)})
        visible = {d["number"] for d in sb} - {None, 0}
        info = f"Kamera {self.reader.fps:4.1f} fps" if self.reader.connected else ""
        return {"balls": sb, "radius": radius, "cloth": self.cfg.get("cloth", "blau"),
                "game": game, "bar": bar_balls(game, visible, seen),
                "suggestion": sug, "cue": cue, "status": status, "info": info, "version": self.scene_version,
                "review": self.reviews.status() if live else None,
                "qr": self.qr_info() if live else None,
                "notices": self.notices() if live else []}

    # ------------------------------------------------------- QR-Code, Meldungen
    def qr_text(self):
        q = self.cfg.get("qr") or {}
        return (q.get("text") or "").strip() or self.panel_url

    def qr_info(self):
        """QR-Code fuer die Beamer-Ecke (unten rechts) oder None."""
        q = self.cfg.get("qr") or {}
        text = self.qr_text()
        if not q.get("enabled", True) or not text:
            return None
        m = qr_matrix(text)
        return None if m is None else {"text": text, "matrix": m, "label": q.get("label", "")}

    def _edge(self, key, ok, title, body=""):
        """Kurze Erfolgsmeldung, wenn etwas, das vorher nicht ging, wieder geht."""
        was = self._edges.get(key)
        self._edges[key] = ok
        if ok and was is False:
            self.notes.push(title, body, "success", ttl=5.0, key=key)

    def notices(self):
        """Meldungen oben rechts (Beamer und Panels): was gerade nicht stimmt + Ereignisse."""
        probs = []
        r = self.reader
        cam_ok = bool(r.connected)
        if not cam_ok:
            probs.append(("kamera", "error", "Keine Kamerabilder",
                          r.last_error or "Warte auf die Kamera ...", 3.0))
        self._edge("kamera", cam_ok, "Kamera verbunden", SOURCE_NAMES.get(self.cfg["camera"]["source"], ""))
        with self.pipe.lock:
            calibrated = self.pipe.detector is not None
            not_ready = self.pipe.not_ready
        if cam_ok and not calibrated:
            probs.append(("tisch", "warning", "Tisch nicht eingerichtet",
                          "Im Admin-Panel unter „Tisch einrichten“ die vier Ecken antippen.", 1.0))
        if calibrated:
            if not_ready:
                probs.append(("ki", "error", "KI-Chip nicht bereit", not_ready, 2.0))
            self._edge("ki", not not_ready, "KI-Chip bereit", "Kugelerkennung läuft")
        c = self.cue
        if self.cfg.get("assist") and c.enabled and c.castcode:
            if not c.connected:
                why = "Castcode passt nicht zum Queue." if c.last_error.startswith("Castcode") else \
                    "Der Pi sucht ihn im WLAN – ist er eingeschaltet?"
                probs.append(("queue", "warning", "Queue nicht verbunden", why, 5.0))
            self._edge("queue", c.connected, "Queue verbunden", f"Castcode {c.castcode}")
        t, ok = self._disk
        if time.monotonic() - t > 30:
            ok = self.recorder.disk_ok()
            self._disk = (time.monotonic(), ok)
        if not ok:
            probs.append(("speicher", "warning", "Speicher fast voll",
                          "Es werden keine Trainingsbilder mehr aufgenommen.", 0.0))
        if self.rounds.status().get("error"):
            probs.append(("video", "warning", "Spielaufzeichnung", self.rounds.status()["error"], 0.0))
        return self.notes.current(probs)

    def graphic_jpeg(self, size=(1280, 720)):
        """Grafische Ansicht fuer das iPad. Nur neu zeichnen, wenn sich etwas geaendert hat."""
        sc = self.scene()
        cue = sc["cue"] or {}
        key = (sc["version"], None if cue.get("angle") is None else round(cue["angle"], 1), cue.get("ok"),
               sc["status"], size)
        if self._graphic[0] == key:
            return self._graphic[1]
        img = surface_to_bgr(self._web_renderer.render_view(sc, size))
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            self._graphic = (key, jpg.tobytes())
        return self._graphic[1]

    def beamer_jpeg(self, size=(1280, 720), max_age=0.15):
        """Das komplette Beamerbild (Kamera + Grafik + Game-Bar) fuer den Browser.
        Mehrere Zuschauer teilen sich ein Bild; neu gezeichnet hoechstens alle max_age s."""
        import pygame
        from display import draw_beamer
        from scene import RENDER_LOCK
        with self._beamer_lock:
            b = self._beamer
            now = time.monotonic()
            if b["jpg"] is not None and b["size"] == size and now - b["t"] < max_age:
                return b["jpg"]
            if b["renderer"] is None:
                b["renderer"] = SceneRenderer()
            frame, fid, _ = self.reader.latest()
            sc = self.scene()
            with RENDER_LOCK:
                surf = pygame.Surface(size)
                draw_beamer(surf, b["renderer"], b["cam"], frame, fid, self.pipe, self.reader, sc)
                img = surface_to_bgr(surf)
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                b.update(t=now, jpg=jpg.tobytes(), size=size)
            return b["jpg"]

    def _record_step(self):
        if not self.rounds.active:
            return
        sc = self.scene(live=False)
        self.rounds.add(sc)
        if sc["game"]["phase"] != "playing":
            self.rounds.stop()             # Rack vorbei -> Video bauen

    # ------------------------------------------------- Korrekturen fuers KI-Training
    def snapshot(self):
        """Aktuelles Draufsicht-Bild + erkannte Kugeln einfrieren, damit man sie im
        Portal in Ruhe korrigieren kann."""
        with self.pipe.lock:
            warped = self.pipe.last_warped
            balls = self.pipe.current_balls()
            r = self.pipe.table.ball_radius_px
        if warped is None:
            return None
        self._snap_id += 1
        sid = self._snap_id
        self._snapshots[sid] = (warped.copy(), r)
        for k in sorted(self._snapshots)[:-3]:
            del self._snapshots[k]
        h, w = warped.shape[:2]
        return {"id": sid, "width": w, "height": h, "radius": r,
                "balls": [{"x": round(float(b.x), 1), "y": round(float(b.y), 1), "kind": b.kind,
                           "color": b.color or "", "number": ball_number(b.kind, b.color)} for b in balls]}

    def snapshot_jpeg(self, sid):
        snap = self._snapshots.get(sid)
        if snap is None:
            return None
        ok, jpg = cv2.imencode(".jpg", snap[0], [cv2.IMWRITE_JPEG_QUALITY, 85])
        return jpg.tobytes() if ok else None

    def _train_correction(self, p):
        from ballnum import Ball
        snap = self._snapshots.get(int(p["id"]))
        if snap is None:
            raise config.UserError("Bild ist veraltet, bitte neu laden")
        warped, r = snap
        balls, numbers, corrections = [], [], []
        for d in p["balls"]:
            kind = d["kind"]
            color = d.get("color", "") or ""
            if kind not in KINDS + ("none",) or color not in COLORS:
                raise ValueError("Kugeltyp/Farbe")
            x, y = float(d["x"]), float(d["y"])
            if d.get("changed"):
                corrections.append({"x": x, "y": y, "kind": kind, "color": color})
            if kind == "none":
                continue
            balls.append(Ball(x, y, r, kind, color))
            numbers.append(ball_number(kind, color))
        if None in numbers:
            raise config.UserError(f"{numbers.count(None)} Kugel(n) ohne Nummer - "
                                   "Kugel antippen und Nummer wählen")
        self.recorder.save_verified(warped, balls, r, numbers)
        applied = 0
        if p.get("apply") and corrections:
            with self.pipe.lock:
                applied = self.pipe.correct_balls(corrections)
        return (f"Gespeichert ({self.recorder.verified} geprüfte Bilder)"
                + (f", {applied} Kugel(n) im Spiel übernommen" if applied else ""))

    def _captured(self, name, source):
        """Trainingsbild gespeichert: kurz oben rechts melden (auch auf dem Beamer)."""
        n = self.recorder.pending
        self.notes.push("Bild automatisch gespeichert" if source == "auto" else "Bild gespeichert",
                        f"{n} " + ("Bild wartet" if n == 1 else "Bilder warten") + " aufs Festlegen",
                        "success", ttl=3.0, key="capture")

    def review_as(self, p, by):
        """Gespeichertes Bild festgelegt - von einem Pruefer (/pruefen) oder dem Admin."""
        with self.pipe.lock:
            return self._review_save(p, by)

    def _review_save(self, p, by="Admin"):
        """Ein gesammeltes Trainingsbild wurde auf dem iPad geprueft."""
        balls = []
        for d in p.get("balls", []):
            kind, color = d["kind"], d.get("color", "") or ""
            if kind not in KINDS + ("none",) or color not in COLORS:
                raise ValueError("Kugeltyp/Farbe")
            balls.append({"x": float(d["x"]), "y": float(d["y"]), "kind": kind, "color": color,
                          "number": ball_number(kind, color), "changed": bool(d.get("changed"))})
        try:
            res = self.recorder.review(str(p["name"]), balls, bool(p.get("discard")), by=by)
        except (FileNotFoundError, ValueError) as e:
            raise config.UserError(str(e))
        return f"Bild {res}. Noch {self.recorder.pending} zu prüfen, {self.recorder.verified} geprüft."

    def save_model(self, data: bytes):
        """Hochgeladenes Coral-Modell speichern. Der Detektor laedt es von selbst (Aenderungszeit)."""
        if len(data) < 1000 or data[4:8] != b"TFL3":
            raise config.UserError("Das ist keine TFLite-Modelldatei (.tflite)")
        if b"edgetpu-custom-op" not in data:
            raise config.UserError("Modell ist nicht für den Coral kompiliert "
                                   "(die Datei *_edgetpu.tflite aus dem Training nehmen)")
        folder = os.path.join(config.DATA_DIR, "models")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "billard_edgetpu.tflite")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return f"Modell gespeichert ({len(data) / 1e6:.1f} MB). Der Coral lädt es in wenigen Sekunden."

    # ------------------------------------------------------------- Portal-API
    def _cue_found(self, address):
        """Queue im WLAN gefunden: Adresse merken (beim naechsten Start sofort da)."""
        self.cfg["cue_address"] = list(address)
        config.save(self.cfg)

    # ------------------------------------------------------------- Pruefer
    def reviewer_list(self):
        return list(self.cfg.get("reviewers") or [])

    def _new_code(self):
        used = {str(r["code"]) for r in self.reviewer_list()} | {os.environ.get("ADMIN_PIN", ""),
                                                                 os.environ.get("GAME_PIN", "")}
        while True:
            code = f"{secrets.randbelow(9000) + 1000}"
            if code not in used:
                return code

    def _reviewer_cmd(self, name, p):
        revs = self.reviewer_list()
        who = str(p.get("name", "")).strip()[:20]
        if name == "reviewer_add":
            if not who:
                raise config.UserError("Name fehlt")
            if any(r["name"].lower() == who.lower() for r in revs):
                raise config.UserError(f"{who} gibt es schon")
            code = self._new_code()
            revs.append({"name": who, "code": code})
            msg = f"{who} angelegt - Code {code}"
        elif name in ("reviewer_remove", "reviewer_newcode"):
            r = next((r for r in revs if r["name"] == who), None)
            if r is None:
                raise config.UserError("Prüfer nicht gefunden")
            if self.auth is not None:
                self.auth.revoke(who)                         # alte Anmeldung gilt nicht mehr
            if name == "reviewer_remove":
                revs.remove(r)
                msg = f"{who} entfernt"
            else:
                r["code"] = self._new_code()
                msg = f"Neuer Code für {who}: {r['code']}"
        self.cfg["reviewers"] = revs
        config.save(self.cfg)
        return msg

    def _start_review(self):
        names = [r["name"] for r in self.reviewer_list()]
        participants = self.reviews.online(names)
        with self.pipe.lock:
            warped = self.pipe.last_warped
            balls = list(self.pipe.balls)
            radius = self.pipe.table.ball_radius_px
        if warped is None:
            raise config.UserError("Kein Kamerabild (Kamera und Tisch eingerichtet?)")
        st = self.coral.status() if balls else {}
        model = f"{st['model']} vom {st['model_time']}" if st.get("model") else None
        r = self.reviews.start(warped, balls, radius, model, participants,
                               show_ai=bool(self.cfg.get("review_show_ai", False)))
        ki = (" - KI-Vorschlag liegt vor" + ("" if r["show_ai"] else " (Prüfer sehen ihn nicht)")) if model else ""
        return f"Bild {r['no']} an {len(participants)} Prüfer geschickt: {', '.join(participants)}{ki}"

    def state_dict(self):
        online = set(self.reviews.online([r["name"] for r in self.reviewer_list()]))
        with self.pipe.lock:
            g = self.pipe.game.to_dict()
            status = self.pipe.status_text()
            counts = self.pipe.stable_counts()
            table = self.pipe.table
        return {
            "game": g,
            "status": status,
            "camera_fps": round(self.reader.fps, 1),
            "processing_fps": round(self.proc_fps, 1),
            "counts": counts,
            "calibrated": table.calibrated,
            "corners": table.corners,
            "camera": self.camera_state(),
            "table_size": self.cfg.get("table_size"),
            "cloth": self.cfg.get("cloth", "blau"),
            "training": {"pending": self.recorder.pending, "auto_capture": self.pipe.auto_capture,
                         "verified": self.recorder.verified, "discarded": self.recorder.discarded,
                         "disk_ok": self.recorder.disk_ok(), "quality": self.recorder.model_quality()},
            "detector": {"coral": self.coral.status(), "not_ready": self.pipe.not_ready},
            "assist": bool(self.cfg.get("assist")),
            "suggestion": (self.suggestion or {}).get("text"),
            "scene_version": self.scene_version,
            "cue": dict(self.cue.status(), tolerance=self.align.tolerance,
                        ok=self.align.ok, err=None if self.align.err is None else round(self.align.err, 1)),
            "qr": dict(self.cfg.get("qr") or {}, url=self.qr_text(), panel_url=self.panel_url),
            "notices": self.notices(),
            "round": self.rounds.status(),
            "videos": self.rounds.videos(),
            "reviewers": [dict(r, online=r["name"] in online) for r in self.reviewer_list()],
            "review": self.reviews.status(),
            "review_show_ai": bool(self.cfg.get("review_show_ai", False)),
            "sound": {"device": self.sound.device, "error": self.sound.error},
        }

    def game_state(self):
        """Was das Spiel-Panel braucht - ohne Pruefer-Codes und Admin-Einstellungen."""
        with self.pipe.lock:
            g = self.pipe.game.to_dict()
            status = self.pipe.status_text()
            if self.pipe.detector is None or self.pipe.not_ready:
                status = ""             # steht als Meldung oben rechts
        c = self.cue.status()
        return {
            "game": g,
            "status": status,
            "camera_fps": round(self.reader.fps, 1),
            "assist": bool(self.cfg.get("assist")),
            "suggestion": (self.suggestion or {}).get("text"),
            "scene_version": self.scene_version,
            "cue": {"paired": bool(c["castcode"]), "enabled": c["enabled"], "connected": c["connected"],
                    "state": c["state"], "angle": c["angle"], "battery": c["battery"],
                    "ok": self.align.ok, "err": None if self.align.err is None else round(self.align.err, 1)},
            "round": self.rounds.status(),
            "videos": self.rounds.videos(),
            "training": {"pending": self.recorder.pending, "auto_capture": self.pipe.auto_capture},
            "notices": self.notices(),
        }

    def qr_preview(self):
        return qr_png(self.qr_text())

    def jpeg(self, kind):
        frame, _, _ = self.reader.latest()
        if frame is None:
            return None
        if kind == "warped":
            with self.pipe.lock:
                warped = self.pipe.last_warped
                balls = list(self.pipe.balls)
            if warped is None:
                img = frame.copy()
            else:
                img = warped.copy()
                for b in balls:
                    cv2.circle(img, (int(b.x), int(b.y)), int(b.r + 3), KIND_BGR.get(b.kind, (255, 255, 255)), 2)
        else:
            img = frame
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return jpg.tobytes() if ok else None

    def command(self, name, p):
        result = self._command(name, p)
        if result is not None:
            if name in ("start", "new_rack"):
                with self.scene_lock:
                    self.seen_numbers = set()
            self.refresh_scene()
            if self.rounds.active and name in ("start", "new_rack", "foul", "undo", "undo_shot",
                                               "winner", "abort", "switch_turn"):
                sc = self.scene(live=False)
                if name in ("winner", "abort") or sc["game"]["phase"] != "playing":
                    self.rounds.stop(sc)
                else:
                    self.rounds.add(sc)
        return result

    def _command(self, name, p):
        g = self.pipe.game
        if name == "camera":
            return self._set_camera(p)
        if name in ("reviewer_add", "reviewer_remove", "reviewer_newcode"):
            return self._reviewer_cmd(name, p)
        try:
            if name == "round_start":
                return self._start_review()
            if name == "round_finish":
                return self.reviews.finish()["text"]
            if name == "round_cancel":
                self.reviews.cancel()
                return "Prüfrunde abgebrochen"
        except ValueError as e:
            raise config.UserError(str(e))
        if name == "review_show_ai":
            self.cfg["review_show_ai"] = bool(p.get("enabled"))
            config.save(self.cfg)
            return ("Prüfer sehen den KI-Vorschlag" if self.cfg["review_show_ai"]
                    else "Prüfer legen fest, ohne den KI-Vorschlag zu sehen")
        if name == "sound_test":
            err = self.sound.ding(wait=True)
            if err:
                raise config.UserError(err)
            return f"Ding gespielt über {self.sound.device}"
        if name == "train_correction":
            return self._train_correction(p)
        if name == "record_round":
            if p.get("enabled"):
                if not self.rounds.start(self.scene(live=False)):
                    return "Aufzeichnung läuft bereits"
                return "Aufzeichnung läuft. Endet automatisch, wenn das Rack vorbei ist."
            if not self.rounds.stop(self.scene(live=False)):
                return "Es läuft keine Aufzeichnung"
            return "Aufzeichnung beendet, Video wird erstellt ..."
        if name == "assist":
            self.cfg["assist"] = bool(p.get("enabled"))
            config.save(self.cfg)
            return "Schlagverbesserung an" if self.cfg["assist"] else "Schlagverbesserung aus"
        if name == "cue_use":
            on = bool(p.get("enabled"))
            self.cfg["cue_enabled"] = on
            config.save(self.cfg)
            self.cue.set_enabled(on)
            if on and not self.cue.castcode:
                return "Queue an - im Admin-Panel muss noch ein Castcode eingetragen werden"
            return "Queue an - der Pi sucht ihn im WLAN" if on else "Queue aus"
        if name == "qr_settings":
            q = dict(self.cfg.get("qr") or {})
            if "enabled" in p:
                q["enabled"] = bool(p["enabled"])
            if "text" in p:
                text = str(p["text"]).strip()
                if len(text) > QR_MAX_LEN:
                    raise config.UserError(f"QR-Code: höchstens {QR_MAX_LEN} Zeichen")
                if text and qr_matrix(text) is None:
                    raise config.UserError("Daraus lässt sich kein QR-Code machen")
                q["text"] = text
            if "label" in p:
                q["label"] = str(p["label"]).strip()[:24]
            self.cfg["qr"] = q
            config.save(self.cfg)
            return ("QR-Code gespeichert: " + self.qr_text()) if q.get("enabled") else "QR-Code ausgeblendet"
        if name == "cue_pair":
            code = clean_castcode(p.get("castcode"))
            self.cfg.update(cue_castcode=code, cue_address=None)
            config.save(self.cfg)
            self.cue.pair(code)
            return f"Castcode {code} gespeichert - der Pi sucht jetzt den Queue im WLAN"
        if name == "cue_unpair":
            self.cfg.update(cue_castcode="", cue_address=None)
            config.save(self.cfg)
            self.cue.pair("")
            return "Queue entkoppelt"
        if name == "cue_calibrate":
            self.cfg["cue_offset"] = self.cue.calibrate()
            config.save(self.cfg)
            return "Queue kalibriert: Lage an der Bande ist jetzt 0°"
        if name == "cue_settings":
            if "flip" in p:
                self.cue.flip = self.cfg["cue_flip"] = bool(p["flip"])
            if "tolerance" in p:
                tol = float(p["tolerance"])
                if not 0.3 <= tol <= 15:
                    raise config.UserError("Toleranz: 0,3 bis 15 Grad")
                self.align.tolerance = self.cfg["cue_tolerance"] = tol
            config.save(self.cfg)
            return "Queue-Einstellungen gespeichert"
        with self.pipe.lock:
            if name == "start":
                g.start_match(str(p.get("team_a", "")), str(p.get("team_b", "")), int(p.get("breaker", 0)) % 2)
                self.pipe.restart_tracking()
            elif name == "new_rack":
                g.new_rack(p.get("breaker"))
                self.pipe.restart_tracking()
            elif name == "abort":
                g.abort()
            elif name == "foul":
                g.foul()
            elif name == "switch_turn":
                g.switch_turn()
            elif name == "undo":
                g.undo()
            elif name == "undo_shot":
                g.undo_shot()
            elif name == "set_group":
                if p["group"] not in ("solid", "stripe"):
                    raise ValueError("Gruppe")
                g.set_group(int(p["team"]) % 2, p["group"])
            elif name == "adjust":
                if p["group"] not in ("solid", "stripe"):
                    raise ValueError("Gruppe")
                g.adjust(p["group"], int(p["delta"]))
            elif name == "winner":
                g.declare_winner(int(p["team"]) % 2)
            elif name == "calibrate":
                corners = p.get("corners")
                size = p.get("table_size", "8ft")
                if size not in TABLE_SIZES:
                    raise ValueError("Tischgröße")
                if not corners or len(corners) != 4:
                    raise ValueError("Es werden genau 4 Ecken gebraucht")
                corners = [[float(x), float(y)] for x, y in corners]
                cloth = p.get("cloth", self.cfg.get("cloth", "blau"))
                if cloth not in ("blau", "gruen"):
                    raise ValueError("Tuchfarbe")
                table = Table(corners, TABLE_SIZES[size])
                self.cfg["corners_by_source"][self.reader.calib_key] = table.corners
                self.cfg.update(table_size=size, cloth=cloth)
                config.save(self.cfg)
                self.pipe.set_table(table)
                return "Tisch gespeichert. Die Erkennung startet neu."
            elif name == "delete_images":
                if p.get("confirm") is not True:
                    raise config.UserError("Löschen nicht bestätigt")
                n = self.recorder.delete_all()
                return f"{n} Bilder gelöscht. Das KI-Modell bleibt erhalten."
            elif name == "capture":
                if self.pipe.capture_now() is None:
                    raise config.UserError("Kein Kamerabild (Tisch eingerichtet?) oder Speicher voll")
                return f"Bild gespeichert. {self.recorder.pending} warten aufs Festlegen."
            elif name == "auto_capture":
                on = bool(p.get("enabled"))
                self.cfg["auto_capture"] = self.pipe.auto_capture = on
                config.save(self.cfg)
                return ("Ab jetzt wird nach jedem Stoß ein Bild gespeichert" if on
                        else "Bilder werden nur noch per Knopf gespeichert")
            elif name == "review_save":
                return self._review_save(p)
            else:
                return None
            return g.message

    def stop(self):
        self._stop.set()
        self.reader.stop()
        self.cue.stop()


def main():
    ap = argparse.ArgumentParser(description="Billard-Scoreboard")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    ap.add_argument("--cue-port", type=int, default=int(os.environ.get("CUE_PORT", "5005")))
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--no-display", action="store_true")
    args = ap.parse_args()

    app = App(args.cue_port)
    web.start(app, args.port)
    ip = host_ip()
    base = f"http://{ip}:{args.port}" if ip else ""
    app.panel_url = base + "/" if base else ""
    cam = app.cfg["camera"]
    print(f"[start] Kamera {SOURCE_NAMES[cam['source']]} ({app.reader.info or '-'}), "
          f"Spiel-Panel {app.panel_url or '(IP unbekannt)'}  Admin-Panel {base + '/admin' if base else '-'}",
          flush=True)

    if args.no_display:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        app.stop()
        return 0

    from display import Display   # pygame nur laden, wenn wirklich angezeigt wird
    disp = Display(args.windowed)
    disp.open()
    import pygame
    clock = pygame.time.Clock()
    try:
        while disp.handle_events():
            frame, fid, _ = app.reader.latest()
            disp.draw(frame, fid, app.pipe, app.reader, app.scene())
            clock.tick(20)
    finally:
        app.stop()
        disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
