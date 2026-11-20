"""
Tests der Erweiterungen: Stossvorschlag, Gyroskop, Game-Bar, Stoss verwerfen,
Spielaufzeichnung (inkl. Video), Grafik.

    python3 -m sim.test_features
"""

import json
import math
import os
import sys
import tempfile
from types import SimpleNamespace as B

from ballnum import ball_number
from cue_sensor import AlignChecker, CueSensor, angle_diff
from game import Game
from planner import plan_shot
from replay import RoundRecorder, build_video, match_balls, tween

FAILS = []
R = 10.2


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def playing(groups=(None, None), remaining=(7, 7), turn=0, breaking=False):
    g = Game()
    g.start_match("Rot", "Blau", 0)
    g.groups = list(groups)
    g.remaining = {"solid": remaining[0], "stripe": remaining[1]}
    g.turn = turn
    g.breaking = breaking
    return g.to_dict()


def test_planner():
    print("Stossvorschlag")
    # Gerader Stoss: Weisse - Kugel - Ecke unten rechts auf einer Linie
    balls = [B(x=500, y=200, kind="cue", color=""), B(x=650, y=300, kind="solid", color="rot")]
    s = plan_shot(balls, R, playing(("solid", "stripe"), (7, 7)))
    check(s and s["type"] == "pot" and s["pocket_index"] == 5, "gerader Stoss in die naechste Ecke")
    check(s and s["cut"] < 1.0, f"Schnitt ~0 Grad ({s and s['cut']:.1f})")
    exp = math.degrees(math.atan2(s["ghost"][1] - 200, s["ghost"][0] - 500))
    check(abs(angle_diff(s["angle"], exp)) < 0.01, "Queue-Winkel zeigt auf die Geisterkugel")
    check(s["target_number"] == 3 and "die 3" in s["text"], "Text nennt die Kugelnummer")

    # Blockierter Weg: Kugel genau dazwischen -> andere Tasche oder kein Stoss in diese Ecke
    balls2 = balls + [B(x=575, y=250, kind="stripe", color="blau")]
    s2 = plan_shot(balls2, R, playing(("solid", "stripe")))
    check(s2 is not None and s2.get("pocket_index") != 5, "verdeckter Weg wird nicht vorgeschlagen")

    # Fremde Gruppe wird nicht vorgeschlagen
    s3 = plan_shot(balls, R, playing(("stripe", "solid")))
    check(s3["type"] == "none", "nur eigene Gruppe (Halbe spielt nicht auf Volle)")

    # Eigene Gruppe leer -> die 8
    balls4 = [B(x=500, y=200, kind="cue", color=""), B(x=650, y=300, kind="eight", color="schwarz")]
    s4 = plan_shot(balls4, R, playing(("solid", "stripe"), (0, 3)))
    check(s4["type"] == "pot" and s4["target_kind"] == "eight", "Gruppe abgeraeumt -> Vorschlag auf die 8")

    # Anstoss
    s5 = plan_shot(balls, R, playing(breaking=True))
    check(s5["type"] == "break", "Anstoss-Vorschlag")

    # Kein Spiel / keine Weisse
    check(plan_shot(balls, R, Game().to_dict()) is None, "ohne Spiel kein Vorschlag")
    check(plan_shot(balls[1:], R, playing()) is None, "ohne Weisse kein Vorschlag")

    # Mitteltasche nur bei steilem Anlauf
    balls6 = [B(x=200, y=380, kind="cue", color=""), B(x=360, y=388, kind="solid", color="gelb")]
    s6 = plan_shot(balls6, R, playing(("solid", "stripe")))
    check(s6.get("pocket_index") != 4, "flach an der Bande nicht in die Mitteltasche")


def test_cue():
    print("Gyroskop")
    now = [100.0]
    clock = lambda: now[0]
    cs = CueSensor(offset=0, clock=clock)
    check(cs.table_angle() is None, "ohne Daten nicht verbunden")
    check(not cs.feed(b"kaputt"), "kaputtes Paket wird ignoriert")
    cs.feed(json.dumps({"yaw": 50.0, "pitch": -2}).encode(), ("1.2.3.4", 5005))
    check(cs.connected, "verbunden nach Paket")
    cs.calibrate()
    check(abs(cs.table_angle()) < 1e-9, "nach Kalibrierung 0 Grad")
    cs.feed(json.dumps({"yaw": 80.0}).encode())
    check(abs(cs.table_angle() - (-30)) < 1e-9, "30 Grad gegen den Uhrzeigersinn = -30 in der Grafik")
    cs.flip = True
    check(abs(cs.table_angle() - 30) < 1e-9, "Drehrichtung umkehren")
    cs.feed(json.dumps({"yaw": -170.0}).encode())
    check(abs(cs.table_angle() - 140) < 1e-9, "Winkel ueber +-180 sauber umgerechnet")
    now[0] += 5
    check(not cs.connected and cs.table_angle() is None, "nach 5 s ohne Daten getrennt")

    al = AlignChecker(tolerance=2.0, hold_s=0.4, clock=clock)
    al.update(10.0, 11.0)
    check(not al.ok, "Haken nicht sofort (Haltezeit)")
    now[0] += 0.5
    check(al.update(10.5, 11.0), "nach Haltezeit gruener Haken")
    check(al.update(13.0, 11.0), "kleines Zittern ueber Toleranz: Haken bleibt (Hysterese)")
    check(not al.update(16.0, 11.0), "deutlich daneben: Haken weg")
    check(abs(angle_diff(179, -179) - (-2)) < 1e-9, "Winkeldifferenz ueber 180 Grad")


def test_bar():
    print("Game-Bar")
    from scene import bar_balls, team_points
    g = playing(("solid", "stripe"), (5, 7))
    g["on_table"] = [n for n in range(1, 16) if n not in (2, 7)]
    bar = bar_balls(g, set(), set())
    check(not bar[2] and not bar[7] and all(bar[n] for n in (1, 3, 4, 5, 6)), "Spielstand je Kugel: 2 und 7 durchgestrichen")
    check(all(bar[n] for n in range(9, 16)) and bar[8], "Halbe und 8 noch im Spiel")
    alt = dict(g)
    del alt["on_table"]                                  # aeltere Aufzeichnung: nur Anzahl, Nummern von der Kamera
    visible = {1, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15}
    bar = bar_balls(alt, visible, visible | {2, 7})
    check(not bar[2] and not bar[7] and all(bar[n] for n in (1, 3, 4, 5, 6)), "alte Aufzeichnung: verschwundene 2 und 7")
    alt = playing(("solid", "stripe"), (6, 7))
    del alt["on_table"]
    bar = bar_balls(alt, set(), set())
    check(sum(not bar[n] for n in range(1, 8)) == 1, "ohne lesbare Farben: Anzahl stimmt trotzdem")
    check(team_points(g) == [2, 0], "Punkte = versenkte eigene Kugeln")
    idle = Game().to_dict()
    check(all(bar_balls(idle, set(), set()).values()), "ohne Spiel alle Kugeln im Spiel")


def test_pockets():
    print("Stand je Kugel, Kugel fehlt 4 Bilder = im Loch, manueller Modus")
    from shots import PocketTracker
    from ballnum import Ball

    def kugel(n, x, y):
        return Ball.from_number(x, y, R, n, 0.9)
    liegen = [kugel(0, 100, 100), kugel(3, 300, 200), kugel(11, 500, 150), kugel(8, 600, 300)]
    t = PocketTracker(R)
    t.reset(liegen)
    ohne3 = [b for b in liegen if ball_number(b.kind, b.color) != 3]
    check([t.update(ohne3, False) for _ in range(3)] == [[], [], []], "3 Bilder ohne Kugel 3: noch nicht im Loch")
    weg = t.update(ohne3, False)
    check([ball_number(b.kind, b.color) for b in weg] == [3], "4. Bild ohne Kugel 3: im Loch")
    t.reset(liegen)
    for _ in range(6):
        t.update(ohne3, True)
    check(t.update(ohne3, False) == [], "verdeckte Bilder (Arm über dem Tisch) zählen nicht")
    t.reset(liegen)
    gerollt = [kugel(0, 120, 110), kugel(3, 760, 390), kugel(11, 520, 160), kugel(8, 600, 300)]
    check(all(t.update(gerollt, False) == [] for _ in range(6)), "weit gerollte Kugel wird wiedergefunden (Nummer)")
    t.reset(liegen)
    ohne11 = [b for b in liegen if ball_number(b.kind, b.color) != 11] + [kugel(4, 520, 160)]
    weg = [b for _ in range(4) for b in t.update(ohne11, False)]
    check([ball_number(b.kind, b.color) for b in weg] == [11], "fällt die 11, springt keine andere Kugel daneben ein")

    g = Game()
    g.start_match("Rot", "Blau", 0)
    g.breaking = False
    g.ball_gone("solid", 3)
    check(3 not in g.on_table and g.remaining == {"solid": 6, "stripe": 7} and "Kugel 3 im Loch" in g.message,
          "Kamera: Kugel 3 sofort im Loch, Zähler 6/7")
    msg = g.apply_shot({"solid": 7, "stripe": 7, "eight": 1}, {"solid": 6, "stripe": 7, "eight": 1}, True, True,
                       seen={1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
    check(g.remaining == {"solid": 6, "stripe": 7} and g.turn == 0 and g.groups == ["solid", "stripe"],
          f"Stoßende zählt Kugel 3 nicht doppelt, Rot spielt Volle und bleibt dran ({msg})")
    g.ball_gone("stripe", 12)
    g.apply_shot({"solid": 6, "stripe": 7, "eight": 1}, {"solid": 6, "stripe": 7, "eight": 1}, True, True,
                 seen={1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
    check(12 in g.on_table and g.remaining["stripe"] == 7, "Kugel war nur verdeckt: nach dem Stoß wieder zu sehen -> zurück")
    g.ball_gone("stripe", 13)
    g.settled({1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
    check(13 in g.on_table, "auch ohne Stoß: wieder zu sehen -> zurück")
    vorher = sorted(g.on_table)
    g.ball_gone("solid", 5)
    g.apply_shot({"solid": 6, "stripe": 7, "eight": 1}, {"solid": 5, "stripe": 7, "eight": 1}, True, True)
    g.undo_shot()
    check(sorted(g.on_table) == vorher, "„Stoß verwerfen“ nimmt die gerade erkannte Kugel mit zurück")

    check("schon versenkt" in g.pocket_ball(3), "Kugel 3 ist schon versenkt")
    g.restore_ball(3)
    check(3 in g.on_table and g.remaining["solid"] == 7, "Panel: Kugel 3 zurück auf den Tisch")
    g.pocket_ball(9)
    check(9 not in g.on_table and g.remaining["stripe"] == 6, "Panel: Kugel 9 ins Loch")
    g.undo()
    check(9 in g.on_table, "Rückgängig holt Kugel 9 zurück")

    m = Game()
    m.start_match("Rot", "Blau", 1)
    m.breaking = False
    m.set_manual(True)
    m.ball_gone("solid", 2)
    check(m.remaining["solid"] == 7, "manuell: Kamera nimmt keine Kugel weg")
    check(m.apply_shot({"solid": 7, "stripe": 7, "eight": 1}, {"solid": 5, "stripe": 7, "eight": 1}, True, True) is None
          and m.remaining["solid"] == 7 and m.turn == 1, "manuell: Kamera-Stoß ändert nichts")
    m.pocket_ball(10)
    check(m.groups == ["solid", "stripe"] and m.remaining["stripe"] == 6, "manuell: Blau tippt die 10 an -> spielt Halbe")
    m.undo()
    check(m.manual and m.groups == [None, None], "Rückgängig ändert den manuellen Modus nicht")
    m.groups = ["solid", "stripe"]
    for n in range(9, 16):
        m.pocket_ball(n)
    m.pocket_ball(8)
    check(m.phase == "finished" and m.winner == 1, "manuell: Blau hat alle Halben, versenkt die 8 -> gewinnt")
    d = m.to_dict()
    check(d["manual"] is True and 8 not in d["on_table"] and d["remaining"] == {"solid": 7, "stripe": 0},
          "Spielstand fürs Panel: manuell, Kugeln je Nummer")


def test_undo_shot():
    print("Stoss verwerfen")
    C = lambda s, h: {"solid": s, "stripe": h, "eight": 1, "cue": 1}
    g = Game()
    g.start_match("Rot", "Blau", 0)
    g.apply_shot(C(7, 7), C(6, 6), True, True)
    g.apply_shot(C(6, 6), C(6, 5), True, True)       # Rot spielt Halbe
    before = (g.turn, list(g.groups), dict(g.remaining))
    g.apply_shot(C(6, 5), C(5, 5), True, True)       # faelschlich: Volle weg -> Wechsel
    g.foul()                                          # danach noch eine Admin-Aenderung
    msg = g.undo_shot()
    check((g.turn, g.groups, g.remaining) == before, "Spielstand wie vor dem falschen Stoss")
    check("1 späterer" in msg, "Meldung nennt mitverworfene Aenderung")
    g2 = Game()
    g2.start_match("A", "B")
    check("Kein erkannter" in g2.undo_shot(), "ohne Stoss nichts zu verwerfen")
    check(g.to_dict()["can_undo_shot"], "weitere Stoesse verwerfbar")


def scene_of(balls, game):
    return {"balls": balls, "radius": R, "game": game, "bar": {}, "suggestion": None, "cue": None,
            "status": "Bereit", "info": ""}


def test_replay():
    print("Spielaufzeichnung")
    a = [{"x": 100, "y": 100, "kind": "cue", "color": "", "number": 0},
         {"x": 300, "y": 200, "kind": "solid", "color": "rot", "number": 3},
         {"x": 500, "y": 300, "kind": "stripe", "color": "blau", "number": 10},
         {"x": 600, "y": 100, "kind": "solid", "color": "", "number": None}]
    b = [{"x": 250, "y": 150, "kind": "cue", "color": "", "number": 0},
         {"x": 520, "y": 330, "kind": "stripe", "color": "blau", "number": 10},
         {"x": 610, "y": 110, "kind": "solid", "color": "", "number": None}]
    pairs, gone, new = match_balls(a, b, R)
    check(sorted(pairs) == [(0, 0), (2, 1), (3, 2)] and gone == [1] and new == [], "Kugeln ueber Nummer/Typ zugeordnet")
    g = playing()
    mid = tween(scene_of(a, g), scene_of(b, g), 0.5, R)
    cue = next(x for x in mid["balls"] if x["kind"] == "cue")
    check(100 < cue["x"] < 250, "Zwischenbild: Weisse unterwegs")
    sunk = next(x for x in mid["balls"] if x.get("number") == 3)
    check(sunk["alpha"] < 1 and sunk["scale"] < 1, "versenkte Kugel verschwindet")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.mp4")
        build_video([scene_of(a, g), scene_of(b, g)], path, size=(320, 180), fps=5)
        check(os.path.getsize(path) > 1000, "Video wird geschrieben")
        import cv2
        cap = cv2.VideoCapture(path)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        check(n >= 20, f"Video hat Bilder ({n})")

        # Mit Trainingsseite: nur Daten, der Pi baut kein Video
        rec = RoundRecorder(os.path.join(d, "rec"), size=(320, 180), fps=5, local_build=lambda: False)
        rec.start(scene_of(a, g))
        rec.add(scene_of(b, g))
        check(rec.active and os.path.isfile(os.path.join(d, "rec", "laufend.json")), "laufendes Rack wird gesichert")
        check(not any(f.endswith((".png", ".jpg")) for f in os.listdir(os.path.join(d, "rec"))),
              "keine Bilder auf dem Pi (weder Grafik noch Kamera)")
        rec.stop(scene_of(b, g))
        fertig = rec.finished()
        check(len(fertig) == 1 and not rec.active and not rec.building, "Rack vorbei: Daten fertig, kein Video auf dem Pi")
        with open(fertig[0][1]) as f:
            data = json.load(f)
        check(len(data["schritte"]) == 3 and data["teams"] == g["teams"] and data["ende"], "Daten fürs Video (3 Züge, Teams)")
        rec.start(scene_of(a, g))
        rec.stop()
        check(len(rec.finished()) == 1, "nur gestartet und gleich abgebrochen: keine Aufzeichnung")
        rec.start(scene_of(a, g))
        rec.add(scene_of(b, g))
        rec2 = RoundRecorder(os.path.join(d, "rec"), local_build=lambda: False)   # "Neustart" mitten im Rack
        check(len(rec2.finished()) == 2 and not os.path.exists(os.path.join(d, "rec", "laufend.json")),
              "nach einem Neustart wird das angefangene Rack abgeschlossen")

        # Ohne Trainingsseite baut der Pi das Video selbst
        rec = RoundRecorder(os.path.join(d, "lokal"), size=(320, 180), fps=5, local_build=lambda: True)
        rec.start(scene_of(a, g))
        rec.stop(scene_of(b, g))
        import time
        for _ in range(100):
            if not rec.building:
                break
            time.sleep(0.1)
        vids = rec.videos()
        check(len(vids) == 1 and rec.video_path(vids[0]["name"]) and not rec.finished(),
              "ohne Trainingsseite: Video auf dem Pi, Daten danach weg")
        check(rec.video_path("../config.json") is None and rec.video_path("x.mp4") is None, "nur echte Videos abrufbar")


def test_numbers():
    print("Kugelnummern")
    check(ball_number("solid", "gelb") == 1 and ball_number("stripe", "gelb") == 9, "gelb = 1 / 9")
    check(ball_number("stripe", "weinrot") == 15 and ball_number("eight", "") == 8, "15 und 8")
    check(ball_number("solid", "") is None, "unbekannte Farbe -> keine Nummer")


if __name__ == "__main__":
    test_numbers()
    test_planner()
    test_cue()
    test_bar()
    test_pockets()
    test_undo_shot()
    test_replay()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
