"""
Misst, wie oft ein einzelner Stoss richtig ausgewertet wird (ohne Folgefehler).

Pro Versuch: zufaelliger Tisch -> ruhig -> Stoss (0-2 Kugeln versenkt, ggf. Weisse,
Weisse + 2 weitere Kugeln rollen) -> ruhig -> Vergleich mit der Wahrheit.

    python3 -m sim.benchmark_shots [versuche]
"""

import math
import sys
from collections import Counter

import numpy as np

from geometry import Table, TABLE_SIZES
from shots import MotionMeter, ShotEvent, ShotMonitor
from detector import ClassicDetector
from sim.render import DEFAULT_CORNERS, SimBall, random_layout, render_topdown, to_camera

L = TABLE_SIZES["8ft"]
W = L / 2


def run(trials=60, seed=3, cloth="gruen"):
    rng = np.random.default_rng(seed)
    table = Table(DEFAULT_CORNERS, L)
    r = table.ball_radius_px
    det = ClassicDetector(r)
    ok = Counter()
    errors = Counter()
    for t in range(trials):
        clock = [0.0]
        mon = ShotMonitor(r, clock=lambda: clock[0])
        mm = MotionMeter()

        def feed(balls, n):
            ev = None
            for _ in range(n):
                warped = table.warp(to_camera(render_topdown(balls, L, W, cloth, rng), DEFAULT_CORNERS))
                found, info = det.detect(warped)
                e = mon.update(found, mm.update(warped), info.occlusion > 0.006)
                clock[0] += 1 / 12
                if isinstance(e, ShotEvent):
                    ev = e
            return ev

        n_obj = int(rng.integers(6, 15))
        nums = [0, 8] + [int(x) for x in rng.choice([i for i in range(1, 16) if i != 8], n_obj - 1, replace=False)]
        balls = random_layout(rng, nums, L, W, cluster_prob=0.15)
        feed(balls, 20)

        # Stoss
        objs = [b for b in balls if b.number not in (0, 8)]
        k = int(rng.choice([0, 1, 1, 2]))
        pocket = {int(n) for n in rng.choice([b.number for b in objs], k, replace=False)}
        scratch = rng.random() < 0.15
        if scratch:
            pocket.add(0)
        keep = [b for b in balls if b.number not in pocket]
        movers = {0} | {int(n) for n in rng.choice([b.number for b in keep if b.number != 0], 2, replace=False)}
        placed = [b for b in keep if b.number not in movers]
        for b in keep:
            if b.number in movers:
                for _ in range(300):
                    nb = random_layout(rng, [b.number], L, W)[0]
                    if all(math.hypot(nb.x - p.x, nb.y - p.y) > 0.06 for p in placed):
                        placed.append(nb)
                        break
        for _ in range(5):   # rollen
            feed([SimBall(b.number, min(L - .04, max(.04, b.x + rng.normal(0, .05))),
                          min(W - .04, max(.04, b.y + rng.normal(0, .05))), b.look) for b in balls], 1)
        ev = feed(placed, 20)

        truth = {"solid": sum(1 for n in pocket if 1 <= n <= 7), "stripe": sum(1 for n in pocket if n >= 9)}
        if ev is None:
            errors["Stoss nicht erkannt"] += 1
            continue
        d = ev.diff(r)
        got = {"solid": d.pocketed["solid"], "stripe": d.pocketed["stripe"]}
        good_pocket = got == truth
        good_scratch = (d.cue_before and not d.cue_after) == scratch
        ok["Versenkte Kugeln richtig"] += good_pocket
        ok["Foul (Weisse) richtig"] += good_scratch
        ok["Stoss komplett richtig"] += good_pocket and good_scratch
        if not good_pocket:
            errors[f"versenkt {truth} erkannt {got}"] += 1
    print(f"\n=== {trials} Einzelstoesse, Tuch {cloth} ===")
    for k_, v in ok.items():
        print(f"{k_:28s} {v / trials:6.1%}")
    print("Fehlerbilder:", errors.most_common(5))


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 60)
