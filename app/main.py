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
  Upload     schickt Trainingsbilder zum VPS und holt neue Modelle von dort (uploader.py)
  Haupt      Anzeige mit pygame (display.py)

KI-Training (Kugeln festlegen, Farben lernen, Modelle trainieren) laeuft NICHT auf dem Pi, sondern auf
der Trainingsseite im Netz (training_server/) bzw. in Google Colab. Der Pi nimmt nur Bilder auf und
erkennt die Kugeln mit den Modellen, die er vom VPS bekommt (oder die im Admin-Panel hochgeladen werden).
"""

import argparse
import json
import os
import socket
import sys
import threading
import time

import cv2

# OpenCV startet sonst pro Kern einen Hilfsthread. Bei den kleinen Bildern hier kostet das
# auf dem Pi mehr (Kontextwechsel, Waerme) als es bringt.
cv2.setNumThreads(1)

import config
import web
from ballnum import ball_number
import kugel_merkmale
from coral_detector import (DATA_FEATURES, DATA_KLASSEN, START_FEATURES, CoralDetector, CoralEngine,
                            default_features, klassen_engine)
from cue_sensor import AlignChecker, CueSensor, clean_castcode
from cue_update import CueUpdater
from geometry import Table, TABLE_SIZES
from notices import Notices
from pipeline import Pipeline
from planner import plan_shot
from qr import MAX_LEN as QR_MAX_LEN, qr_matrix, qr_png
from recorder import Recorder
from replay import RoundRecorder
from scene import SceneRenderer, bar_balls, surface_to_bgr
from cameras import SOURCE_NAMES, SOURCES, make_reader
from single_ball import SingleBall

# Bildraten (weniger CPU -> der Pi bleibt kuehler und drosselt nicht)
DISPLAY_FPS = 10          # HDMI-Bild (Beamer); reicht fuer Scoreboard und Kamerabild
# Kamera und Erkennung: hoechstens cameras.KAMERA_FPS (3) Bilder pro Sekunde - jedes wird ausgewertet

# Farbe der Teams auf der LED am Queue (wer dran ist). Rot/Gruen sind fuer die Verbindung reserviert.
TEAM_COLORS = ([0, 90, 255], [255, 110, 0])        # Team A blau, Team B orange
TEAM_COLOR_NAMES = ("blau", "orange")

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
    def __init__(self, cue_port=5005, coral_engine=None, klassen=None):
        self.cfg = config.load()
        self._migrate_config()
        self.reader = make_reader(self.cfg["camera"], config.DATA_DIR).start()
        self.recorder = Recorder(os.path.join(config.DATA_DIR, "dataset"))
        self.uploader = self._start_uploader()
        self.coral = coral_engine or CoralEngine()          # Stufe 1: Kugeln finden (KI-Chip)
        self.klassen = klassen or klassen_engine()          # Stufe 2 + 3 auf dem KI-Chip (optional)
        table = self._table_for(self.reader)
        self.pipe = Pipeline(table, lambda r: CoralDetector(r, self.coral, klassen=self.klassen), recorder=self.recorder)
        self.pipe.auto_capture = bool(self.cfg.get("auto_capture"))
        self.pipe.game.manual = bool(self.cfg.get("manual"))
        self.pipe.on_capture = self._captured
        last = self.cfg.get("cue_address")
        self.cue = CueSensor(self.cfg.get("cue_castcode", ""), self.cfg.get("cue_offset", 0.0),
                             self.cfg.get("cue_flip", False), discovery_port=cue_port,
                             address=(last[0], cue_port) if last else None, on_found=self._cue_found,
                             enabled=self.cfg.get("cue_enabled", True),
                             on_button=self._cue_button, led_color=self._cue_led,
                             on_stroke=self._cue_stroke).start()
        self.align = AlignChecker(self.cfg.get("cue_tolerance", 2.0))
        self.cue_fw = CueUpdater(self.cue)                    # Firmware des Queues ueber das WLAN
        # jedes Rack wird aufgezeichnet (nur Daten); das Video baut der VPS - ohne VPS der Pi selbst
        self.rounds = RoundRecorder(os.path.join(config.DATA_DIR, "recordings"),
                                    local_build=lambda: self.uploader is None)
        if self.uploader is not None:
            self.uploader.rounds = self.rounds           # fertige Racks gehen mit zum VPS
        self.auth = None                                      # setzt web.start()
        self.panel_url = ""                                   # Adresse des Spiel-Panels (QR-Code)
        self.notes = Notices()                                # Meldungen oben rechts
        self.single = SingleBall(self.pipe, self.recorder, self.notes,      # Einzelkugel per Knopf
                                 has_upload=lambda: self.uploader is not None)
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
        # True, solange der Hauptthread das HDMI-Bild zeichnet: dann bekommt der Browser eine
        # verkleinerte Kopie davon. Ein zweites pygame-Zeichnen im Web-Thread parallel zur
        # HDMI-Ausgabe (kmsdrm) liess das Pi-Kameramodul haengen (Standbild).
        self.display_active = False
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
                if isinstance(ev, tuple) or self.pipe.monitor.ref_id != self._ref_id:
                    self.refresh_scene()          # auch "Kugel im Loch" sofort in Grafik und Panel
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
        rec_error = self.rounds.status().get("error")
        if rec_error:
            probs.append(("video", "warning", "Spielaufzeichnung", rec_error, 0.0))
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

    def _record_step(self):
        if not self.rounds.active:
            return
        sc = self.scene(live=False)
        self.rounds.add(sc)
        if sc["game"]["phase"] != "playing":
            self.rounds.stop()             # Rack vorbei -> Aufzeichnung fertig (Video baut der VPS)

    # ------------------------------------------------- Trainingsbilder (festgelegt wird auf dem VPS)
    def _capture_text(self):
        if self.uploader is None:
            return "Trainingsseite nicht eingerichtet - das Bild bleibt auf dem Pi"
        return "geht zur Trainingsseite, festgelegt wird dort"

    def _captured(self, name, source):
        """Trainingsbild gespeichert: kurz oben rechts melden (auch auf dem Beamer)."""
        self.notes.push("Bild automatisch gespeichert" if source == "auto" else "Bild gespeichert",
                        self._capture_text(), "success", ttl=3.0, key="capture")

    def save_model(self, data: bytes, name=""):
        """Hochgeladene Datei speichern: Coral-Modell (.tflite, Stufe 1), Klassen-Modell (Stufe 2 + 3 auf
        dem KI-Chip, billard_klassen*.tflite) oder Farb-/Streifen-Werte (billard_merkmale.json, Statistik).
        Der Detektor laedt alles von selbst (Aenderungszeit)."""
        if data[:1] == b"{":
            return self.save_features(data)
        if len(data) < 1000 or data[4:8] != b"TFL3":
            raise config.UserError("Das ist keine TFLite-Modelldatei (.tflite)")
        # Das Klassen-Modell (Stufe 2 + 3) ist klein (~0,2 MB), das Kugel-Modell (Stufe 1) mehrere MB:
        # ein kleines Modell ersetzt nie Stufe 1, auch wenn der Dateiname nicht passt
        if "klassen" in name.lower() or len(data) < 1_500_000:
            return self.save_klassen(data)
        if b"edgetpu-custom-op" not in data:
            raise config.UserError("Modell ist nicht für den Coral kompiliert "
                                   "(die Datei *_edgetpu.tflite aus dem Training nehmen)")
        folder = os.path.join(config.DATA_DIR, "models")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "billard_edgetpu.tflite")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        if os.path.exists(path):                     # vorheriges Modell aufheben (zurueck: Datei umbenennen)
            os.replace(path, path + ".vorher")
        os.replace(tmp, path)
        return f"Modell gespeichert ({len(data) / 1e6:.1f} MB). Der Coral lädt es in wenigen Sekunden."

    def save_klassen(self, data: bytes):
        os.makedirs(os.path.dirname(DATA_KLASSEN), exist_ok=True)
        tmp = DATA_KLASSEN + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, DATA_KLASSEN)
        where = "auf dem KI-Chip" if b"edgetpu-custom-op" in data else \
            "auf dem Prozessor (nicht für den Coral umgewandelt - in Colab mit edgetpu_compiler umwandeln)"
        return f"Klassen-Modell für Art und Farbe gespeichert ({len(data) / 1e3:.0f} KB), läuft {where}."

    def reset_klassen(self):
        try:
            os.remove(DATA_KLASSEN)
        except FileNotFoundError:
            return "Es gibt kein Klassen-Modell"
        return "Klassen-Modell gelöscht - Art und Farbe bestimmt wieder die Statistik"

    def cue_firmware(self, data: bytes):
        """Hochgeladene Queue-Firmware (.bin) ueber das WLAN aufspielen."""
        return self.cue_fw.start_file(data)

    def save_features(self, data: bytes):
        try:
            clf = kugel_merkmale.BallClassifier.from_dict(json.loads(data))
        except (ValueError, KeyError, TypeError) as e:
            raise config.UserError(f"Keine gültige Datei billard_merkmale.json ({e})")
        os.makedirs(os.path.dirname(DATA_FEATURES), exist_ok=True)
        clf.save(DATA_FEATURES)
        n = clf.info.get("kugeln")
        return "Farben und Streifen gespeichert" + (f" (gelernt aus {n} Kugeln)" if n else "") + "."

    def reset_features(self):
        try:
            os.remove(DATA_FEATURES)
        except FileNotFoundError:
            pass
        return "Gelernte Farben gelöscht - es gelten wieder die Startwerte"

    # ------------------------------------------------------------- Portal-API
    def _cue_found(self, address):
        """Queue im WLAN gefunden: Adresse merken (beim naechsten Start sofort da)."""
        self.cfg["cue_address"] = list(address)
        config.save(self.cfg)

    def _cue_led(self):
        """Farbe fuer die LED am Queue: Team, das dran ist - oder None (kein Spiel -> gruen)."""
        g = self.pipe.game
        if g.phase != "playing":
            return None
        return TEAM_COLORS[g.turn % 2]

    def _cue_stroke(self, strength):
        """Gyroskop im Queue hat einen Stoss gemessen: hilft der Stosserkennung (nur im Spiel)."""
        if self.pipe.game.phase == "playing" and self.cue.enabled:
            self.pipe.cue_stroke()

    def shot_done(self, source="Spiel-Panel"):
        """'Stoss fertig': der Spieler sagt, sein Stoss ist vorbei -> sofort auswerten."""
        if self.pipe.game.phase != "playing":
            raise config.UserError("Es läuft kein Spiel")
        if not self.pipe.finish_shot():
            raise config.UserError("Noch kein Ausgangszustand - erst den Tisch ruhig werden lassen")
        self.notes.push("Stoß fertig", f"{source}: wird ausgewertet, sobald der Tisch frei ist",
                        "info", ttl=4.0, key="stoss-fertig")
        return "Stoß wird ausgewertet, sobald der Tisch ruhig und frei ist"

    def _cue_button(self):
        """Knopf am Queue: ohne Spiel (LED gruen) Team A gegen Team B starten,
        im Spiel (LED in Teamfarbe) 'Stoss fertig'."""
        if self.pipe.game.phase == "playing":
            try:
                self.shot_done("Knopf am Queue")
            except config.UserError as e:
                print(f"[queue] Knopf: {e}", flush=True)
            return
        self.command("start", {"team_a": "Team A", "team_b": "Team B", "breaker": 0})
        self.notes.push("Spiel gestartet (Knopf am Queue)",
                        f"Team A = {TEAM_COLOR_NAMES[0]}, Team B = {TEAM_COLOR_NAMES[1]} - "
                        "die LED am Queue zeigt, wer dran ist", "success", ttl=8.0, key="queue-knopf")
        print("[queue] Knopf: Spiel Team A gegen Team B gestartet", flush=True)

    def _start_uploader(self):
        """Trainingsseite im Netz (nur wenn eingerichtet): gespeicherte Bilder hinschicken (und danach
        auf dem Pi loeschen), neue Modelle von dort holen."""
        url, key = os.environ.get("TRAINING_UPLOAD_URL", ""), os.environ.get("TRAINING_UPLOAD_KEY", "")
        if not (url and key):
            return None
        from uploader import Uploader
        return Uploader(self.recorder, url, key, install=self.install_model,
                        state_path=os.path.join(config.DATA_DIR, "models", "vom-vps.json")).start()

    def install_model(self, name, data):
        """Modell vom VPS uebernehmen - mit denselben Pruefungen wie beim Hochladen im Admin-Panel."""
        msg = self.save_model(data, name)
        self.notes.push("Neues Modell vom VPS", msg, "success", ttl=8.0, key="modell-vps")
        print(f"[modell] vom VPS: {name}: {msg}", flush=True)
        return msg

    def state_dict(self):
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
            "training": {"pending": self.recorder.waiting, "auto_capture": self.pipe.auto_capture,
                         "disk_ok": self.recorder.disk_ok(),
                         "upload": self.uploader.status() if self.uploader else None},
            "detector": {"coral": self.coral.status(), "not_ready": self.pipe.not_ready,
                         "klassen": self.klassen.status(),
                         "features": default_features().status(START_FEATURES)},
            "assist": bool(self.cfg.get("assist")),
            "suggestion": (self.suggestion or {}).get("text"),
            "scene_version": self.scene_version,
            "cue": dict(self.cue.status(), tolerance=self.align.tolerance,
                        ok=self.align.ok, err=None if self.align.err is None else round(self.align.err, 1)),
            "cue_fw": self.cue_fw.status(),
            "qr": dict(self.cfg.get("qr") or {}, url=self.qr_text(), panel_url=self.panel_url),
            "notices": self.notices(),
            "round": self.rounds.status(),
            "videos": self.videos(),
            "single": self.single.status(),
        }

    def game_state(self):
        """Was das Spiel-Panel braucht - ohne Admin-Einstellungen."""
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
                    "stroke": c["stroke"], "strokes": c["strokes"],
                    "ok": self.align.ok, "err": None if self.align.err is None else round(self.align.err, 1)},
            "round": self.rounds.status(),
            "videos": self.videos(),
            "training": {"pending": self.recorder.waiting, "auto_capture": self.pipe.auto_capture,
                         "upload": self.uploader is not None},
            "notices": self.notices(),
        }

    def videos(self):
        """Videos der aufgezeichneten Racks: vom VPS (Links direkt dorthin) und - ohne VPS - vom Pi."""
        remote = self.uploader.videos() if self.uploader else []
        return (remote + self.rounds.videos())[:30]

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
            self._record_command(name)
        return result

    def _record_command(self, name):
        """Jedes Rack automatisch aufzeichnen: Start / Neues Rack beginnt, Korrekturen sind Schritte,
        Sieg oder Abbruch beendet die Aufzeichnung."""
        if name in ("start", "new_rack"):
            self.rounds.start(self.scene(live=False))      # eine noch laufende wird vorher abgeschlossen
        elif self.rounds.active and name in ("foul", "undo", "undo_shot", "winner", "abort", "switch_turn",
                                             "set_group", "adjust", "ball"):
            sc = self.scene(live=False)
            if name in ("winner", "abort") or sc["game"]["phase"] != "playing":
                self.rounds.stop(sc)
            else:
                self.rounds.add(sc)

    def _command(self, name, p):
        g = self.pipe.game
        if name == "camera":
            return self._set_camera(p)
        if name == "reset_features":
            return self.reset_features()
        if name == "reset_klassen":
            return self.reset_klassen()
        if name.startswith("single_"):
            try:
                if name == "single_mode":
                    return self.single.set_enabled(bool(p.get("enabled")))
                if name == "single_ball":
                    return self.single.set_number(p["number"])
                if name == "single_empty":
                    return self.single.remember_empty()
                if name == "single_shot":
                    return self.single.shot()
            except ValueError as e:
                raise config.UserError(str(e))
            return None
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
        if name == "cue_fw_check":
            return self.cue_fw.check()
        if name == "cue_fw_update":
            return self.cue_fw.start_github()
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
                b = p.get("breaker")
                g.new_rack(None if b is None else int(b) % 2)
                self.pipe.restart_tracking()
            elif name == "abort":
                g.abort()
            elif name == "foul":
                g.foul()
            elif name == "shot_done":
                return self.shot_done()
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
            elif name == "ball":
                # Spiel-Panel: Kugel ins Loch / zurueck auf den Tisch (auch im Automatik-Modus als Korrektur)
                n = int(p.get("number", -1))
                if not 1 <= n <= 15:
                    raise ValueError("Kugel 1 bis 15")
                return g.pocket_ball(n) if p.get("pocketed", True) else g.restore_ball(n)
            elif name == "manual":
                on = bool(p.get("enabled"))
                self.cfg["manual"] = on
                config.save(self.cfg)
                g.set_manual(on)
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
                return f"Bild gespeichert - {self._capture_text()}."
            elif name == "auto_capture":
                on = bool(p.get("enabled"))
                self.cfg["auto_capture"] = self.pipe.auto_capture = on
                config.save(self.cfg)
                return ("Ab jetzt wird nach jedem Stoß ein Bild gespeichert" if on
                        else "Bilder werden nur noch per Knopf gespeichert")
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
    app.display_active = True
    try:
        while disp.handle_events():
            frame, fid, _ = app.reader.latest()
            disp.draw(frame, fid, app.pipe, app.reader, app.scene())
            clock.tick(DISPLAY_FPS)
    finally:
        app.display_active = False
        app.stop()
        disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
