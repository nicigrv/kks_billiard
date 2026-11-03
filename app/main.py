"""
Billard-System: Kamera -> Kugelerkennung -> 8-Ball-Regeln -> Scoreboard (HDMI) + Admin-Portal.

    python3 main.py --url http://192.168.88.200:81/stream
    python3 main.py --url ... --windowed       # zum Testen im Fenster
    python3 main.py --url ... --no-display     # nur Portal, keine HDMI-Ausgabe

Threads:
  Kamera     liest den MJPEG-Stream (stream_reader.py), haelt nur das neueste Bild
  Erkennung  verarbeitet jedes neue Bild (pipeline.py)
  Queue      empfaengt die Lage des Queues vom Gyroskop (cue_sensor.py)
  Web        Admin-Portal auf Port 8080 (web.py)
  Video      baut am Ende einer aufgezeichneten Runde das Video (replay.py)
  Haupt      Anzeige mit pygame (display.py)
"""

import argparse
import os
import socket
import sys
import threading
import time

import cv2

import config
import web
from ballnum import ball_number
from cue_sensor import AlignChecker, CueSensor
from geometry import Table, TABLE_SIZES
from pipeline import Pipeline
from planner import plan_shot
from recorder import Recorder
from replay import RoundRecorder
from scene import SceneRenderer, bar_balls, surface_to_bgr
from stream_reader import MjpegReader

KINDS = ("cue", "eight", "solid", "stripe")
COLORS = ("gelb", "blau", "rot", "violett", "orange", "gruen", "weinrot", "schwarz", "")

KIND_BGR = {"cue": (255, 255, 255), "eight": (130, 130, 130), "solid": (0, 190, 255), "stripe": (255, 200, 0)}


def host_ip():
    """IP des Pi im Heimnetz (fuer die Anzeige 'Admin: http://...')."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


class App:
    def __init__(self, url, cue_port=5005):
        self.cfg = config.load()
        self.reader = MjpegReader(url).start()
        self.recorder = Recorder(os.path.join(config.DATA_DIR, "dataset"))
        self.recorder.enabled = bool(self.cfg.get("record_dataset"))
        table = Table(self.cfg.get("corners"), TABLE_SIZES.get(self.cfg.get("table_size"), TABLE_SIZES["8ft"]))
        self.pipe = Pipeline(table, recorder=self.recorder)
        self.cue = CueSensor(cue_port, self.cfg.get("cue_offset", 0.0), self.cfg.get("cue_flip", False)).start()
        self.align = AlignChecker(self.cfg.get("cue_tolerance", 2.0))
        self.rounds = RoundRecorder(os.path.join(config.DATA_DIR, "recordings"))
        self.admin_url = ""
        self.proc_fps = 0.0
        # Grafik-Zustand
        self.scene_lock = threading.RLock()
        self.scene_version = 0
        self.suggestion = None
        self.seen_numbers = set()
        self._ref_id = -1
        self._web_renderer = SceneRenderer()
        self._graphic = (None, None)        # (Schluessel, JPEG)
        self._snapshots = {}
        self._snap_id = 0
        self._stop = threading.Event()
        threading.Thread(target=self._process_loop, name="erkennung", daemon=True).start()

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
        with self.scene_lock:
            sug = self.suggestion
            seen = set(self.seen_numbers)
            cue = None
            if live and self.cfg.get("assist"):
                angle = self.cue.table_angle()
                target = sug.get("angle") if sug else None
                ok = self.align.update(angle, target)
                cue = {"angle": angle, "ok": ok, "err": self.align.err, "connected": self.cue.connected}
        sb = []
        for b in balls:
            sb.append({"x": float(b.x), "y": float(b.y), "kind": b.kind, "color": b.color,
                       "number": ball_number(b.kind, b.color)})
        visible = {d["number"] for d in sb} - {None, 0}
        info = f"Kamera {self.reader.fps:4.1f} fps"
        if self.admin_url:
            info = f"Admin: {self.admin_url}   " + info
        return {"balls": sb, "radius": radius, "game": game, "bar": bar_balls(game, visible, seen),
                "suggestion": sug, "cue": cue, "status": status, "info": info, "version": self.scene_version}

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
        from detector import Ball
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
        self.recorder.save_labeled(warped, balls, numbers)
        applied = 0
        if p.get("apply") and corrections:
            with self.pipe.lock:
                applied = self.pipe.correct_balls(corrections)
        return (f"Gespeichert ({self.recorder.corrected} korrigierte Bilder)"
                + (f", {applied} Kugel(n) im Spiel übernommen" if applied else ""))

    # ------------------------------------------------------------- Portal-API
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
            "table_size": self.cfg.get("table_size"),
            "record": self.recorder.enabled,
            "dataset_count": self.recorder.count,
            "corrected_count": self.recorder.corrected,
            "assist": bool(self.cfg.get("assist")),
            "suggestion": (self.suggestion or {}).get("text"),
            "scene_version": self.scene_version,
            "cue": dict(self.cue.status(), tolerance=self.align.tolerance,
                        ok=self.align.ok, err=None if self.align.err is None else round(self.align.err, 1)),
            "round": self.rounds.status(),
            "videos": self.rounds.videos(),
        }

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
                table = Table(corners, TABLE_SIZES[size])
                self.cfg.update(corners=table.corners, table_size=size)
                config.save(self.cfg)
                self.pipe.set_table(table)
                return "Tisch gespeichert. Die Erkennung startet neu."
            elif name == "record":
                self.recorder.enabled = bool(p.get("enabled"))
                self.cfg["record_dataset"] = self.recorder.enabled
                config.save(self.cfg)
                return "Sammeln gestartet" if self.recorder.enabled else "Sammeln gestoppt"
            else:
                return None
            return g.message

    def stop(self):
        self._stop.set()
        self.reader.stop()
        self.cue.stop()


def main():
    ap = argparse.ArgumentParser(description="Billard-Scoreboard")
    ap.add_argument("--url", default=os.environ.get("CAM_URL", "http://192.168.88.200:81/stream"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    ap.add_argument("--cue-port", type=int, default=int(os.environ.get("CUE_PORT", "5005")))
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--no-display", action="store_true")
    args = ap.parse_args()

    app = App(args.url, args.cue_port)
    web.start(app, args.port)
    ip = host_ip()
    admin_url = f"http://{ip}:{args.port}" if ip else ""
    app.admin_url = admin_url
    print(f"[start] Kamera {args.url}, Admin {admin_url or '(IP unbekannt)'}", flush=True)

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
