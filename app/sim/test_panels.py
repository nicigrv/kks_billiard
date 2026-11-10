"""
Tests: Spiel-Panel (/ mit Spiel-PIN) und Admin-Panel (/admin), QR-Code und Meldungen auf dem
Beamer. Startet das echte Programm im Demo-Modus (ohne Kamera, ohne KI-Chip, ohne HDMI).

    python3 -m sim.test_panels
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

from notices import Notices
from qr import qr_matrix

FAILS = []
PORT = 8197
BASE = f"http://127.0.0.1:{PORT}"


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def req(path, data=None, cookie="", method=None):
    body = None if data is None else json.dumps(data).encode()
    r = urllib.request.Request(BASE + path, data=body, method=method or ("POST" if data is not None else "GET"),
                               headers={"Content-Type": "application/json", "Cookie": cookie})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def login(secret):
    code, body, headers = req("/api/login", {"secret": secret})
    cookie = (headers.get("Set-Cookie") or "").split(";")[0]
    return code, json.loads(body or b"{}"), cookie


def test_notices():
    print("Meldungen")
    t = [1000.0]
    n = Notices(clock=lambda: t[0])
    probs = [("kamera", "error", "Keine Kamerabilder", "", 3.0)]
    check(n.current(probs) == [], "Problem erst nach der Wartezeit zeigen (kein Flackern)")
    t[0] += 3.5
    cur = n.current(probs)
    check(len(cur) == 1 and cur[0]["key"] == "kamera", "danach als Meldung da")
    n.push("Queue verbunden", level="success", ttl=5)
    cur = n.current(probs + [("speicher", "warning", "Speicher", "", 0)])
    check([c["key"] for c in cur][:2] == ["kamera", "speicher"] and cur[2]["level"] == "success",
          "Fehler vor Warnungen vor Ereignissen")
    t[0] += 6
    check(n.current([]) == [], "Problem weg + Ereignis abgelaufen -> nichts mehr")


def test_qr():
    print("QR-Code")
    m = qr_matrix("http://192.168.88.120:8080/")
    check(m is not None and m.shape[0] == m.shape[1] and (m.shape[0] - 17) % 4 == 0, f"Matrix {m.shape if m is not None else None}")
    check(qr_matrix("") is None, "leer -> kein QR-Code")


def main():
    test_notices()
    test_qr()
    print("Panels (echtes Programm im Demo-Modus)")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump({"camera": {"source": "esp32", "url": "http://alt:81/stream"}, "corners": [[0, 0]] * 4}, f)
        env = dict(os.environ, DATA_DIR=d, ADMIN_PIN="1902", GAME_PIN="1212", SDL_VIDEODRIVER="dummy",
                   DEMO_IMAGE_URL="")
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen([sys.executable, "main.py", "--no-display", "--port", str(PORT)], cwd=here, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(100):
                try:
                    if req("/api/me")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            run_panels(d)
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
            if FAILS:
                print(out[-3000:])
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")


def run_panels(d):
    with open(os.path.join(d, "config.json")) as f:
        cfg = json.load(f)
    code, body, _ = req("/")
    check(code == 200 and b"Spiel-PIN" in body, "Startseite = Spiel-Panel")
    code, body, _ = req("/admin")
    check(code == 200 and b"Admin-Panel" in body, "/admin = Admin-Panel")
    check(req("/admin/")[0] == 200, "/admin/ geht auch")
    code, body, _ = req("/notify.js")
    check(code == 200 and b"Notify" in body, "Meldungen-Skript wird ausgeliefert")
    check(req("/api/game")[0] == 401, "Spiel-Panel ohne PIN: gesperrt")

    code, data, game = login("1212")
    check(code == 200 and data["role"] == "spiel", "Spiel-PIN 1212 -> Spiel-Panel")
    code, body, _ = req("/api/game", cookie=game)
    st = json.loads(body)
    check(code == 200 and "reviewers" not in st and "qr" not in st, "Spiel-Panel sieht keine Prüfer-Codes / Admin-Einstellungen")
    check(req("/api/state", cookie=game)[0] == 403, "Spiel-PIN: kein Admin-Zustand")
    code, body, _ = req("/api/assist", {"enabled": True}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["assist"] is True, "Spiel-Panel: Schlagverbesserung an")
    code, body, _ = req("/api/start", {"team_a": "Rot", "team_b": "Blau", "breaker": 0}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["game"]["phase"] == "playing", "Spiel-Panel: Spiel starten")
    code, body, _ = req("/api/cue_use", {"enabled": False}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["cue"]["enabled"] is False, "Spiel-Panel: Queue aus")
    for cmd, payload in (("camera", {"source": "demo"}), ("qr_settings", {"enabled": False}),
                         ("reviewer_add", {"name": "X"}), ("cue_pair", {"castcode": "SMQ234"})):
        check(req("/api/" + cmd, payload, cookie=game)[0] == 403, f"Spiel-PIN darf nicht: {cmd}")

    code, data, admin = login("1902")
    check(data["role"] == "admin", "Admin-PIN 1902")
    code, body, _ = req("/api/state", cookie=admin)
    st = json.loads(body)
    check(st["camera"]["source"] == "picam" and "url" not in st["camera"], "alte ESP32-CAM-Einstellung -> Pi-Kameramodul")
    check(st["qr"]["enabled"] and st["qr"]["url"].endswith(f":{PORT}/"), f"QR-Code zeigt aufs Spiel-Panel ({st['qr']['url']})")
    check(req("/api/game", cookie=admin)[0] == 200, "Admin darf auch ins Spiel-Panel")
    code, body, _ = req("/api/camera", {"source": "demo"}, cookie=admin)
    check(code == 200, "Admin: Demo-Kamera")
    code, body, _ = req("/api/qr_settings", {"text": "https://example.org/billard", "label": "Webseite"}, cookie=admin)
    check(code == 200 and json.loads(body)["state"]["qr"]["url"] == "https://example.org/billard", "Admin: eigener QR-Inhalt")
    check(req("/api/qr_settings", {"text": "x" * 400}, cookie=admin)[0] == 400, "zu langer QR-Inhalt abgelehnt")
    code, png, _ = req("/api/qr.png", cookie=admin)
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE) if code == 200 else None
    txt = cv2.QRCodeDetector().detectAndDecode(img)[0] if img is not None else ""
    check(txt == "https://example.org/billard", f"QR-Vorschau lesbar ({txt!r})")

    # Beamerbild: QR-Code unten rechts lesbar, Meldung (kein KI-Chip) oben rechts
    deadline = time.monotonic() + 15
    notices = []
    while time.monotonic() < deadline:
        notices = json.loads(req("/api/game", cookie=game)[1])["notices"]
        if any(n["key"] == "ki" for n in notices):
            break
        time.sleep(0.5)
    check(any(n["key"] == "ki" and n["level"] == "error" for n in notices), "Meldung: KI-Chip nicht bereit")
    code, jpg, _ = req("/api/beamer.jpg?q=fhd")
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    corner = img[int(h * 0.8):, int(w * 0.88):]
    corner = cv2.resize(corner, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    txt = cv2.QRCodeDetector().detectAndDecode(corner)[0]
    check(txt == "https://example.org/billard", f"Beamer: QR-Code unten rechts lesbar ({txt!r})")
    top_right = img[:int(h * 0.12), int(w * 0.7):].astype(int)
    red = ((top_right[:, :, 2] > 220) & (top_right[:, :, 1] < 100) & (top_right[:, :, 0] < 100)).sum()
    check(red > 50, "Beamer: rotes Meldungs-Symbol oben rechts")
    code, body, _ = req("/api/qr_settings", {"enabled": False}, cookie=admin)
    code, jpg, _ = req("/api/beamer.jpg?q=fhd&x=1")
    time.sleep(0.3)
    code, jpg, _ = req("/api/beamer.jpg?q=fhd&x=2")
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    corner = cv2.resize(img[int(h * 0.8):, int(w * 0.88):], None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    check(cv2.QRCodeDetector().detectAndDecode(corner)[0] == "", "QR-Code ausgeschaltet -> weg")
    check(b"Admin" not in req("/beamer")[1], "Beamer-Ansicht ohne Link zum Admin")

    code, data, _ = login("0000")
    check(code == 401, "falsche PIN abgelehnt")
    req("/api/logout", {}, cookie=game)
    check(req("/api/game", cookie=game)[0] == 401, "nach Abmelden gesperrt")


if __name__ == "__main__":
    main()
