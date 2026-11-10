"""
Tests fuer Pruefrunden (mehrere Pruefer pro Bild), Anmeldung und Ding-Ton.

    python3 -m sim.test_review
"""

import os
import sys
import tempfile
import wave

import numpy as np

from auth import Auth, LoginError
from ballnum import Ball
from recorder import Recorder
from review_rounds import RoundManager, compare_ai, consensus
from sound import make_ding

FAILS = []
R = 10.0


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def b(x, y, n):
    kinds = {0: ("cue", ""), 8: ("eight", "schwarz")}
    k, c = kinds.get(n, ("solid" if n < 8 else "stripe", ["gelb", "blau", "rot", "violett", "orange", "gruen", "weinrot"][(n if n < 8 else n - 8) - 1]))
    return {"x": x, "y": y, "kind": k, "color": c}


def test_consensus():
    print("Mehrheit")
    subs = {"Anna": [b(100, 100, 0), b(300, 200, 3), b(500, 300, 11)],
            "Ben": [b(102, 99, 0), b(301, 203, 3), b(498, 301, 11)],
            "Clara": [b(99, 101, 0), b(300, 201, 3), b(501, 299, 11)]}
    final, disputed, agree = consensus(subs, R)
    check(len(final) == 3 and not disputed and agree == 1.0, "alle einig -> 3 Kugeln, 100 %")
    cue = next(f for f in final if f["number"] == 0)
    check(abs(cue["x"] - 100.3) < 0.1 and abs(cue["y"] - 100.0) < 0.1, "Position = Mittelwert")

    subs["Clara"][1] = b(300, 201, 11)              # Clara liest 11 statt 3
    final, disputed, agree = consensus(subs, R)
    check(any(f["number"] == 3 for f in final) and not disputed, "2 gegen 1: Mehrheit (3) gewinnt")
    check(round(agree, 2) == 0.67, "Einigkeit sinkt (2 von 3 Kugeln einstimmig)")

    subs["Clara"].append(b(700, 50, 5))            # nur Clara sieht eine Kugel
    final, disputed, _ = consensus(subs, R)
    check(not any(f["number"] == 5 for f in final) and not disputed, "Minderheit sieht Kugel -> keine Kugel")

    two = {"Anna": [b(100, 100, 3)], "Ben": [b(101, 100, 11)]}
    final, disputed, _ = consensus(two, R)
    check(not final and len(disputed) == 1 and "Mehrheit" in disputed[0]["why"], "2 Pruefer uneinig -> strittig")
    two = {"Anna": [b(100, 100, 3)], "Ben": []}
    final, disputed, _ = consensus(two, R)
    check(not final and len(disputed) == 1 and "Hälfte" in disputed[0]["why"], "nur die Haelfte sieht Kugel -> strittig")
    final, disputed, agree = consensus({"Anna": [b(10, 10, 1)]}, R)
    check(len(final) == 1 and not disputed, "ein einzelner Pruefer -> sein Ergebnis")


class FakeSound:
    def __init__(self):
        self.n = 0

    def ding(self, wait=False):
        self.n += 1
        return ""


def test_rounds():
    print("Pruefrunde")
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(os.path.join(d, "dataset"))
        snd = FakeSound()
        t = [1000.0]
        rm = RoundManager(rec, d, snd, clock=lambda: t[0])
        img = np.zeros((400, 800, 3), np.uint8)
        rm.heartbeat("Anna")
        rm.heartbeat("Ben")
        check(rm.online(["Anna", "Ben", "Clara"]) == ["Anna", "Ben"], "online = zuletzt gesehen")
        t[0] += 30
        check(rm.online(["Anna", "Ben"]) == [], "nach 30 s ohne Lebenszeichen offline")
        try:
            rm.start(img, [], R, None, [])
            check(False, "ohne Pruefer keine Runde")
        except ValueError:
            check(True, "ohne Pruefer keine Runde")

        # reiner Pruefer-Ablauf (Vorschlag sichtbar, aber ohne KI-Modell -> kein KI-Abgleich)
        r = rm.start(img, [Ball.from_number(100, 100, R, 0)], R, None, ["Anna", "Ben"], show_ai=True)
        v = rm.view("Anna")["round"]
        check(v["id"] == r["id"] and len(v["balls"]) == 1 and not v["submitted"], "Pruefer bekommt Bild + Vorschlag")
        check(rm.view("Clara")["round"] is None, "wer nicht dabei ist, sieht nichts")
        try:
            rm.submit("Anna", r["id"], [{"x": 1, "y": 1, "kind": "solid", "color": ""}])
            check(False, "ohne Nummer abgelehnt")
        except ValueError:
            check(True, "ohne Nummer abgelehnt")
        res = rm.submit("Anna", r["id"], [b(100, 100, 0), b(300, 200, 3)])
        check(res is None and rm.status()["done"] == 1 and snd.n == 0, "1 von 2: noch kein Ding")
        res = rm.submit("Ben", r["id"], [b(101, 101, 0), b(301, 200, 3)])
        check(res is not None and res["ok"] and snd.n == 1, "alle gespeichert -> fertig + Ding")
        check(rec.verified == 1 and rec.pending == 0, "einig -> direkt in den Trainingsdaten")
        check(not rm.status()["active"] and rm.view("Anna")["last"]["ok"], "Runde zu, Ergebnis sichtbar")

        r2 = rm.start(img, [], R, None, ["Anna", "Ben"])
        rm.submit("Anna", r2["id"], [b(100, 100, 3)])
        rm.submit("Ben", r2["id"], [b(100, 100, 11)])
        check(rec.pending == 1 and rec.verified == 1, "uneinig -> bei den offenen Bildern (Admin entscheidet)")
        item = rec.next_pending()
        check(item and item["disputed"] and any(x["number"] is None for x in item["balls"]),
              "strittige Kugel ist mit ? markiert")
        try:
            rm.submit("Anna", r2["id"], [])
            check(False, "alte Runde: Speichern abgelehnt")
        except ValueError:
            check(True, "alte Runde: Speichern abgelehnt")

        r3 = rm.start(img, [], R, None, ["Anna", "Ben", "Clara"])
        rm.submit("Anna", r3["id"], [b(50, 50, 1)])
        res = rm.finish()
        check(res["names"] == ["Anna"] and rec.verified == 2, "Admin schliesst mit den bisherigen ab")
        r4 = rm.start(img, [], R, None, ["Anna"])
        rm.cancel()
        check(not rm.status()["active"] and rm.count == 4, "abbrechen, Zaehler laeuft weiter")


def test_ai_compare():
    print("KI-Abgleich")
    final = [{"x": 100, "y": 100, "number": 0}, {"x": 300, "y": 200, "number": 3}, {"x": 500, "y": 300, "number": 11}]
    sugg = [b(101, 100, 0), b(300, 201, 3), b(500, 300, 11)]
    diffs, ok, total = compare_ai(final, sugg, R)
    check(not diffs and ok == 3 and total == 3, "KI ueberall richtig -> kein ?")
    sugg = [b(101, 100, 0), b(300, 201, 4), b(700, 50, 5)]       # 3 als 4 gelesen, 11 uebersehen, 5 erfunden
    diffs, ok, total = compare_ai(final, sugg, R)
    notes = sorted(d["note"] for d in diffs)
    check(ok == 1 and total == 4 and len(diffs) == 3, f"3 Abweichungen erkannt ({ok}/{total} richtig)")
    check(notes == ["KI: 4 · Prüfer: 3", "KI: 5 · Prüfer: keine Kugel", "KI: übersehen · Prüfer: 11"],
          "Notizen: falsche Nummer, uebersehen, erfunden")

    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(os.path.join(d, "dataset"))
        rm = RoundManager(rec, d, FakeSound())
        img = np.zeros((400, 800, 3), np.uint8)
        ai = [Ball.from_number(100, 100, R, 0), Ball.from_number(300, 200, R, 3)]
        r = rm.start(img, ai, R, "eigenes Modell vom 01.12.2026", ["Anna", "Ben"])
        v = rm.view("Anna")["round"]
        check(v["balls"] == [] and v["blind"] and v["model"] is None, "Pruefer sehen den KI-Vorschlag nicht (Standard)")
        rm.submit("Anna", r["id"], [b(100, 100, 0), b(300, 200, 3)])
        res = rm.submit("Ben", r["id"], [b(101, 99, 0), b(299, 201, 3)])
        check(res["ok"] and res["ai"] == {"correct": 2, "total": 2, "wrong": 0} and rec.verified == 1,
              "KI richtig + Pruefer einig -> direkt in die Trainingsdaten")
        q = rec.model_quality()
        check(q and q["percent"] == 100, "Trefferquote zaehlt gegen das Pruefer-Ergebnis")

        r = rm.start(img, ai, R, "eigenes Modell vom 01.12.2026", ["Anna", "Ben"])
        rm.submit("Anna", r["id"], [b(100, 100, 0), b(300, 200, 11)])     # die KI sagte 3
        res = rm.submit("Ben", r["id"], [b(100, 100, 0), b(300, 200, 11)])
        item = rec.next_pending()
        qs = [x for x in item["balls"] if x["number"] is None]
        check(not res["ok"] and res["ai"]["wrong"] == 1 and len(qs) == 1 and qs[0]["note"] == "KI: 3 · Prüfer: 11",
              "KI falsch -> ? mit Notiz beim Admin")
        check(len(item["balls"]) == 2 and rec.verified == 1, "die richtige Kugel bleibt, die falsche ist ?")
        check(rec.model_quality()["percent"] == 75, "Trefferquote sinkt (3 von 4)")
        fixed = [dict(x, number=11, kind="stripe", color="rot") if x["number"] is None else x for x in item["balls"]]
        rec.review(item["name"], fixed)
        import json as _j
        meta = _j.load(open(os.path.join(rec.ok_lbl, item["name"] + ".json")))
        check(meta.get("ai", {}).get("wrong") == 1 and set(meta.get("reviewers", {})) == {"Anna", "Ben"},
              "Admin legt fest: KI-Abgleich und Einzelergebnisse bleiben im Label")
        check(rec.model_quality()["percent"] == 75, "Admin-Entscheidung zaehlt nicht doppelt in die Trefferquote")

        r = rm.start(img, ai, R, "eigenes Modell vom 01.12.2026", ["Anna"], show_ai=True)
        check(len(rm.view("Anna")["round"]["balls"]) == 2, "umschaltbar: Pruefer sehen den Vorschlag")


def test_auth():
    print("Anmeldung")
    with tempfile.TemporaryDirectory() as d:
        revs = [{"name": "Anna", "code": "4821"}]
        a = Auth("1902", d, lambda: revs)
        sess, tok = a.login("1902", "ip1")
        check(sess["role"] == "admin", "Admin-PIN 1902")
        check(a.who(f"billard={tok}")["role"] == "admin", "Cookie -> Admin")
        sess, tok2 = a.login("4821", "ip1")
        check(sess == {"role": "pruefer", "name": "Anna"}, "Pruefer-Code -> Anna")
        check(a.who(f"x=1; billard={tok2}")["name"] == "Anna", "Cookie unter mehreren")
        check(a.who("")["role"] is None and a.who("", "1902")["role"] == "admin", "ohne Anmeldung nichts, PIN-Kopfzeile geht")
        a2 = Auth("1902", d, lambda: revs)
        check(a2.who(f"billard={tok2}")["name"] == "Anna", "Sitzung ueberlebt Neustart")
        revs.clear()
        check(a.who(f"billard={tok2}")["role"] is None, "entfernter Pruefer ist abgemeldet")
        for _ in range(5):
            try:
                a.login("0000", "ip9")
            except LoginError:
                pass
        try:
            a.login("1902", "ip9")
            check(False, "nach 5 Fehlversuchen kurz gesperrt")
        except LoginError as e:
            check("Fehlversuche" in str(e), "nach 5 Fehlversuchen kurz gesperrt")
        check(Auth("", d, lambda: []).who("")["role"] == "admin", "ohne PIN offen wie frueher")


def test_sound():
    print("Ding")
    with tempfile.TemporaryDirectory() as d:
        p = make_ding(os.path.join(d, "ding.wav"))
        with wave.open(p) as w:
            ok = w.getnchannels() == 2 and w.getframerate() == 48000 and 1.0 < w.getnframes() / 48000 < 1.5
        check(ok, "Ding-Ton erzeugt (Stereo, 48 kHz, ~1,2 s)")


if __name__ == "__main__":
    test_consensus()
    test_rounds()
    test_ai_compare()
    test_auth()
    test_sound()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
