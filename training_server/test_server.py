"""
Tests fuer die Trainingsseite (ohne Netz, ohne Docker):
  * Pi laedt hoch (echter Uploader aus app/) -> Runde 1
  * Anmeldung mit Code, Sperre nach Fehlversuchen
  * jedes Bild 3x von verschiedenen Personen, ohne Vorschlaege; Mehrheit in der ZIP
  * Admin nur mit Authelia-Kopfzeilen, Trainings-ZIP im Format des Pi
  * Modelle fuer den Pi: Farben lernen auf dem Server, hochladen, Pi holt sie (echter Uploader)
  * Spielaufzeichnung: Pi schickt jedes Rack als Daten, der Server baut das Video (echter Uploader)

    python3 test_server.py
"""

import io
import json
import math
import os
import sys
import tempfile
import threading
import time
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
        mod = server.modelle.Modelle(os.path.join(d, "srv"))
        auf = server.aufzeichnung.Aufzeichnungen(os.path.join(d, "srv"))
        srv = server.http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(daten, mod, auf))
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
        check(rec.waiting == 0 and not [n for n in os.listdir(rec.pending_dir) if n.endswith(".jpg")]
              and not os.listdir(rec.ok_img), "nach dem Upload auf dem Pi gelöscht (Speicher)")
        check(up.run_once() == 0, "beim nächsten Mal nichts doppelt")
        check(Uploader(rec, base, "test-schluessel-123").run_once() == 0, "auch nach Neustart nichts doppelt (hochgeladen.txt)")
        z = daten.zaehler()
        check(z["offen"] == 4 and z["offen_je"] == [4, 0, 0], "alle 4 Bilder offen, auch das am Tisch festgelegte")

        print("Anmeldung")
        anna, ben, carl = Client(base), Client(base), Client(base)
        check(anna.call("GET", "/api/next")[0] == 401, "ohne Code kein Bild")
        for pfad, merkmal in (("/", b"loginForm"), ("/ui.css", b"--tint"), ("/notify.js", b"Notify")):
            st, roh = Client(base).call("GET", pfad)
            check(st == 200 and merkmal in roh, pfad + " wird ohne Anmeldung ausgeliefert (Seite, Aussehen, Meldungen)")
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
        carl_p = next(p for p in st["personen"] if p["name"] == "Carl")
        admin.call("POST", "/api/admin/person/loeschen", {"id": carl_p["id"]})
        admin.call("POST", "/api/admin/person", {"name": "Dora"})
        dora = next(p for p in admin.call("GET", "/api/admin/stand")[1]["personen"] if p["name"] == "Dora")
        check(dora["id"] != carl_p["id"] and dora["festgelegt"] == 0 and dora["verworfen"] == 0,
              "neuer Trainer erbt nichts vom gelöschten (eigene Nummer, Zähler 0)")

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
        server.EXPORT_KEY = ""
        check(Client(base).call("GET", "/api/export.zip", headers={"X-Export-Key": ""})[0] == 403,
              "Export ohne EXPORT_KEY auf dem Server: aus")
        server.EXPORT_KEY = "export-schluessel-456"
        check(Client(base).call("GET", "/api/export.zip", headers={"X-Export-Key": "falsch"})[0] == 403,
              "Export mit falschem Schlüssel: abgelehnt")
        s, raw = Client(base).call("GET", "/api/export.zip", headers={"X-Export-Key": "export-schluessel-456"})
        check(s == 200 and sum(n.startswith("images/") for n in zipfile.ZipFile(io.BytesIO(raw)).namelist()) == 1,
              "Export mit Schlüssel (für Colab): dieselbe ZIP ohne greylo-Login")

        print("Einzelkugel: Bild per Knopf am Pi, die Stelle findet der Server - ohne Menschen")
        import numpy as np
        import cv2
        rng = np.random.default_rng(3)
        tuch = np.zeros((400, 800, 3), np.uint8); tuch[:] = (150, 70, 30)            # BGR: blaues Tuch
        leer = np.clip(tuch + rng.normal(0, 3, tuch.shape), 0, 255).astype(np.uint8)
        ok, leer_jpg = cv2.imencode(".jpg", leer)
        rec2 = Recorder(os.path.join(d, "pi2"))
        bilder = {}
        # (x, y, Hand, Helligkeit, Kugel da?) - Hand daneben und andere Belichtung stoeren nicht mehr
        faelle = [(200, 150, False, 1.0, True), (610, 320, False, 1.15, True), (400, 200, True, 1.0, True),
                  (500, 100, True, 0.9, False)]
        for i, (x, y, hand, hell, da) in enumerate(faelle):
            img = np.clip(tuch * hell + rng.normal(0, 3, tuch.shape), 0, 255).astype(np.uint8)
            if da:
                cv2.circle(img, (x, y), 10, (40, 40, 210), -1)                       # rote Kugel (3)
            if hand:
                cv2.rectangle(img, (300, 0), (380, 260), (120, 160, 210), -1)
            bilder[rec2.save_single(img, 3, 10.2, leer_jpg.tobytes())] = (x, y, hand, da)
            time.sleep(0.002)
        check(rec2.single == 4 and len([f for f in os.listdir(rec2.single_dir) if f.startswith("leer-")]) == 1,
              "Pi: 4 Knopf-Bilder, leerer Tisch nur einmal abgelegt")
        vorher = daten.zaehler()
        up2 = Uploader(rec2, base, "test-schluessel-123")
        check(up2.run_once() == 4, "4 Einzelkugel-Bilder hochgeladen (mit Bild des leeren Tisches)")
        z = daten.zaehler()
        check(z["einzel"] == 4 and z["einzel_erkannt"] == 3 and z["einzel_aussortiert"] == 1,
              f"Server: 4 Einzelkugel-Bilder, 3 erkannt ({z['einzel_erkannt']}), 1 aussortiert")
        check(z["fertig"] == vorher["fertig"] + 3 and z["offen"] == vorher["offen"]
              and z["verworfen"] == vorher["verworfen"] + 1,
              "erkannte sind sofort fertig, ohne Kugel von selbst aussortiert - nichts wartet auf Trainer")
        check(z["festlegungen"] == vorher["festlegungen"], "Bilderkennung zählt nicht als Trainer-Festlegung")
        for name, (x, y, hand, da) in bilder.items():
            fl = daten.festlegungen(name)
            if not da:
                check(not fl, "ohne Kugel (nur Hand): keine Festlegung, aussortiert")
                continue
            k = json.loads(fl[0]["kugeln"])[0] if fl else {}
            check(fl and fl[0]["person_name"] == "Bilderkennung" and k.get("number") == 3 and k.get("kind") == "solid"
                  and k.get("color") == "rot" and abs(k["x"] - x) < 2 and abs(k["y"] - y) < 2,
                  f"Kugel 3 bei ({x}, {y}) gefunden: {k.get('x')}, {k.get('y')}")
        ohne = next(n for n, v in bilder.items() if not v[3])
        eintrag = next(b for b in admin.call("GET", "/api/admin/stand")[1]["letzte"] if b["name"] == ohne)
        check(eintrag["status"] == "verworfen" and eintrag["verworfen_von"] == "Bilderkennung"
              and eintrag["erkennung"] == "keine Kugel gefunden", "Admin sieht: von der Bilderkennung aussortiert, mit Grund")
        s, msg = admin.call("POST", "/api/admin/zurueck", {"name": ohne})
        check(s == 200 and "nicht sicher erkannt" in msg["message"] and daten.zaehler()["offen"] == z["offen"],
              "„Nochmal erkennen“: bleibt aussortiert, geht nicht an Trainer")
        erster = sorted(bilder)[0]
        s1, roh = admin.call("GET", "/api/admin/bild?name=" + erster)
        s2, kreis = admin.call("GET", "/api/admin/bild?kreis=1&name=" + erster)
        check(s1 == 200 and s2 == 200 and kreis[:2] == b"\xff\xd8" and kreis != roh, "Admin-Vorschau mit Kreis")
        eintrag = next(b for b in admin.call("GET", "/api/admin/stand")[1]["letzte"] if b["name"] == erster)
        check(eintrag["einzel"] == 3 and eintrag["erkennung"] == "gefunden" and eintrag["wer"] == ["Bilderkennung"],
              "Admin-Liste zeigt Einzelkugel + Bilderkennung")
        s, raw = admin.call("GET", "/api/admin/zip")
        zz = zipfile.ZipFile(io.BytesIO(raw))
        check(f"images/{erster}.jpg" in zz.namelist() and zz.read(f"labels/{erster}.txt").decode().startswith("3 0.25"),
              "erkannte Einzelkugel-Bilder sind in der Trainings-ZIP (Klasse 3)")
        alt_name = "20261001-090000-000"
        img = np.clip(tuch + rng.normal(0, 3, tuch.shape), 0, 255).astype(np.uint8)
        cv2.circle(img, (321, 123), 10, (40, 40, 210), -1)
        with open(os.path.join(daten.bilder, alt_name + ".jpg"), "wb") as f:
            f.write(cv2.imencode(".jpg", img)[1].tobytes())
        daten.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status, einzel,
                            erkennung) VALUES (?, 1, 800, 400, 10.2, 'Einzelkugel', '[]', 'offen', 3, 'unsicher')""", (alt_name,))
        daten.db.commit()
        check(all(daten.naechstes(p["id"]) is None or daten.naechstes(p["id"])["name"] != alt_name
                  for p in daten.personen()), "offene Einzelkugel-Bilder gehen nie an Trainer")
        check(daten.einzel_nachholen() == (1, 0), "altes offenes Einzelkugel-Bild nachgeholt (leerer Tisch passend gesucht)")
        k = json.loads(daten.festlegungen(alt_name)[0]["kugeln"])[0]
        check(abs(k["x"] - 321) < 2 and abs(k["y"] - 123) < 2 and k["number"] == 3, "… und an der richtigen Stelle")
        nicht = Client(base).call("POST", "/api/upload", {"name": "20261113-130000-000"})
        check(nicht[0] == 403, "Upload ohne Schlüssel weiter gesperrt")

        print("Modelle für den Pi")
        def roh(pfad, data, name):
            req = urllib.request.Request(base + pfad, data=data, method="POST", headers={
                "Remote-User": "nicolas", "Remote-Groups": "admins", "Content-Type": "application/octet-stream",
                "X-Dateiname": name})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")
        pi = Client(base, {"X-Upload-Key": "test-schluessel-123"})
        check(Client(base).call("GET", "/api/modelle")[0] == 403, "Modell-Liste nur mit Schlüssel des Pi")
        check(pi.call("GET", "/api/modelle") == (200, {"dateien": []}), "am Anfang keine Modelle")
        with open(os.path.join(HIER, "start_merkmale.json"), "rb") as f:
            merkmale = f.read()
        st, res = roh("/api/admin/modell", merkmale, "billard_merkmale.json")
        check(st == 200 and "billard_merkmale.json" in res["message"], f"Farben hochgeladen ({st} {res})")
        klassen = b"\0\0\0\0TFL3" + b"x" * 5000 + b"edgetpu-custom-op"
        st, res = roh("/api/admin/modell", klassen, "irgendwas.tflite")
        check(st == 200 and "billard_klassen_edgetpu.tflite" in res["message"], "kleines Modell = Klassen-Modell")
        check(roh("/api/admin/modell", b"kein modell" * 200, "x.tflite")[0] == 400, "keine Modelldatei abgelehnt")
        check(roh("/api/admin/modell", b"{\"falsch\": 1}", "billard_merkmale.json")[0] == 400, "falsche JSON abgelehnt")
        st, res = Client(base).call("POST", "/api/admin/lernen", {})
        check(st == 403, "Lernen nur für Admins")
        liste = pi.call("GET", "/api/modelle")[1]["dateien"]
        check(sorted(f["name"] for f in liste) == ["billard_klassen_edgetpu.tflite", "billard_merkmale.json"]
              and all(len(f["sha256"]) == 64 for f in liste), "Pi sieht beide Dateien mit Prüfsumme")
        bekommen = {}

        def install(name, data):
            if name == "billard_klassen_edgetpu.tflite" and "kaputt" in bekommen:
                raise ValueError("unbrauchbar")
            bekommen[name] = data
            return "ok"
        zustand = os.path.join(d, "pi", "vom-vps.json")
        sync = Uploader(rec, base, "test-schluessel-123", install=install, state_path=zustand)
        check(sorted(sync.sync_models()) == ["billard_klassen_edgetpu.tflite", "billard_merkmale.json"]
              and bekommen["billard_merkmale.json"] == merkmale, "Pi holt neue Modelle und übernimmt sie")
        check(sync.sync_models() == [], "unveränderte Modelle werden nicht nochmal geladen")
        check(Uploader(rec, base, "test-schluessel-123", install=install, state_path=zustand).sync_models() == [],
              "auch nach Neustart nicht (Prüfsummen gemerkt)")
        abgeholt = {f["name"]: f["abgeholt"] for f in admin.call("GET", "/api/admin/stand")[1]["modelle"]["dateien"]}
        check(all(abgeholt.values()), "Admin sieht, dass der Pi die Dateien abgeholt hat")
        bekommen["kaputt"] = True
        roh("/api/admin/modell", klassen + b"neu", "billard_klassen.tflite")
        check(sync.sync_models() == [] and sync.models["billard_klassen_edgetpu.tflite"]["ok"] is False,
              "unbrauchbare Datei: Pi behält seine, merkt sich den Fehler")
        check(sync.sync_models() == [], "... und lädt sie nicht immer wieder")
        st, res = admin.call("POST", "/api/admin/modell/loeschen", {"name": "billard_klassen_edgetpu.tflite"})
        check(st == 200 and [f["name"] for f in pi.call("GET", "/api/modelle")[1]["dateien"]] == ["billard_merkmale.json"],
              "Admin entfernt ein Modell")

        print("Farben lernen auf dem Server")
        st, res = admin.call("POST", "/api/admin/lernen", {})
        while mod.lernen_laeuft:
            time.sleep(0.05)
        vorher = pi.call("GET", "/api/modelle")[1]["dateien"][0]["sha256"]
        check(st == 200 and mod.lernen_ergebnis["ok"] is False and "Zu wenig" in mod.lernen_ergebnis["text"]
              and pi.call("GET", "/api/modelle")[1]["dateien"][0]["sha256"] == vorher,
              "zu wenig festgelegte Kugeln: nichts veröffentlicht, bisherige Farben bleiben")
        from sim.make_start_merkmale import sim_samples
        import cv2
        beispiele = []
        for i, (img, kugeln, radius) in enumerate(sim_samples(12, 5, jitter=1.0)):
            pfad = os.path.join(d, f"sim{i}.jpg")
            cv2.imwrite(pfad, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            beispiele.append((pfad, kugeln, radius))
        alt_sha = pi.call("GET", "/api/modelle")[1]["dateien"][0]["sha256"]
        mod.lernen(lambda: beispiele, len(beispiele), warten=True)
        e = mod.lernen_ergebnis
        neu = pi.call("GET", "/api/modelle")[1]["dateien"][0]
        check(e["ok"] and e["kugeln"] > 100 and neu["sha256"] != alt_sha, f"Farben aus Bildern gelernt ({e['text'][:60]})")
        from kugel_merkmale import BallClassifier
        with open(mod.pfad("billard_merkmale.json")) as f:
            clf = BallClassifier.from_dict(json.load(f))
        check(clf.info.get("quelle", "").startswith("VPS"), "gelernte Datei ist gültig, Quelle VPS")
        check(sync.sync_models() == ["billard_merkmale.json"], "Pi holt die neu gelernten Farben")

        print("Spielaufzeichnung: Video baut der Server")
        from replay import RoundRecorder
        g = {"phase": "playing", "teams": ["Rot", "Blau"], "frames_won": [0, 0], "turn": 0, "groups": [None, None],
             "group_names": ["offen", "offen"], "remaining": {"solid": 7, "stripe": 7}, "eight_on_table": True,
             "breaking": False, "ball_in_hand": False, "winner": None, "message": "Rot ist dran", "log": [],
             "can_undo": False, "can_undo_shot": False}
        kugeln = [{"x": 100.0, "y": 100.0, "kind": "cue", "color": "", "number": 0},
                  {"x": 300.0, "y": 200.0, "kind": "solid", "color": "rot", "number": 3}]
        def szene(balls):
            return {"balls": balls, "radius": 10.0, "game": g, "bar": {}, "suggestion": None, "cue": None,
                    "status": "", "info": "", "qr": None, "notices": []}
        rounds = RoundRecorder(os.path.join(d, "pi", "recordings"), local_build=lambda: False)
        rounds.start(szene(kugeln))
        rounds.add(szene([dict(kugeln[0], x=250.0), kugeln[1]]))
        rounds.stop(szene([dict(kugeln[0], x=250.0)]))
        check(len(rounds.finished()) == 1, "Pi: Rack fertig aufgezeichnet (nur Daten)")
        check(Client(base).call("POST", "/api/aufzeichnung", {"name": "runde-20261119-100000"})[0] == 403,
              "Aufzeichnung nur mit Schlüssel des Pi")
        check(Client(base).call("GET", "/api/aufzeichnungen")[0] == 403, "Videoliste nur mit Schlüssel des Pi")
        check(pi.call("POST", "/api/aufzeichnung", {"name": "runde-20261119-100000", "schritte": [{}]})[0] == 400,
              "kaputte Aufzeichnung abgelehnt")
        vu = Uploader(rec, base, "test-schluessel-123", rounds=rounds)
        check(vu.upload_recordings() == 1 and rounds.finished() == [], "Pi schickt das Rack und löscht es danach")
        for _ in range(300):
            liste = pi.call("GET", "/api/aufzeichnungen")[1]["videos"]
            if liste and liste[0]["status"] in ("fertig", "fehler"):
                break
            time.sleep(0.1)
        v = liste[0] if liste else {}
        check(v.get("status") == "fertig" and v.get("teams") == ["Rot", "Blau"] and v.get("url", "").startswith("/video/"),
              f"Server hat das Video gebaut ({v.get('status')} {v.get('fehler') or ''})")
        st, roh_video = Client(base).call("GET", v.get("url") or "/video/x.mp4")
        check(st == 200 and isinstance(roh_video, bytes) and len(roh_video) > 1000, "Video unter der geheimen Adresse")
        req = urllib.request.Request(base + (v.get("url") or ""), headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req) as r:
            check(r.status == 206 and len(r.read()) == 100 and r.headers["Content-Range"].endswith(f"/{len(roh_video)}"),
                  "Teil-Abruf (Range) wie ihn Safari braucht")
        check(Client(base).call("GET", "/video/" + "a" * 24 + ".mp4")[0] == 404, "erfundene Adresse: nichts")
        vu.fetch_videos()
        check(vu.videos() and vu.videos()[0]["url"] == base + v.get("url", "") and vu.videos()[0]["status"] == "fertig",
              "Pi kennt das Video mit Link direkt zum Server")
        check(pi.call("POST", "/api/aufzeichnung", json.load(open(os.path.join(auf.dir, v["name"] + ".json"))))[1].get("neu")
              is False, "dasselbe Rack zweimal: nur einmal")
        stand = admin.call("GET", "/api/admin/stand")[1]["videos"]
        check(stand["anzahl"].get("fertig") == 1 and stand["liste"][0]["url"], "Admin sieht die Aufzeichnung")
        aufraeumen = server.aufzeichnung.Aufzeichnungen(os.path.join(d, "auf2"), bauen=lambda s_, p_: open(p_, "wb").write(b"x"))
        server.aufzeichnung.MAX_VIDEOS, alt_max = 2, server.aufzeichnung.MAX_VIDEOS
        for i in range(4):
            aufraeumen.eingang({"name": f"runde-20261119-10000{i}", "start": 1000 + i,
                                "schritte": [szene(kugeln), szene(kugeln)]})
        for _ in range(100):
            if all(e["status"] == "fertig" for e in aufraeumen.index.values()) and len(aufraeumen.index) == 2:
                break
            time.sleep(0.05)
        server.aufzeichnung.MAX_VIDEOS = alt_max
        check(sorted(aufraeumen.index) == ["runde-20261119-100002", "runde-20261119-100003"],
              "nur die neuesten Videos bleiben")

        print("Einzelkugel-Erkennung an Simulator-Bildern (Schatten, Glanz, Licht, Arm im Bild)")
        app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
        if os.path.isdir(os.path.join(app_dir, "sim")):
            sys.path.append(app_dir)
            from geometry import TABLE_SIZES, WARP_H, WARP_W, Table
            from sim.render import DEFAULT_CORNERS, SimBall, render_topdown, to_camera
            import einzelkugel
            lang = TABLE_SIZES["8ft"]
            tisch = Table(DEFAULT_CORNERS, lang)
            rs = np.random.default_rng(5)

            def sim_bild(kugeln, tuch_farbe, arm=None, hell=1.0):
                w = tisch.warp(to_camera(render_topdown(kugeln, lang, lang / 2, tuch_farbe, rs, arm), DEFAULT_CORNERS, rng=rs))
                return cv2.imencode(".jpg", np.clip(w * hell, 0, 255).astype(np.uint8))[1].tobytes()
            treffer, n_sim = 0, 0
            for tuch_farbe in ("blau", "gruen"):
                leer_sim = sim_bild([], tuch_farbe)
                for nr in range(16):
                    x, y = rs.uniform(0.1, lang - 0.1), rs.uniform(0.1, lang / 2 - 0.1)
                    arm = ((x + 0.6) % lang, rs.uniform(0, lang / 2), 0.12) if nr % 2 else None
                    kug, _ = einzelkugel.finde_kugel(
                        sim_bild([SimBall(nr, x, y, int(rs.integers(1 << 30)))], tuch_farbe, arm, rs.uniform(0.85, 1.15)),
                        leer_sim, tisch.ball_radius_px, nr)
                    n_sim += 1
                    treffer += len(kug) == 1 and math.hypot(kug[0][0] - x / lang * WARP_W, kug[0][1] - y / (lang / 2) * WARP_H) < 4
            check(treffer == n_sim, f"Simulator: {treffer} von {n_sim} Einzelkugeln richtig gefunden (alle Nummern, beide Tücher)")

        print("Umstellung alter Daten")
        alt = server.Daten(os.path.join(d, "alt"))
        alt.db.execute("INSERT INTO bilder (name, eingang, breite, hoehe, radius, vorschlag, status) VALUES ('20261101-100000-000', 1, 800, 400, 10, '[]', 'runde2')")
        alt.db.commit()
        alt2 = server.Daten(os.path.join(d, "alt"))
        check(alt2.zaehler()["offen"] == 1, "alte Runde-2-Bilder werden beim Start zu offen")
        # alte Version: geloeschter Trainer 1 hatte festgelegt, der neue bekam wieder Nummer 1
        alt2.db.execute("INSERT INTO festlegungen (bild, person, person_name, kugeln, zeit) VALUES ('20261101-100000-000', 1, 'Weg', '[]', 5)")
        alt2.db.execute("INSERT INTO personen (id, name, code, erstellt) VALUES (1, 'PC-01', '105098', 100)")
        alt2.db.commit()
        alt3 = server.Daten(os.path.join(d, "alt"))
        pc = alt3.personen()[0]
        check(pc["id"] != 1 and pc["festgelegt"] == 0 and pc["code"] == "105098",
              "Reparatur: geerbte Festlegungen gehören nicht mehr zum neuen Trainer (Code bleibt)")
        check(alt3.naechstes(pc["id"]) is not None, "… und er bekommt das Bild, das der gelöschte schon hatte")
        srv.shutdown()

    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("Alles OK")


if __name__ == "__main__":
    main()
