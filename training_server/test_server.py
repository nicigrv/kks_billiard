"""
Tests fuer die Trainingsseite (ohne Netz, ohne Docker):
  * Pi laedt hoch (echter Uploader aus app/) -> Runde 1
  * Anmeldung mit Code, Sperre nach Fehlversuchen
  * Runde 1 zuerst, Runde 2 nie vom selben, "Spaeter", Unbrauchbar
  * Admin nur mit Authelia-Kopfzeilen, Trainings-ZIP im Format des Pi

    python3 test_server.py
"""

import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)
sys.path.insert(0, os.path.join(HIER, "..", "app"))

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


JPG = b"\xff\xd8\xff\xe0" + b"\0" * 200 + b"\xff\xd9"
BALLS = [{"x": 100.0, "y": 50.0, "kind": "cue", "color": "", "number": 0},
         {"x": 300.0, "y": 120.0, "kind": "solid", "color": "rot", "number": 3}]


class Client:
    def __init__(self, base, headers=None):
        self.base, self.cookie, self.headers = base, "", headers or {}

    def call(self, method, pfad, daten=None, headers=None):
        h = dict(self.headers, **(headers or {}))
        if self.cookie:
            h["Cookie"] = self.cookie
        body = None
        if daten is not None:
            body = json.dumps(daten).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + pfad, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                status, raw, ck = r.status, r.read(), r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            status, raw, ck = e.code, e.read(), e.headers.get("Set-Cookie")
        if ck:
            self.cookie = ck.split(";")[0]
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw


def main():
    with tempfile.TemporaryDirectory() as d:
        os.environ["UPLOAD_KEY"] = "test-schluessel-123"
        os.environ["COOKIE_SECURE"] = "0"
        import server
        server.UPLOAD_KEY = "test-schluessel-123"
        daten = server.Daten(os.path.join(d, "srv"))
        srv = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(daten))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        admin = Client(base, {"Remote-User": "nicolas", "Remote-Groups": "admins,m365-admins"})

        print("Upload vom Pi")
        from recorder import Recorder
        from uploader import Uploader
        rec = Recorder(os.path.join(d, "pi"))
        os.makedirs(rec.pending_dir)
        for i in range(3):
            name = f"20261113-1200{i:02d}-000"
            with open(os.path.join(rec.pending_dir, name + ".jpg"), "wb") as f:
                f.write(JPG)
            with open(os.path.join(rec.pending_dir, name + ".json"), "w") as f:
                json.dump({"width": 800, "height": 400, "radius": 10.0, "model": None, "balls": BALLS}, f)
        os.makedirs(rec.ok_img)
        os.makedirs(rec.ok_lbl)
        with open(os.path.join(rec.ok_img, "20261113-110000-000.jpg"), "wb") as f:
            f.write(JPG)
        with open(os.path.join(rec.ok_lbl, "20261113-110000-000.json"), "w") as f:
            json.dump({"width": 800, "height": 400, "radius": 10.0, "balls": BALLS, "labeled_by": "Admin"}, f)
        falsch = Uploader(rec, base, "falsch")
        falsch.run_once()
        check(falsch.error == "Server antwortet 403" and daten.zaehler()["runde1"] == 0, "falscher Schlüssel wird abgelehnt")
        up = Uploader(rec, base, "test-schluessel-123")
        check(up.run_once() == 4, "4 Bilder hochgeladen")
        check(up.run_once() == 0, "beim nächsten Mal nichts doppelt")
        check(Uploader(rec, base, "test-schluessel-123").run_once() == 0, "auch nach Neustart nichts doppelt (hochgeladen.txt)")
        z = daten.zaehler()
        check(z["runde1"] == 3 and z["runde2"] == 1, "offene -> Runde 1, am Tisch festgelegte -> Runde 2")

        print("Anmeldung")
        anna, ben = Client(base), Client(base)
        check(anna.call("GET", "/api/next")[0] == 401, "ohne Code kein Bild")
        check(admin.call("POST", "/api/admin/person", {"name": "Anna"})[0] == 200, "Admin legt Anna an")
        code_a = admin.call("GET", "/api/admin/stand")[1]["personen"][0]["code"]
        code_b = admin.call("POST", "/api/admin/person", {"name": "Ben"})[1]["code"]
        check(len(code_a) == 6 and code_a.isdigit(), "Code hat 6 Ziffern")
        check(admin.call("POST", "/api/admin/person", {"name": "anna"})[0] == 400, "doppelter Name abgelehnt")
        check(Client(base).call("GET", "/api/admin/stand")[0] == 403, "Admin-Bereich ohne greylo-Login gesperrt")
        check(Client(base, {"Remote-User": "x", "Remote-Groups": "nutzer"}).call("GET", "/api/admin/stand")[0] == 403,
              "Admin-Bereich nur für Gruppe admins")
        dieb = Client(base, {"X-Forwarded-For": "9.9.9.9"})
        for _ in range(8):
            dieb.call("POST", "/api/login", {"code": "000000"})
        check(dieb.call("POST", "/api/login", {"code": code_a})[0] == 429, "nach 8 Fehlversuchen kurz gesperrt")
        check(anna.call("POST", "/api/login", {"code": code_a}) == (200, {"name": "Anna"}), "Anna meldet sich an")
        check(ben.call("POST", "/api/login", {"code": code_b})[0] == 200, "Ben meldet sich an")

        print("Runde 1 und 2")
        s, a1 = anna.call("GET", "/api/next")
        check(s == 200 and a1["runde"] == 1, "Anna bekommt zuerst Runde 1")
        s, b1 = ben.call("GET", "/api/next")
        check(b1["runde"] == 1 and b1["name"] != a1["name"], "Ben bekommt gleichzeitig ein anderes Bild")
        check(ben.call("POST", "/api/speichern", {"name": a1["name"], "balls": BALLS})[0] == 409, "Annas offenes Bild kann Ben nicht speichern")
        ohne = [dict(BALLS[0]), {"x": 5, "y": 5, "kind": "solid", "color": ""}]
        check(anna.call("POST", "/api/speichern", {"name": a1["name"], "balls": ohne})[0] == 400, "Kugel ohne Nummer wird abgelehnt")
        check(anna.call("POST", "/api/speichern", {"name": a1["name"], "balls": BALLS})[0] == 200, "Anna speichert Runde 1")
        s, a2 = anna.call("GET", "/api/next")
        check(a2["runde"] == 1 and a2["name"] not in (a1["name"], b1["name"]), "Anna: nächstes freies Bild aus Runde 1")
        s, a3 = anna.call("GET", "/api/next?after=" + a2["name"])
        check(s == 200 and a3["runde"] == 2, "„Später“: keine weiteren freien Bilder in Runde 1 -> Runde 2")
        check(a3["name"] != a1["name"] and a3["von"] == "am Tisch (Admin)", "Runde 2 nie das eigene Bild (hier: am Tisch festgelegt)")
        korr = [dict(BALLS[0]), dict(BALLS[1], x=360.0, changed=True)]
        s, r = anna.call("POST", "/api/speichern", {"name": a3["name"], "balls": korr})
        check(s == 200 and "Korrigiert" in r["message"], "Runde 2 mit Korrektur -> fertig, als korrigiert vermerkt")
        check(ben.call("POST", "/api/speichern", {"name": b1["name"], "discard": True})[0] == 200, "Ben sortiert sein Bild aus")
        s, b2 = ben.call("GET", "/api/next")
        if b2.get("runde") == 1:
            ben.call("POST", "/api/speichern", {"name": b2["name"], "balls": BALLS})
            s, b2 = ben.call("GET", "/api/next")
        check(b2.get("runde") == 2 and b2["von"] == "Anna" and b2["balls"][1]["number"] == 3, "Ben kontrolliert Annas Bild, sieht ihre Kugeln")
        s, r = ben.call("POST", "/api/speichern", {"name": b2["name"], "balls": b2["balls"]})
        check(s == 200 and "Bestätigt" in r["message"], "unverändert -> bestätigt")
        s, a4 = anna.call("GET", "/api/next")
        check(s == 200 and a4["runde"] == 2 and a4["von"] == "Ben", "Anna kontrolliert Bens Bild")
        check(ben.call("GET", "/api/next")[0] == 404, "Ben: Rest aus Runde 2 ist sein eigenes -> nichts für ihn")
        anna.call("POST", "/api/speichern", {"name": a4["name"], "balls": a4["balls"]})
        check(anna.call("GET", "/api/next")[0] == 404, "alles erledigt -> 404")
        z = daten.zaehler()
        check(z == {"runde1": 0, "runde2": 0, "fertig": 3, "verworfen": 1}, f"Stand stimmt ({z})")

        print("Admin")
        s, st = admin.call("GET", "/api/admin/stand")
        anna_p = next(p for p in st["personen"] if p["name"] == "Anna")
        check(anna_p["runde1"] == 1 and anna_p["runde2"] == 2, "Zähler je Trainer")
        verworfen = next(b for b in st["letzte"] if b["status"] == "verworfen")
        check(admin.call("POST", "/api/admin/zurueck", {"name": verworfen["name"]})[0] == 200, "aussortiertes Bild zurück in Runde 1")
        admin.call("POST", "/api/admin/person/aktiv", {"id": anna_p["id"], "aktiv": False})
        check(anna.call("GET", "/api/next")[0] == 401, "gesperrt -> abgemeldet")
        neu = admin.call("POST", "/api/admin/person/code", {"id": anna_p["id"]})[1]["code"]
        admin.call("POST", "/api/admin/person/aktiv", {"id": anna_p["id"], "aktiv": True})
        check(anna.call("GET", "/api/next")[0] == 401 and anna.call("POST", "/api/login", {"code": code_a})[0] == 401,
              "neuer Code: alter Code und alte Sitzung gelten nicht mehr")
        check(anna.call("POST", "/api/login", {"code": neu})[0] == 200, "mit neuem Code wieder drin")

        print("ZIP")
        s, raw = admin.call("GET", "/api/admin/zip")
        z = zipfile.ZipFile(io.BytesIO(raw))
        namen = z.namelist()
        check("classes.txt" in namen and sum(n.startswith("images/") for n in namen) == 3, "ZIP: 3 fertige Bilder + classes.txt")
        txt = [n for n in namen if n.endswith(".txt") and n.startswith("labels/")]
        zeile = z.read(txt[0]).decode().splitlines()
        check(len(zeile) == 2 and zeile[0].startswith("0 0.12500 0.12500 0.02500 0.05000"), "YOLO-Zeilen wie am Pi")
        s, raw = admin.call("GET", "/api/admin/zip?runde1=1")
        check(sum(n.startswith("images/") for n in zipfile.ZipFile(io.BytesIO(raw)).namelist()) == 3, "inkl. Runde 1: auch 3 (keins wartet)")
        check(Client(base).call("GET", "/api/admin/zip")[0] == 403, "ZIP nur für Admins")
        srv.shutdown()

    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("Alles OK")


if __name__ == "__main__":
    main()
