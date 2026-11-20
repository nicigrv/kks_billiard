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
            ok = False
            run_panels(d)
            ok = True
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
            if FAILS or not ok:
                print(out[-3000:])
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")


def run_panels(d):
    with open(os.path.join(d, "config.json")) as f:
        json.load(f)                          # config.json ist lesbar
    code, body, _ = req("/")
    check(code == 200 and b"Spiel-PIN" in body, "Startseite = Spiel-Panel")
    code, body, _ = req("/admin")
    check(code == 200 and b"Admin-Panel" in body, "/admin = Admin-Panel")
    check(req("/admin/")[0] == 200, "/admin/ geht auch")
    code, body, _ = req("/notify.js")
    check(code == 200 and b"Notify" in body, "Meldungen-Skript wird ausgeliefert")
    code, body, hdr = req("/ui.css")
    check(code == 200 and b"--tint" in body, "gemeinsames Aussehen (ui.css) wird ausgeliefert")
    check(b"/ui.css" in req("/")[1] and b"/ui.css" in req("/admin")[1], "Spiel- und Admin-Panel binden ui.css ein")
    check(req("/api/game")[0] == 401, "Spiel-Panel ohne PIN: gesperrt")

    code, data, game = login("1212")
    check(code == 200 and data["role"] == "spiel", "Spiel-PIN 1212 -> Spiel-Panel")
    code, body, _ = req("/api/game", cookie=game)
    st = json.loads(body)
    check(code == 200 and "qr" not in st and "camera" not in st, "Spiel-Panel sieht keine Admin-Einstellungen")
    check(req("/api/state", cookie=game)[0] == 403, "Spiel-PIN: kein Admin-Zustand")
    code, body, _ = req("/api/assist", {"enabled": True}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["assist"] is True, "Spiel-Panel: Schlagverbesserung an")
    code, body, _ = req("/api/start", {"team_a": "Rot", "team_b": "Blau", "breaker": 0}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["game"]["phase"] == "playing", "Spiel-Panel: Spiel starten")
    check(json.loads(body)["state"]["round"]["active"], "Spielstart: Aufzeichnung läuft automatisch")
    check(req("/api/record_round", {"enabled": True}, cookie=game)[0] == 403, "Aufzeichnen-Knopf gibt es nicht mehr")
    code, body, _ = req("/api/ball", {"number": 5, "pocketed": True}, cookie=game)
    g5 = json.loads(body)["state"]["game"] if code == 200 else {}
    check(code == 200 and 5 not in g5.get("on_table", []) and g5["remaining"]["solid"] == 6,
          "Spiel-Panel: Kugel 5 antippen -> im Loch")
    code, body, _ = req("/api/ball", {"number": 5, "pocketed": False}, cookie=game)
    check(code == 200 and 5 in json.loads(body)["state"]["game"]["on_table"], "… nochmal antippen -> zurück")
    check(req("/api/ball", {"number": 0, "pocketed": True}, cookie=game)[0] == 400, "Weiße/ungültige Nummer abgelehnt")
    code, body, _ = req("/api/manual", {"enabled": True}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["game"]["manual"] is True, "Spiel-Panel: manueller Modus an")
    code, body, _ = req("/api/manual", {"enabled": False}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["game"]["manual"] is False, "… und wieder aus")
    code, body, _ = req("/api/cue_use", {"enabled": False}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["cue"]["enabled"] is False, "Spiel-Panel: Queue aus")
    for cmd, payload in (("camera", {"source": "demo"}), ("qr_settings", {"enabled": False}),
                         ("delete_images", {"confirm": True}), ("cue_pair", {"castcode": "SMQ234"})):
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

    # Stufe 2 + 3 (gestreift? Farbe?): Datei hochladen, Status, loeschen
    st = json.loads(req("/api/state", cookie=admin)[1])
    f0 = st["detector"]["features"]
    check(f0["ready"] and not f0["own"], f"Farben: Startwerte aktiv ({f0['model']})")
    start = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models",
                              "start_merkmale.json"), "rb").read()

    def upload(data):
        r = urllib.request.Request(BASE + "/api/model", data=data, method="POST",
                                   headers={"Content-Type": "application/octet-stream", "Cookie": admin})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    code, res = upload(start)
    check(code == 200 and "Farben" in res["message"] and os.path.isfile(os.path.join(d, "models", "billard_merkmale.json")),
          f"billard_merkmale.json hochgeladen ({res.get('message')})")
    code, res = upload(b'{"format": "etwas anderes"}')
    check(code == 400 and "billard_merkmale" in res["error"], "falsche JSON-Datei abgelehnt")
    kl = b"xxxxTFL3" + b"edgetpu-custom-op" + b"0" * 200_000
    code, res = upload(kl)
    check(code == 200 and "Klassen-Modell" in res["message"]
          and os.path.isfile(os.path.join(d, "models", "billard_klassen_edgetpu.tflite"))
          and not os.path.exists(os.path.join(d, "models", "billard_edgetpu.tflite")),
          "kleines Modell (Stufe 2 + 3) ersetzt nie das Kugel-Modell (Stufe 1)")
    big = b"xxxxTFL3" + b"edgetpu-custom-op" + b"1" * 2_000_000
    upload(big)
    upload(big + b"2")
    check(os.path.isfile(os.path.join(d, "models", "billard_edgetpu.tflite.vorher")),
          "neues Kugel-Modell: das vorherige bleibt als .vorher erhalten")
    time.sleep(2.2)
    st = json.loads(req("/api/state", cookie=admin)[1])
    check(st["detector"]["features"]["own"], "hochgeladene Farben werden benutzt")
    check(req("/api/learn_features", {}, cookie=admin)[0] == 404, "Farben lernen gibt es am Pi nicht mehr (nur auf dem VPS)")
    code, body, _ = req("/api/reset_features", {}, cookie=admin)
    check(code == 200 and not os.path.exists(os.path.join(d, "models", "billard_merkmale.json")),
          "gelernte Farben loeschen -> Startwerte")
    code, body, _ = req("/api/qr_settings", {"text": "https://example.org/billard", "label": "Webseite"}, cookie=admin)
    check(code == 200 and json.loads(body)["state"]["qr"]["url"] == "https://example.org/billard", "Admin: eigener QR-Inhalt")
    check(req("/api/qr_settings", {"text": "x" * 400}, cookie=admin)[0] == 400, "zu langer QR-Inhalt abgelehnt")
    code, png, _ = req("/api/qr.png", cookie=admin)
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE) if code == 200 else None
    txt = cv2.QRCodeDetector().detectAndDecode(img)[0] if img is not None else ""
    check(txt == "https://example.org/billard", f"QR-Vorschau lesbar ({txt!r})")

    # Meldung (kein KI-Chip) fuer Beamer und Panels, QR-Code-Einstellung
    deadline = time.monotonic() + 15
    notices = []
    while time.monotonic() < deadline:
        notices = json.loads(req("/api/game", cookie=game)[1])["notices"]
        if any(n["key"] == "ki" for n in notices):
            break
        time.sleep(0.5)
    check(any(n["key"] == "ki" and n["level"] == "error" for n in notices), "Meldung: KI-Chip nicht bereit")
    st = json.loads(req("/api/state", cookie=admin)[1])
    check(st["qr"]["url"] == "https://example.org/billard", "QR-Code fuer den Beamer eingestellt")
    code, body, _ = req("/api/qr_settings", {"enabled": False}, cookie=admin)
    check(code == 200 and not json.loads(body)["state"]["qr"].get("enabled"), "QR-Code ausgeschaltet")
    check(req("/beamer", cookie=admin)[0] == 404 and req("/api/beamer.jpg", cookie=admin)[0] == 404,
          "keine Live-Uebertragung des Beamerbilds mehr (/beamer, /api/beamer.jpg)")

    print("Bilder am Tisch speichern - festgelegt und gelernt wird nur auf der Trainingsseite")
    code, body, _ = req("/api/capture", {}, cookie=game)
    st = json.loads(body).get("state", {}) if code == 200 else {}
    check(code == 200 and st["training"]["pending"] == 1 and "Trainingsseite nicht eingerichtet" in json.loads(body)["message"],
          "Spiel-Panel: Bild speichern (ohne Trainingsseite bleibt es auf dem Pi)")
    code, body, _ = req("/api/auto_capture", {"enabled": True}, cookie=game)
    check(code == 200 and json.loads(body)["state"]["training"]["auto_capture"] is True, "Spiel-Panel: automatisch nach jedem Stoß")
    req("/api/auto_capture", {"enabled": False}, cookie=game)
    st = json.loads(req("/api/state", cookie=admin)[1])
    check(st["training"]["pending"] == 1 and st["training"]["upload"] is None and "reviewers" not in st,
          "Admin: 1 Bild auf dem Pi, keine Trainingsseite, keine Prüfer")
    for pfad in ("/training", "/pruefen", "/api/review/next", "/api/round", "/api/snapshot", "/api/dataset.zip",
                 "/api/all-images.zip"):
        check(req(pfad, cookie=admin)[0] == 404, f"am Pi nicht mehr da: {pfad}")
    for cmd, payload in (("reviewer_add", {"name": "Anna"}), ("round_start", {}), ("review_save", {"name": "x"}),
                         ("train_correction", {"id": 1}), ("single_start", {"count": 5})):
        check(req("/api/" + cmd, payload, cookie=admin)[0] == 404, f"Befehl gibt es nicht mehr: {cmd}")
    req("/api/single_mode", {"enabled": True}, cookie=admin)
    req("/api/single_ball", {"number": 3}, cookie=admin)
    code, body, _ = req("/api/single_shot", {}, cookie=admin)
    check(code == 400 and "Trainingsseite" in json.loads(body)["error"], "Einzelkugel per Knopf nur mit Trainingsseite")
    code, body, _ = req("/api/delete_images", {"confirm": True}, cookie=admin)
    check(code == 200 and json.loads(body)["state"]["training"]["pending"] == 0, "Admin löscht die Bilder auf dem Pi")

    code, body, _ = req("/api/new_rack", {"breaker": "1"}, cookie=game)
    g = json.loads(body).get("state", {}).get("game", {}) if code == 200 else {}
    check(g.get("turn") == 1 and g.get("phase") == "playing", f"Neues Rack mit Anstoß als Text ({code})")
    code, body, _ = req("/api/new_rack", {"breaker": "x"}, cookie=game)
    check(code == 400 and req("/api/game", cookie=game)[0] == 200, "ungültiger Anstoß abgelehnt, Spielstand heil")
    r = urllib.request.Request(BASE + "/api/assist", data=b"[1]", method="POST",
                               headers={"Content-Type": "application/json", "Cookie": game})
    try:
        urllib.request.urlopen(r, timeout=10)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check(code == 400, "Befehl ohne JSON-Objekt abgelehnt")

    code, data, _ = login("0000")
    check(code == 401, "falsche PIN abgelehnt")
    req("/api/logout", {}, cookie=game)
    check(req("/api/game", cookie=game)[0] == 401, "nach Abmelden gesperrt")

    # Admin-PIN als Kopfzeile (fuer Skripte): richtig geht, Durchprobieren wird gesperrt
    def with_pin(pin):
        r = urllib.request.Request(BASE + "/api/state", headers={"X-Admin-Pin": pin})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
    check(with_pin("1902") == 200, "PIN-Kopfzeile: richtige Admin-PIN geht")
    for pin in ("1000", "1001", "1002", "1003", "1004"):
        with_pin(pin)
    check(with_pin("1902") == 401, "PIN-Kopfzeile: nach 5 Fehlversuchen gesperrt (kein Durchprobieren)")


if __name__ == "__main__":
    main()
