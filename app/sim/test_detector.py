"""
Misst die Erkennungsqualitaet auf simulierten Kamerabildern.

    python3 -m sim.test_detector [anzahl_szenen]
"""

import sys
from collections import Counter

import numpy as np

from detector import ClassicDetector
from geometry import WARP_W, WARP_H, Table, TABLE_SIZES
from sim.render import DEFAULT_CORNERS, TOP_W, rack_positions, random_layout, render_topdown, to_camera

LENGTH = TABLE_SIZES["8ft"]
WIDTH = LENGTH / 2


def evaluate(scenes=40, cloth="gruen", seed=1, verbose=False):
    rng = np.random.default_rng(seed)
    cam_scale = 800 / 1600  # Kamerapixel je Simulationspixel ungefaehr
    table = Table(DEFAULT_CORNERS, LENGTH)
    det = ClassicDetector(table.ball_radius_px)
    tp = fp = fn = 0
    kind_ok = Counter()
    kind_total = Counter()
    confusion = Counter()
    for s in range(scenes):
        if s == 0:
            balls = rack_positions(LENGTH, WIDTH) + [random_layout(rng, [0], LENGTH, WIDTH)[0]]
        else:
            n = int(rng.integers(3, 16))
            nums = [0, 8] + list(rng.choice([i for i in range(1, 16) if i != 8], n - 2, replace=False))
            balls = random_layout(rng, nums, LENGTH, WIDTH, cluster_prob=0.3)
        top = render_topdown(balls, LENGTH, WIDTH, cloth, rng)
        cam = to_camera(top, DEFAULT_CORNERS)
        found, info = det.detect(table.warp(cam))

        gt = [(b.x / LENGTH * WARP_W, b.y / WIDTH * WARP_H, b.kind) for b in balls]
        used = set()
        for gx, gy, gk in gt:
            best, bd = None, 1e9
            for i, f in enumerate(found):
                if i in used:
                    continue
                d = np.hypot(f.x - gx, f.y - gy)
                if d < bd:
                    best, bd = i, d
            if best is not None and bd < det.r * 0.8:
                used.add(best)
                tp += 1
                kind_total[gk] += 1
                fk = found[best].kind
                if fk == gk:
                    kind_ok[gk] += 1
                else:
                    confusion[(gk, fk)] += 1
            else:
                fn += 1
                confusion[(gk, "NICHT GEFUNDEN")] += 1
        fp += len(found) - len(used)
        if verbose:
            print(f"Szene {s}: {len(balls)} Kugeln, gefunden {len(found)}, Verdeckung {info.occlusion:.4f}")
    print(f"\n=== Tuch {cloth}, {scenes} Szenen, Kugelradius {det.r:.1f} px ===")
    print(f"Gefunden (Recall):    {tp / (tp + fn):6.1%}   ({fn} uebersehen)")
    print(f"Fehlalarme:           {fp}")
    for k in ("cue", "eight", "solid", "stripe"):
        if kind_total[k]:
            print(f"Typ richtig {k:7s}:   {kind_ok[k] / kind_total[k]:6.1%}   ({kind_total[k]} Kugeln)")
    print("Haeufigste Fehler:", confusion.most_common(6))
    return tp / (tp + fn), fp


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    for cloth in ("gruen", "blau"):
        evaluate(n, cloth)
