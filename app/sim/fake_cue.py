"""
Simulierter Queue (zum Testen ohne Hardware) - verhaelt sich wie die Firmware in cue_sensor/:

  * Castcode (Standard SMQ234, aenderbar mit --castcode)
  * antwortet auf die Suche des Pi (UDP 5005, {"suche": "<castcode>"})
  * HTTP-API  GET /api/lage  (Kopfzeile X-Castcode)

    python3 -m sim.fake_cue                         # dann im Portal Castcode SMQ234 eintragen

Ablauf der Bewegung in Schleife (14 s): Queue schwenkt herum, naehert sich dem Vorschlag
des Pi (liest /api/state), bleibt ein paar Sekunden auf dem Ziel (gruener Haken),
schwenkt wieder weg. Ohne Vorschlag pendelt er einfach hin und her.
"""

import argparse
import json
import math
import os
import random
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Cue:
    def __init__(self, castcode):
        self.castcode = castcode
        self.yaw = 30.0
        self.seq = 0
        self.lock = threading.Lock()

    def reading(self):
        with self.lock:
            self.seq += 1
            return {"castcode": self.castcode, "seq": self.seq, "yaw": round(((self.yaw + 180) % 360) - 180, 2),
                    "pitch": round(-3 + random.gauss(0, 0.3), 2), "still": 0, "bat": 3.95,
                    "ms": int(time.monotonic() * 1000)}


def get_state(url):
    try:
        req = urllib.request.Request(url + "/api/state", headers={"X-Admin-Pin": os.environ.get("ADMIN_PIN", "")})
        with urllib.request.urlopen(req, timeout=0.5) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def steer(cue, portal):
    """Bewegung wie von Hand: pendeln, Vorschlag ansteuern, wegschwenken."""
    t0, n, target = time.monotonic(), 0, None
    while True:
        t = (time.monotonic() - t0) % 14
        if n % 10 == 0:
            st = get_state(portal)
            c = (st or {}).get("cue") or {}
            err = c.get("err") if c.get("connected") else None
            # Grafik-Winkel = -Rohwinkel: um die Abweichung weiterdrehen, dann passt es
            target = None if err is None else cue.yaw + err
        with cue.lock:
            if target is not None and 4 < t < 11:
                cue.yaw += max(-2.0, min(2.0, 0.15 * (target - cue.yaw))) + random.gauss(0, 0.03)
            else:
                cue.yaw += 1.5 * math.sin(t * 0.8) + random.gauss(0, 0.1)
        n += 1
        time.sleep(1 / 30)


def discovery(cue, port, api_port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    while True:
        raw, src = sock.recvfrom(512)
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if str(d.get("suche", "")).upper() == cue.castcode:
            sock.sendto(json.dumps({"castcode": cue.castcode, "ip": "127.0.0.1", "port": api_port}).encode(), src)


def make_handler(cue):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if not self.path.startswith("/api/lage"):
                self.send_error(404)
                return
            if self.headers.get("X-Castcode", "").upper() != cue.castcode:
                body, code = b'{"error":"falscher Castcode"}', 403
            else:
                body, code = json.dumps(cue.reading()).encode(), 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--castcode", default=os.environ.get("FAKE_CASTCODE", "SMQ234"))
    ap.add_argument("--api-port", type=int, default=int(os.environ.get("FAKE_CUE_PORT", "8092")))
    ap.add_argument("--discovery-port", type=int, default=int(os.environ.get("CUE_PORT", "5005")))
    ap.add_argument("--portal", default=f"http://127.0.0.1:{os.environ.get('WEB_PORT', '8080')}")
    args = ap.parse_args()
    cue = Cue(args.castcode.upper())
    threading.Thread(target=steer, args=(cue, args.portal), daemon=True).start()
    threading.Thread(target=discovery, args=(cue, args.discovery_port, args.api_port), daemon=True).start()
    print(f"Simulierter Queue: Castcode {cue.castcode}, API http://127.0.0.1:{args.api_port}/api/lage", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.api_port), make_handler(cue)).serve_forever()


if __name__ == "__main__":
    main()
