"""
Billard-System: Kamera -> Kugelerkennung -> 8-Ball-Regeln -> Scoreboard (HDMI) + Admin-Portal.

    python3 main.py --url http://192.168.88.200:81/stream
    python3 main.py --url ... --windowed       # zum Testen im Fenster
    python3 main.py --url ... --no-display     # nur Portal, keine HDMI-Ausgabe

Threads:
  Kamera     liest den MJPEG-Stream (stream_reader.py), haelt nur das neueste Bild
  Erkennung  verarbeitet jedes neue Bild (pipeline.py)
  Web        Admin-Portal auf Port 8080 (web.py)
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
from geometry import Table, TABLE_SIZES
from pipeline import Pipeline
from recorder import Recorder
from stream_reader import MjpegReader

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
    def __init__(self, url):
        self.cfg = config.load()
        self.reader = MjpegReader(url).start()
        self.recorder = Recorder(os.path.join(config.DATA_DIR, "dataset"))
        self.recorder.enabled = bool(self.cfg.get("record_dataset"))
        table = Table(self.cfg.get("corners"), TABLE_SIZES.get(self.cfg.get("table_size"), TABLE_SIZES["8ft"]))
        self.pipe = Pipeline(table, recorder=self.recorder)
        self.proc_fps = 0.0
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
            except Exception as e:  # ein kaputtes Bild darf das System nicht stoppen
                print(f"[erkennung] Fehler: {type(e).__name__}: {e}", flush=True)
            n += 1
            if time.monotonic() - t0 >= 2.0:
                self.proc_fps = n / (time.monotonic() - t0)
                n, t0 = 0, time.monotonic()

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
        g = self.pipe.game
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


def main():
    ap = argparse.ArgumentParser(description="Billard-Scoreboard")
    ap.add_argument("--url", default=os.environ.get("CAM_URL", "http://192.168.88.200:81/stream"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--no-display", action="store_true")
    args = ap.parse_args()

    app = App(args.url)
    web.start(app, args.port)
    ip = host_ip()
    admin_url = f"http://{ip}:{args.port}" if ip else ""
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
    disp = Display(args.windowed, admin_url)
    disp.open()
    import pygame
    clock = pygame.time.Clock()
    try:
        while disp.handle_events():
            frame, fid, _ = app.reader.latest()
            disp.draw(frame, fid, app.pipe, app.reader)
            clock.tick(20)
    finally:
        app.stop()
        disp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
