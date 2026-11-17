"""
Tests fuer Stufe 2 + 3 der Kugelerkennung (kugel_merkmale.py): gestreift? Farbe?

  * Nummer <-> Art + Farbe passt zu ballnum.py
  * Lernen aus Simulator-Bildern, Pruefen auf anderen Bildern (auch wenn Stufe 1 die Kugel
    ein paar Pixel daneben findet)
  * Speichern/Laden, falsche Dateien werden abgelehnt, neue Datei wird von selbst geladen
  * Lernen aus den festgelegten Bildern auf dem Pi (Recorder)
  * Klassen mit zu wenig Beispielen behalten die Startwerte

    python3 -m sim.test_merkmale
"""

import json
import os
import sys
import tempfile

import numpy as np

import kugel_merkmale as km
from ballnum import Ball, color_of_number, kind_of_number
from coral_detector import START_FEATURES
from recorder import Recorder
from sim.make_start_merkmale import sim_samples

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def test_numbers():
    print("Nummer <-> Art + Farbe")
    ok = all(km.split_number(n) == (kind_of_number(n), color_of_number(n) or None if n != 8 else None)
             for n in range(16))
    check(ok and all(km.number_of(*km.split_number(n)) == n for n in range(16)), "passt zu ballnum.py")


def test_learn():
    print("Lernen und Pruefen (Simulator)")
    start = km.BallClassifier.load(START_FEATURES)
    check(start.info.get("quelle") == "Simulator", "Startwerte vorhanden (models/start_merkmale.json)")
    clf, res = km.learn(sim_samples(40, 5, jitter=1.0), fallback=start, source="Test")
    check(res is not None and res["art"] >= 0.9 and res["farbe"] >= 0.95,
          f"zurueckgehaltene Kugeln: Art {res['art']:.2f}, Farbe {res['farbe']:.2f}")
    test = km.evaluate(clf, km.collect(sim_samples(15, 77, jitter=2.0)))
    check(test["nummer"] >= 0.9, f"neue Bilder, Kugel 2 px daneben: Nummer {test['nummer']:.2f} richtig")
    check(clf.info["kugeln"] > 400 and clf.info["pruefung"]["kugeln"] > 10, "Info: Anzahl und Pruefung")

    # Einzelne Kugel: Wahrscheinlichkeiten fuer alle 16 Nummern
    img, balls, r = next(sim_samples(1, 8))
    x, y, n = balls[0]
    s, pk, pc = clf.classify(img, x, y, r)
    check(s.shape == (16,) and abs(s.sum() - 1) < 1e-6 and int(np.argmax(s)) == n,
          f"16 Wahrscheinlichkeiten, beste = Kugel {n}")

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "billard_merkmale.json")
        clf.save(p)
        again = km.BallClassifier.load(p)
        check(np.allclose(again.classify(img, x, y, r)[0], s), "speichern und laden ergibt dasselbe")
        for bad, why in (({"format": "x"}, "fremde Datei"),
                         (dict(clf.to_dict(), art=dict(clf.to_dict()["art"], classes=["a", "b"])), "andere Klassen")):
            try:
                km.BallClassifier.from_dict(json.loads(json.dumps(bad)))
                check(False, f"{why} abgelehnt")
            except (ValueError, KeyError):
                check(True, f"{why} abgelehnt")

        # Datei wird von selbst (neu) geladen
        t = [0.0]
        store = km.ClassifierStore((p, START_FEATURES), clock=lambda: t[0])
        check(store.get() is not None and store.status(START_FEATURES)["own"], "eigene Datei hat Vorrang")
        os.remove(p)
        t[0] += 5
        check(store.get() is not None and not store.status(START_FEATURES)["own"], "geloescht -> Startwerte")


def test_inherit():
    print("Zu wenig Beispiele")
    start = km.BallClassifier.load(START_FEATURES)
    data = km.collect(sim_samples(10, 21))
    keep = [i for i, n in enumerate(data[4]) if n != 15]          # Kugel 15 fehlt ganz
    sub = tuple([part[i] for i in keep] for part in data)
    clf = km.fit(sub, fallback=start)
    check("weinrot" not in clf.info["geerbt"], "weinrot gibt es noch als Volle (7) -> gelernt")
    sub2 = tuple([part[i] for i in keep if data[4][i] not in (7, 15)] for part in data)
    clf2 = km.fit(sub2, fallback=start)
    check(clf2.info["geerbt"] == ["weinrot"], f"weinrot fehlt ganz -> Startwerte behalten ({clf2.info['geerbt']})")
    try:
        km.fit(sub2)
        check(False, "ohne Startwerte: Fehlermeldung")
    except ValueError as e:
        check("weinrot" in str(e), "ohne Startwerte: Fehlermeldung nennt die Farbe")


def test_from_recorder():
    print("Lernen aus festgelegten Bildern auf dem Pi")
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d)
        for img, balls, r in sim_samples(25, 31):
            rec.save_verified(img, [Ball.from_number(x, y, r, n) for x, y, n in balls], r, [n for _, _, n in balls])
        got = list(rec.verified_samples())
        check(len(got) == 25 and all(len(b) == 12 for _, b, _ in got), "25 Bilder mit je 12 Kugeln gelesen")
        clf, res = km.learn(rec.verified_samples(), fallback=km.BallClassifier.load(START_FEATURES))
        check(res["nummer"] >= 0.85, f"aus JPEGs gelernt: Nummer {res['nummer']:.2f} richtig")


if __name__ == "__main__":
    test_numbers()
    test_learn()
    test_inherit()
    test_from_recorder()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
