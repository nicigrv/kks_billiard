"""
Simulierte ESP32-CAM mit einem laufenden 8-Ball-Spiel (zum Testen ohne Tisch).

    python3 -m sim.fake_table            # Stream unter http://<rechner>:8081/stream

Ablauf in Schleife: Rack liegt -> Break -> mehrere Stoesse -> neues Rack.
Fuer die Kalibrierung im Portal die Ecken anklicken oder diese Werte nehmen:
    DEFAULT_CORNERS = [(70, 150), (740, 120), (765, 470), (45, 480)]
"""

import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from geometry import TABLE_SIZES
from sim.render import DEFAULT_CORNERS, SimBall, rack_positions, random_layout, render_topdown, to_camera

L = TABLE_SIZES["8ft"]
W = L / 2
BOUNDARY = "billardframeboundary"
FPS = 10


class Scenario(threading.Thread):
    def __init__(self, hold_s=6.0):
        super().__init__(daemon=True)
        self.rng = np.random.default_rng()
        self.hold_s = hold_s
        self.jpeg = None
        self.lock = threading.Lock()
        self.caption = ""

    def _emit(self, balls, seconds, caption, occluder=None):
        self.caption = caption
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            cam = to_camera(render_topdown(balls, L, W, "gruen", self.rng, occluder), DEFAULT_CORNERS)
            cv2.putText(cam, caption, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            ok, jpg = cv2.imencode(".jpg", cam, [cv2.IMWRITE_JPEG_QUALITY, 80])
            with self.lock:
                self.jpeg = jpg.tobytes()
            time.sleep(1 / FPS)

    def _roll(self, balls):
        for _ in range(6):
            moving = [SimBall(b.number, min(L - .04, max(.04, b.x + self.rng.normal(0, .05))),
                              min(W - .04, max(.04, b.y + self.rng.normal(0, .05))), b.look) for b in balls]
            self._emit(moving, 0.12, "Kugeln rollen")

    def _shot(self, balls, pocket, everything=False):
        self._roll(balls)
        keep = [b for b in balls if b.number not in pocket]
        if everything:
            return random_layout(self.rng, [b.number for b in keep], L, W, cluster_prob=0.15)
        others = [b.number for b in keep if b.number != 0]
        movers = {0} | {int(n) for n in self.rng.choice(others, min(2, len(others)), replace=False)}
        placed = [b for b in keep if b.number not in movers]
        for b in keep:
            if b.number in movers:
                for _ in range(300):
                    nb = random_layout(self.rng, [b.number], L, W)[0]
                    if all(math.hypot(nb.x - p.x, nb.y - p.y) > 0.06 for p in placed):
                        placed.append(nb)
                        break
        return placed

    def run(self):
        while True:
            balls = rack_positions(L, W) + [SimBall(0, L * 0.25, W / 2)]
            self._emit(balls, self.hold_s * 2, "Rack aufgebaut - jetzt 'Neues Rack' / 'Spiel starten'")
            balls = self._shot(balls, {3, 11}, everything=True)
            self._emit(balls, self.hold_s, "Break: 3 und 11 versenkt")
            balls = self._shot(balls, {12})
            self._emit(balls, self.hold_s, "12 versenkt")
            self._emit(balls, 2, "Arm ueber dem Tisch", occluder=(L * .5, W * .5, .25))
            self._emit(balls, self.hold_s, "ruhig")
            balls = self._shot(balls, set())
            self._emit(balls, self.hold_s, "nichts versenkt")
            balls = self._shot(balls, {5})
            self._emit(balls, self.hold_s, "5 versenkt")
            balls = self._shot(balls, {0})
            self._emit(balls, self.hold_s, "Weisse versenkt")
            balls = balls + [SimBall(0, L * 0.3, W * 0.4)]
            self._emit(balls, self.hold_s, "Weisse wieder aufgelegt")
            balls = self._shot(balls, {8})
            self._emit(balls, self.hold_s * 2, "8 versenkt - Rack vorbei")


SCENARIO = Scenario()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/status"):
            body = b'{"camera_ok":true,"ap_mode":false,"rssi":-50,"free_heap":0,"version":"sim"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/stream"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY}")
        self.end_headers()
        try:
            while True:
                with SCENARIO.lock:
                    jpg = SCENARIO.jpeg
                if jpg:
                    self.wfile.write(f"\r\n--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                     f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg)
                time.sleep(1 / FPS)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main(port=8081):
    SCENARIO.start()
    print(f"Simulierte Kamera: http://0.0.0.0:{port}/stream", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
