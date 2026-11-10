"""
Erzeugt Trainingsdaten aus dem Simulator - im selben Format wie "Trainingsdaten laden"
im Admin-Portal. Nur zum Testen der Trainingskette (training/train_coral.py)!
Ein Modell nur aus Simulator-Bildern erkennt echte Kugeln schlecht.

    python3 -m sim.make_training_zip 200 sim-training.zip
"""

import sys
import zipfile

import cv2
import numpy as np

from geometry import TABLE_SIZES, WARP_H, WARP_W, Table
from sim.render import DEFAULT_CORNERS, rack_positions, random_layout, render_topdown, to_camera

L = TABLE_SIZES["8ft"]
W = L / 2


def main(n=200, path="sim-training.zip", seed=7):
    rng = np.random.default_rng(seed)
    table = Table(DEFAULT_CORNERS, L)
    r = table.ball_radius_px
    with zipfile.ZipFile(path, "w") as z:
        # Klasse = Kugelnummer (0 = Weisse, 8 = schwarze 8), wie im Admin-Portal-Export
        z.writestr("classes.txt", "\n".join(str(n) for n in range(16)) + "\n")
        for i in range(n):
            if i % 10 == 0:
                balls = rack_positions(L, W) + random_layout(rng, [0], L, W)
            else:
                k = int(rng.integers(3, 16))
                nums = [0, 8] + list(rng.choice([j for j in range(1, 16) if j != 8], k - 2, replace=False))
                balls = random_layout(rng, nums, L, W, cluster_prob=0.3)
            cloth = "blau" if i % 3 else "gruen"
            warped = table.warp(to_camera(render_topdown(balls, L, W, cloth, rng), DEFAULT_CORNERS))
            ok, jpg = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
            name = f"sim-{i:05d}"
            z.writestr(f"images/{name}.jpg", jpg.tobytes())
            lines = [f"{b.number} {b.x / L:.5f} {b.y / W:.5f} {2 * r / WARP_W:.5f} {2 * r / WARP_H:.5f}"
                     for b in balls]
            z.writestr(f"labels/{name}.txt", "\n".join(lines) + "\n")
    print(f"{n} Bilder -> {path}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200, sys.argv[2] if len(sys.argv) > 2 else "sim-training.zip")
