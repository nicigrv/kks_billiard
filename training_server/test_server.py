"""
Tests fuer die Trainingsseite (ohne Netz, ohne Docker):
  * Pi laedt hoch (echter Uploader aus app/) -> Runde 1
  * Anmeldung mit Code, Sperre nach Fehlversuchen
  * jedes Bild 3x von verschiedenen Personen, ohne Vorschlaege; Mehrheit in der ZIP
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
        check(falsch.error == "Server antwortet 403" and daten.zaehler()["offen"] == 0, "falscher Schlüssel wird abgelehnt")
        up = Uploader(rec, base, "test-schluessel-123")
        check(up.run_once() == 4, "4 Bilder hochgeladen")
        check(up.run_once() == 0, "beim nächsten Mal nichts doppelt")
        check(Uploader(rec, base, "test-schluessel-123").run_once() == 0, "auch nach Neustart nichts doppelt (hochgeladen.txt)")
        z = daten.zaehler()
        check(z["offen"] == 4 and z["offen_je"] == [4, 0, 0], "alle 4 Bilder offen, auch das am Tisch festgelegte")

        print("Anmeldung")
        anna, ben, carl = Client(base), Client(base), Client(base)
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
        code_c = admin.call("POST", "/api/admin/person", {"name": "Carl"})[1]["code"]
        check(carl.call("POST", "/api/login", {"code": code_c})[0] == 200, "Carl meldet sich an")

        print("Dreimal festlegen, ohne Vorschlaege")
        s, a1 = anna.call("GET", "/api/next")
        check(s == 200 and a1["balls"] == [] and a1["durchgang"] == 1 and a1["ziel"] == 3, "Anna bekommt ein Bild ohne Vorschlag (1 von 3)")
        ohne = [dict(BALLS[0]), {"x": 5, "y": 5, "kind": "solid", "color": ""}]
        check(anna.call("POST", "/api/speichern", {"name": a1["name"], "balls": ohne})[0] == 400, "Kugel ohne Nummer wird abgelehnt")
        doppelt = [dict(BALLS[0]), dict(BALLS[1]), dict(BALLS[1], x=500.0)]
        s, r = anna.call("POST", "/api/speichern", {"name": a1["name"], "balls": doppelt})
        check(s == 400 and "doppelt" in r["error"], "doppelte Nummer wird abgelehnt")
        s, r = anna.call("POST", "/api/speichern", {"name": a1["name"], "balls": BALLS})
        check(s == 200 and "2 weitere" in r["message"], "Anna speichert, noch 2 weitere")
        check(anna.call("POST", "/api/speichern", {"name": a1["name"], "balls": BALLS})[0] == 409, "dasselbe Bild nicht zweimal von Anna")
        s, a2 = anna.call("GET", "/api/next")
        check(a2["name"] != a1["name"], "Anna bekommt ihr Bild nicht nochmal")
        s, b1 = ben.call("GET", "/api/next")
        check(b1["name"] == a1["name"] and b1["durchgang"] == 2 and b1["balls"] == [], "Ben bekommt zuerst das angefangene Bild, ohne Annas Kugeln")
        s, c1 = carl.call("GET", "/api/next")
        check(c1["name"] == a1["name"] and c1["durchgang"] == 2, "Carl darf gleichzeitig ran (1 fertig + Ben dran < 3)")
        dave = Client(base)
        code_d = admin.call("POST", "/api/admin/person", {"name": "Dave"})[1]["code"]
        dave.call("POST", "/api/login", {"code": code_d})
        s, d1 = dave.call("GET", "/api/next")
        check(d1["name"] != a1["name"], "Dave nicht: schon 1 fertig und 2 dran")
        check(ben.call("POST", "/api/speichern", {"name": a1["name"], "balls": BALLS})[0] == 200, "Ben speichert (2 von 3)")
        abweichend = [dict(BALLS[0]), dict(BALLS[1], x=360.0)]          # Kugel 3 weit daneben
        s, r = carl.call("POST", "/api/speichern", {"name": a1["name"], "balls": abweichend})
        check(s == 200 and "fertig" in r["message"], "Carl speichert als Dritter -> fertig")
        check(dave.call("POST", "/api/speichern", {"name": a1["name"], "balls": BALLS})[0] == 409, "fertiges Bild nimmt nichts mehr an")
        kugeln, einig = server.konsens([BALLS, BALLS, abweichend], 10.0)
        check(not einig and len(kugeln) == 2 and next(k for k in kugeln if k["number"] == 3)["x"] == 300.0,
              "Mehrheit: Kugel 3 an der Stelle, die 2 von 3 gesetzt haben")
        check(server.konsens([BALLS, abweichend], 10.0)[0] == [dict(BALLS[0], stimmen=2, x=100.0)], "bei 2 Festlegungen zählt nur, was beide gleich haben")
        check(ben.call("POST", "/api/speichern", {"name": b1["name"] if b1["name"] != a1["name"] else a2["name"], "discard": True})[0] == 200,
              "Ben sortiert ein Bild aus")
        z = daten.zaehler()
        check(z["fertig"] == 1 and z["verworfen"] == 1 and z["einig"] == 0, f"Stand stimmt ({z['fertig']} fertig, {z['einig']} einstimmig)")

        print("Admin")
        s, st = admin.call("GET", "/api/admin/stand")
        anna_p = next(p for p in st["personen"] if p["name"] == "Anna")
        check(anna_p["festgelegt"] == 1, "Zähler je Trainer")
        b = next(b for b in st["letzte"] if b["name"] == a1["name"])
        check(b["festlegungen"] == 3 and b["einig"] is False and b["wer"] == ["Anna", "Ben", "Carl"], "Admin sieht wer festgelegt hat")
        verworfen = next(b for b in st["letzte"] if b["status"] == "verworfen")
        check(admin.call("POST", "/api/admin/zurueck", {"name": verworfen["name"]})[0] == 200, "aussortiertes Bild wieder offen")
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
        check("classes.txt" in namen and sum(n.startswith("images/") for n in namen) == 1, "ZIP: 1 fertiges Bild + classes.txt")
        zeile = z.read(f"labels/{a1['name']}.txt").decode().splitlines()
        check(len(zeile) == 2 and zeile[0].startswith("0 0.12500 0.12500 0.02500 0.05000"), "YOLO-Zeilen wie am Pi (Mehrheit)")
        info = json.loads(z.read(f"labels/{a1['name']}.json"))
        check(info["festlegungen"] == 3 and info["einig"] is False, "json: 3 Festlegungen, nicht einstimmig")
        check(admin.call("GET", "/api/admin/zip?modus=einig")[0] == 404, "nur einstimmige: keins da -> Hinweis statt leerer ZIP")
        s, raw = admin.call("GET", "/api/admin/zip?modus=alle")
        check(sum(n.startswith("images/") for n in zipfile.ZipFile(io.BytesIO(raw)).namelist()) == 1, "inkl. unfertige: nur Bilder mit Festlegung")
        check(Client(base).call("GET", "/api/admin/zip")[0] == 403, "ZIP nur für Admins")

        print("Umstellung alter Daten")
        alt = server.Daten(os.path.join(d, "alt"))
        alt.db.execute("INSERT INTO bilder (name, eingang, breite, hoehe, radius, vorschlag, status) VALUES ('20261101-100000-000', 1, 800, 400, 10, '[]', 'runde2')")
        alt.db.commit()
        alt2 = server.Daten(os.path.join(d, "alt"))
        check(alt2.zaehler()["offen"] == 1, "alte Runde-2-Bilder werden beim Start zu offen")
        srv.shutdown()

    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("Alles OK")


if __name__ == "__main__":
    main()
