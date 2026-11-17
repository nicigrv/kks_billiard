"""
Startwerte fuer Stufe 2 + 3 (gestreift? Farbe?) aus Simulator-Bildern -> models/start_merkmale.json

Gelten nur, bis echte Farben gelernt sind (Admin-Panel "Farben aus festgelegten Bildern lernen"
oder training/train_coral.py). Echte Kamera + Lampe sehen anders aus als der Simulator.

    python3 -m sim.make_start_merkmale [bilder] [ziel]
"""

import os
import sys

import numpy as np

import kugel_merkmale as km
from geometry import TABLE_SIZES, WARP_H, WARP_W, Table
from sim.render import DEFAULT_CORNERS, random_layout, render_topdown, to_camera

L = TABLE_SIZES["8ft"]
W = L / 2
TARGET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "start_merkmale.json")


def sim_samples(n, seed, jitter=0.0):
    """Simulator-Draufsichten mit bekannten Kugeln: (bild, [(x, y, nummer)], radius)."""
    rng = np.random.default_rng(seed)
    table = Table(DEFAULT_CORNERS, L)
    r = table.ball_radius_px
    for i in range(n):
        nums = [0, 8] + list(rng.choice([j for j in range(1, 16) if j != 8], 10, replace=False))
        balls = random_layout(rng, nums, L, W, cluster_prob=0.3)
        warped = table.warp(to_camera(render_topdown(balls, L, W, "blau" if i % 3 else "gruen", rng),
                                      DEFAULT_CORNERS))
        yield warped, [(b.x / L * WARP_W + rng.normal(0, jitter), b.y / W * WARP_H + rng.normal(0, jitter),
                        int(b.number)) for b in balls], r


def main(n=120, path=TARGET):
    clf = km.fit(km.collect(sim_samples(n, 11, jitter=1.0)), source="Simulator")
    clf.info["erstellt"] = "Simulator"
    clf.save(path)
    print(km.report(km.evaluate(clf, km.collect(sim_samples(30, 99, jitter=1.0))), "Simulator-Prüfbilder"))
    print(f"-> {path}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 120, sys.argv[2] if len(sys.argv) > 2 else TARGET)
