"""
Tests: Firmware des Queues ueber das WLAN aktualisieren (cue_update.py), ohne Hardware und ohne
Internet - gegen einen simulierten Queue (/api/info, /update) und ein simuliertes GitHub-Release.

    python3 -m sim.test_cue_update
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cue_update
from config import UserError
from cue_update import CueUpdater

FAILS = []
CODE = "SMQ234"
FIRMWARE = bytes([0xE9]) + bytes(range(256)) * 800          # sieht aus wie eine ESP32-App (~200 kB)
cue_update.RESTART_FIRST_S = 0.1


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


class FakeQueue:
    """Wie die Firmware: /api/info und /update (Multipart, Kopfzeile X-Castcode)."""
    def __init__(self, ota=True):
        self.info = {"geraet": "billard-queue", "fw": "3.3.0", "release": "v0.1.7", "board": "m5stack-atom"}
        if ota:
            self.info["ota"] = 1
        self.received = None
        self.code_seen = None


def queue_handler(q):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, d):
            b = json.dumps(d).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/api/info":
                return self._json(200, q.info)
            self._json(404, {"error": "nicht gefunden"})

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            q.code_seen = self.headers.get("X-Castcode")
            if q.code_seen != CODE:
                return self._json(403, {"error": "falscher Castcode"})
            boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
            part = body.split(b"--" + boundary)[1]
            q.received = part.split(b"\r\n\r\n", 1)[1][:-2]          # ohne abschliessendes \r\n
            q.info = dict(q.info, release=q.next_release)
            self._json(200, {"ok": True})
    return H


def github_handler(port_box):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            base = f"http://127.0.0.1:{port_box[0]}"
            if self.path == "/latest":
                b = json.dumps({"tag_name": "v0.1.8", "assets": [
                    {"name": "queue-ota-m5stack-atom.bin", "browser_download_url": base + "/fw.bin"},
                    {"name": "billard-rasp.zip", "browser_download_url": base + "/x.zip"}]}).encode()
            elif self.path == "/fw.bin":
                b = FIRMWARE
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    return H


class FakeCue:
    def __init__(self, port):
        self.address = ("127.0.0.1", 5005)
        self.connected = True
        self.castcode = CODE


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_done(up, s=10):
    end = time.monotonic() + s
    while up.busy and time.monotonic() < end:
        time.sleep(0.05)
    return not up.busy


def main():
    q = FakeQueue()
    q.next_release = "v0.1.8"
    qs = serve(queue_handler(q))
    box = [0]
    gh = serve(github_handler(box))
    box[0] = gh.server_address[1]
    cue = FakeCue(qs.server_address[1])
    up = CueUpdater(cue, http_port=qs.server_address[1], release_api=f"http://127.0.0.1:{box[0]}/latest")

    print("Nach Update suchen")
    msg = up.check()
    check("v0.1.7" in msg and "v0.1.8" in msg, f"zeigt Queue- und neueste Version ({msg})")
    st = up.status()
    check(st["release"] == "v0.1.7" and st["latest"] == "v0.1.8" and st["board"] == "m5stack-atom",
          "Status: Release, neueste Version, Board")

    print("Neueste Version von GitHub installieren")
    up.start_github()
    check(wait_done(up), "Update fertig")
    check(up.ok is True, f"erfolgreich ({up.state})")
    check(q.received == FIRMWARE, "Queue hat genau die Firmware aus dem Release bekommen")
    check(q.code_seen == CODE, "Castcode in der Kopfzeile X-Castcode")
    check(up.status()["release"] == "v0.1.8", "Queue meldet danach die neue Version")
    check("Queue ist aktuell" in up.check(), "danach: aktuell")

    print("Eigene Datei")
    usb = b"\x00" * 0x10000 + FIRMWARE                        # Datei fuer Adresse 0x0 (Bootloader davor)
    try:
        up.start_file(usb)
        check(False, "Datei fuer USB (0x0) wird abgelehnt")
    except UserError as e:
        check("USB" in str(e), f"Datei fuer USB (0x0) wird abgelehnt ({e})")
    try:
        up.start_file(b"hallo" * 50000)
        check(False, "keine Firmware wird abgelehnt")
    except UserError:
        check(True, "keine Firmware wird abgelehnt")
    q.next_release = "v0.1.8"
    up.start_file(FIRMWARE)
    check(wait_done(up) and up.ok is True, f"eigene .bin aufgespielt ({up.state})")

    print("Fehlerfaelle")
    cue.castcode = "AAAAAA"
    up.start_file(FIRMWARE)
    check(wait_done(up) and up.ok is False and "falscher Castcode" in up.state,
          f"falscher Castcode: Fehler, alte Firmware bleibt ({up.state})")
    cue.castcode = CODE
    q.info.pop("ota")
    try:
        up.check()
        check(False, "alte Firmware ohne Online-Update wird erkannt")
    except UserError as e:
        check("USB" in str(e), f"alte Firmware ohne Online-Update wird erkannt ({e})")
    cue.connected = False
    try:
        up.start_github()
        check(False, "Queue nicht verbunden")
    except UserError:
        check(True, "Queue nicht verbunden: verstaendliche Meldung")

    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")


if __name__ == "__main__":
    main()
