"""
Tests der Spiellogik:
  Teil 1: Regeln direkt (ohne Bilder)
  Teil 2: komplettes simuliertes Spiel aus gerenderten Kamerabildern durch die
          echte Pipeline (Bewegung, Verdeckung, Stosserkennung, Regeln). Die Kugeln
          liefert ein Ersatz fuer den KI-Chip (sim/truth_detector.py) mit Rauschen und
          gelegentlichen Fehlern.

    python3 -m sim.test_game
"""

import math
import sys

import numpy as np

from game import Game, pocketed_from_states
from geometry import Table, TABLE_SIZES
from pipeline import Pipeline
from sim.render import DEFAULT_CORNERS, SimBall, rack_positions, random_layout, render_topdown, to_camera
from sim.truth_detector import TruthDetector

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def C(solid=0, stripe=0, eight=1, cue=1):
    return {"solid": solid, "stripe": stripe, "eight": eight, "cue": cue}


# ---------------------------------------------------------------- Teil 1
def test_rules():
    print("Teil 1: Regeln")
    d, u = pocketed_from_states(C(7, 7), C(6, 7))
    check(d["solid"] == 1 and not u, "eine volle versenkt")
    d, u = pocketed_from_states(C(7, 7), C(8, 6))
    check(sum(d.values()) == 0 and u, "nur Typ gekippt (halbe sieht voll aus) -> nichts zaehlen")
    d, u = pocketed_from_states(C(7, 7), C(7, 5))
    check(d["stripe"] == 2, "zwei halbe versenkt")

    g = Game()
    g.start_match("Rot", "Blau", 0)
    g.apply_shot(C(7, 7), C(6, 6), True, True)          # Break: 1 volle + 1 halbe
    check(g.turn == 0 and g.groups == [None, None], "Break mit Treffern: weiter, Tisch offen")
    g.apply_shot(C(6, 6), C(6, 5), True, True)
    check(g.turn == 0 and g.groups == ["stripe", "solid"], "erste Kugel nach Break -> Rot spielt Halbe")
    g.apply_shot(C(6, 5), C(6, 5), True, True)
    check(g.turn == 1, "nichts versenkt -> Wechsel zu Blau")
    g.apply_shot(C(6, 5), C(6, 4), True, True)
    check(g.turn == 0, "Blau versenkt nur eine HALBE (fremd) -> Wechsel")
    g.apply_shot(C(6, 4), C(6, 4), True, False)
    check(g.turn == 1 and g.ball_in_hand, "Rot versenkt Weisse -> Foul, Blau Ball in der Hand")
    g.apply_shot(C(6, 4, 1, 1), C(6, 4, 0, 1), True, True)
    check(g.phase == "finished" and g.winner == 0, "Blau versenkt 8 zu frueh -> Rot gewinnt")
    check(g.frames_won == [1, 0], "Stand 1:0")
    g.undo()
    check(g.phase == "playing" and g.frames_won == [0, 0], "Rueckgaengig stellt Spiel wieder her")

    g = Game()
    g.start_match("A", "B", 0)
    g.set_group(0, "solid")
    g.remaining = {"solid": 0, "stripe": 3}
    g.breaking = False
    g.apply_shot(C(0, 3), C(0, 3, 0), True, True)
    check(g.winner == 0, "Gruppe abgeraeumt, dann 8 -> Sieg")

    g = Game()
    g.start_match("A", "B", 0)
    g.set_group(0, "solid")
    g.remaining = {"solid": 1, "stripe": 3}
    g.breaking = False
    g.apply_shot(C(1, 3), C(0, 3, 0), True, True)
    check(g.winner == 1, "letzte eigene UND 8 im selben Stoss -> Niederlage (strenge Regel)")


# ---------------------------------------------------------------- Teil 2
class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_simulated_game(seed=42):
    print(f"\nTeil 2: simuliertes Spiel ueber Kamerabilder (Zufall {seed})")
    rng = np.random.default_rng(seed)
    L = TABLE_SIZES["8ft"]
    W = L / 2
    clock = FakeClock()
    table = Table(DEFAULT_CORNERS, L)
    det = TruthDetector(table.ball_radius_px, L, W, rng)
    pipe = Pipeline(table, lambda r: det, clock=clock)
    g = pipe.game
    fps = 3                     # wie cameras.KAMERA_FPS

    def show(balls, n_frames=10, occluder=None):
        det.balls, det.occluder = balls, occluder
        for _ in range(n_frames):
            cam = to_camera(render_topdown(balls, L, W, "gruen", rng, occluder), DEFAULT_CORNERS)
            ev = pipe.process(cam)
            clock.t += 1 / fps
            if isinstance(ev, tuple):
                print(f"         -> {ev[1]}")

    def roll(balls, steps=6):
        """Kugeln rollen sichtbar (Bewegung), danach neue Ruheposition."""
        for s in range(steps):
            moving = [SimBall(b.number, min(L - 0.04, max(0.04, b.x + rng.normal(0, 0.05))),
                              min(W - 0.04, max(0.04, b.y + rng.normal(0, 0.05))), b.look) for b in balls]
            show(moving, 1)

    def shoot(balls, pocket_numbers, everything=False, n_move=2):
        """Realistischer Stoss: Weisse + versenkte + n_move weitere Kugeln bewegen sich,
        alle anderen bleiben exakt liegen (gleiche Position, gleiches Aussehen).
        everything=True: Break, alles fliegt."""
        roll(balls)
        keep = [b for b in balls if b.number not in pocket_numbers]
        if everything:
            return random_layout(rng, [b.number for b in keep], L, W, cluster_prob=0.15)
        others = [b for b in keep if b.number != 0]
        movers = {0} | {int(n) for n in rng.choice([b.number for b in others],
                                                    min(n_move, len(others)), replace=False)}
        still = [b for b in keep if b.number not in movers]
        placed = list(still)
        for b in keep:
            if b.number in movers:
                for _ in range(200):
                    nb = random_layout(rng, [b.number], L, W)[0]
                    if all(math.hypot(nb.x - p.x, nb.y - p.y) > 0.06 for p in placed):
                        placed.append(nb)
                        break
        return placed

    # Aufbauen: Rack + Weisse
    balls = rack_positions(L, W) + [SimBall(0, L * 0.25, W / 2)]
    g.start_match("Rot", "Blau", 0)
    pipe.restart_tracking()
    show(balls)
    check(pipe.monitor.state == "ruhig", "Ausgangszustand erkannt")
    print("     erkannt:", pipe.stable_counts())

    print("  Break: 1 volle (3) und 1 halbe (11) fallen")
    balls = shoot(balls, {3, 11}, everything=True)
    show(balls)
    check(g.turn == 0 and g.groups == [None, None] and g.remaining == {"solid": 6, "stripe": 6},
          "Rot bleibt dran, Tisch offen, 6/6")

    print("  Rot versenkt die 12 (halbe)")
    balls = shoot(balls, {12})
    show(balls)
    check(g.groups[0] == "stripe" and g.turn == 0 and g.remaining == {"solid": 6, "stripe": 5}, "Rot spielt Halbe, bleibt dran, 6/5")

    print("  Arm ueber dem Tisch, nichts gestossen")
    show(balls, 10, occluder=(L * 0.5, W * 0.5, 0.25))
    show(balls)
    check(g.turn == 0 and g.shots == 2, "Verdeckung loest keinen Stoss aus")

    print("  Rot trifft nichts")
    balls = shoot(balls, set())
    show(balls)
    check(g.turn == 1 and g.remaining == {"solid": 6, "stripe": 5}, "Wechsel zu Blau, weiter 6/5")

    print("  Blau versenkt die Weisse (Foul)")
    balls = shoot(balls, {0})
    show(balls)
    check(g.turn == 0 and g.ball_in_hand and g.remaining == {"solid": 6, "stripe": 5}, "Foul erkannt, Rot Ball in der Hand, 6/5")

    print("  Rot legt die Weisse auf (kein Stoss)")
    show(balls, 6, occluder=(L * 0.3, W * 0.4, 0.2))
    balls = balls + [SimBall(0, L * 0.3, W * 0.4)]
    show(balls)
    check(g.turn == 0 and g.shots == 4, "Weisse hinlegen ist kein Stoss")

    print("  Rot versenkt 9 und 10")
    balls = shoot(balls, {9, 10})
    show(balls)
    check(g.remaining == {"solid": 6, "stripe": 3} and g.turn == 0, "6/3, Rot bleibt dran")

    print("  Rot versenkt die 8 zu frueh")
    balls = shoot(balls, {8})
    show(balls)
    check(g.phase == "finished" and g.winner == 1, "Blau gewinnt das Rack")
    print("  Endstand:", g.frames_won, "-", g.message)


def test_queue_help():
    """Stosserkennung mit Hilfe vom Queue: Gyroskop-Stoss und Knopf 'Stoss fertig'."""
    from ballnum import Ball
    from shots import ShotEvent, ShotMonitor
    print("Stosserkennung: Hilfe vom Queue")
    r = 10.0
    t = [0.0]
    m = ShotMonitor(r, clock=lambda: t[0])
    base = [Ball.from_number(100, 100, r, 0), Ball.from_number(300, 200, r, 3), Ball.from_number(500, 100, r, 9)]
    nudged = [Ball.from_number(112, 100, r, 0)] + base[1:]          # Weisse nur 1,2 Radien bewegt

    def still(balls, s=2.0):
        out = None
        end = t[0] + s
        while t[0] < end:
            t[0] += 0.1
            ev = m.update(balls, False, False)
            out = ev if ev is not None else out
        return out

    def move():
        t[0] += 0.1
        m.update([], True, False)

    check(still(base) == "reference", "Ausgangszustand")
    move()
    check(still(nudged) is None, "ohne Queue: Weisse 1,2 Radien bewegt -> kein Stoss (zu wenig)")
    m.cue_stroke()
    move()
    back = list(base)
    check(isinstance(still(back), ShotEvent), "Queue meldet Stoss: kleine Bewegung der Weissen reicht")
    m.cue_stroke()
    check(isinstance(still(nudged), ShotEvent), "Queue-Stoss auch ohne von der Kamera gesehene Bewegung")
    check(m.finish(), "'Stoss fertig' angenommen")
    t0 = t[0]
    ev = None
    while ev is None and t[0] - t0 < 3:
        t[0] += 0.1
        ev = m.update(nudged, False, False)
    check(isinstance(ev, ShotEvent) and t[0] - t0 < 0.6,
          f"'Stoss fertig': sofort ausgewertet ({t[0] - t0:.1f} s) und zaehlt auch ohne Veraenderung")
    m.finish()
    t[0] += 0.1
    check(m.update(nudged, False, True) is None, "'Stoss fertig' wartet, solange etwas ueber dem Tisch haengt")
    m.reset()
    check(not m.finish(), "'Stoss fertig' ohne Ausgangszustand: abgelehnt")


if __name__ == "__main__":
    test_queue_help()
    test_rules()
    for seed in (42, 7, 123):
        test_simulated_game(seed)
    print(f"\n{'ALLES OK' if not FAILS else f'{len(FAILS)} FEHLER'}")
    sys.exit(1 if FAILS else 0)
