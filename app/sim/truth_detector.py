"""
Ersatz fuer den KI-Chip in den Simulator-Tests: kennt die echten Kugeln aus dem Simulator
und liefert sie wie der Coral (Position + Nummer) - mit etwas Rauschen und gelegentlich
uebersehenen oder falsch gelesenen Kugeln, damit die Stoss- und Spiellogik unter
realistischen Bedingungen geprueft wird.
"""

import math

import numpy as np

from ballnum import Ball
from coral_detector import CoralNotReady
from geometry import WARP_H, WARP_W


class TruthDetector:
    name = "simulator"

    def __init__(self, radius_px, length_m, width_m, rng=None, noise_px=0.8, miss=0.03, confuse=0.01):
        self.r = float(radius_px)
        self.length_m, self.width_m = length_m, width_m
        self.rng = rng or np.random.default_rng(0)
        self.noise_px, self.miss, self.confuse = noise_px, miss, confuse
        self.balls = []            # SimBall-Liste (Meter), vom Test gesetzt
        self.occluder = None       # (x_m, y_m, radius_m): darunter ist nichts zu sehen
        self.ready = True

    def detect(self, warped):
        if not self.ready:
            raise CoralNotReady("Simulator: KI-Chip aus")
        out = []
        for b in self.balls:
            if self.occluder is not None:
                ox, oy, orad = self.occluder
                if math.hypot(b.x - ox, b.y - oy) < orad:
                    continue
            if self.rng.random() < self.miss:
                continue
            n = b.number
            if self.rng.random() < self.confuse:
                n = int(self.rng.integers(0, 16))
            x = b.x / self.length_m * WARP_W + self.rng.normal(0, self.noise_px)
            y = b.y / self.width_m * WARP_H + self.rng.normal(0, self.noise_px)
            out.append(Ball.from_number(x, y, self.r, n, 0.9))
        return out
