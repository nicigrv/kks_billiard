"""
Simuliertes Queue-Gyroskop (zum Testen ohne Hardware).

    python3 -m sim.fake_cue                       # sendet an 127.0.0.1:5005
    python3 -m sim.fake_cue --host 192.168.88.10  # an den Pi im Netz

Ablauf in Schleife (14 s): Queue schwenkt herum, naehert sich dem Vorschlag des Pi
(liest /api/state), bleibt ein paar Sekunden auf dem Ziel (gruener Haken),
schwenkt wieder weg. Ohne Vorschlag pendelt er einfach hin und her.
"""

import argparse
import json
import math
import os
import random
import socket
import time
import urllib.request


def get_state(url):
    try:
        req = urllib.request.Request(url + "/api/state", headers={"X-Admin-Pin": os.environ.get("ADMIN_PIN", "")})
        with urllib.request.urlopen(req, timeout=0.5) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5005)
    ap.add_argument("--portal", default=f"http://127.0.0.1:{os.environ.get('WEB_PORT', '8080')}")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    yaw, seq, phase_t = 30.0, 0, time.monotonic()
    target = None
    print(f"Simulierter Queue -> {args.host}:{args.port}", flush=True)
    while True:
        t = (time.monotonic() - phase_t) % 14
        if seq % 10 == 0:
            st = get_state(args.portal)
            cue = (st or {}).get("cue") or {}
            err = cue.get("err") if cue.get("connected") else None
            # Grafik-Winkel = -Rohwinkel: um die Abweichung weiterdrehen, dann passt es
            target = None if err is None else yaw + err
        if target is not None and 4 < t < 11:
            yaw += max(-2.0, min(2.0, 0.15 * (target - yaw))) + random.gauss(0, 0.03)
        else:
            yaw += 1.5 * math.sin(t * 0.8) + random.gauss(0, 0.1)
        pkt = {"id": "sim", "seq": seq, "yaw": round(((yaw + 180) % 360) - 180, 2),
               "pitch": round(-3 + random.gauss(0, 0.3), 2), "still": 0, "bat": 3.95}
        sock.sendto(json.dumps(pkt).encode(), (args.host, args.port))
        seq += 1
        time.sleep(1 / 30)


if __name__ == "__main__":
    main()
