"""
Fake-ESP32-CAM zum Testen ohne Hardware.

Sendet einen MJPEG-Stream mit einem gruenen "Tisch" und einer rollenden Kugel
unter http://<rechner>:8081/stream

    python3 fake_cam.py
    python3 viewer.py --url http://127.0.0.1:8081/stream --windowed
"""

import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

BOUNDARY = "billardframeboundary"
W, H, FPS = 800, 600, 20


def make_frame(t: float):
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (40, 110, 30)  # Tuch (BGR)
    cv2.rectangle(img, (20, 20), (W - 20, H - 20), (20, 60, 90), 30)  # Bande
    x = int(W / 2 + (W / 2 - 80) * math.sin(t * 0.9))
    y = int(H / 2 + (H / 2 - 80) * math.sin(t * 1.3))
    cv2.circle(img, (x, y), 18, (240, 240, 240), -1)
    cv2.putText(img, time.strftime("%H:%M:%S"), (40, 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpg.tobytes()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace;boundary={BOUNDARY}")
        self.end_headers()
        t0 = time.monotonic()
        try:
            while True:
                jpg = make_frame(time.monotonic() - t0)
                self.wfile.write(f"\r\n--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                                 f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                self.wfile.write(jpg)
                time.sleep(1 / FPS)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("Fake-Cam laeuft: http://127.0.0.1:8081/stream")
    ThreadingHTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
