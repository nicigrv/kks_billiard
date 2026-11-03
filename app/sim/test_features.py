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
    visible = {1, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15}
    bar = bar_balls(g, visible, visible | {2, 7})
    check(not bar[2] and not bar[7] and all(bar[n] for n in (1, 3, 4, 5, 6)), "verschwundene 2 und 7 durchgestrichen")
    check(all(bar[n] for n in range(9, 16)) and bar[8], "Halbe und 8 noch im Spiel")
    bar = bar_balls(playing(("solid", "stripe"), (6, 7)), set(), set())
    check(sum(not bar[n] for n in range(1, 8)) == 1, "ohne lesbare Farben: Anzahl stimmt trotzdem")
    check(team_points(g) == [2, 0], "Punkte = versenkte eigene Kugeln")
    idle = Game().to_dict()
    check(all(bar_balls(idle, set(), set()).values()), "ohne Spiel alle Kugeln im Spiel")


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

        rec = RoundRecorder(os.path.join(d, "rec"), size=(320, 180), fps=5)
        rec.start(scene_of(a, g))
        rec.add(scene_of(b, g))
        rd = rec.dir
        check(sorted(f for f in os.listdir(rd) if f.endswith(".png")) == ["schritt-001.png", "schritt-002.png"],
              "Grafik je Zug als PNG gespeichert")
        check(not any("cam" in f or f.endswith(".jpg") for f in os.listdir(rd)), "keine Kamerabilder gespeichert")
        rec.stop()
        import time
        for _ in range(100):
            if not rec.building:
                break
            time.sleep(0.1)
        vids = rec.videos()
        check(len(vids) == 1 and rec.video_path(vids[0]["name"]), "Video in der Liste")
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
    test_undo_shot()
    test_replay()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
