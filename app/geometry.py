"""
Tisch-Geometrie: entzerrt das schraege Kamerabild in eine Draufsicht.

Der Spielbereich (zwischen den Banden) wird auf ein Rechteck WARP_W x WARP_H
abgebildet, Verhaeltnis 2:1 wie bei jedem Pooltisch. Alle Erkennungen arbeiten
in diesem Koordinatensystem, die Anzeige rechnet zurueck ins Kamerabild.
"""

import cv2
import numpy as np

WARP_W, WARP_H = 800, 400
BALL_DIAMETER_M = 0.05715          # Poolkugel 57,15 mm

# Laenge der Spielflaeche (Bande zu Bande) je Tischgroesse
TABLE_SIZES = {"7ft": 1.98, "8ft": 2.24, "9ft": 2.54}


def order_corners(corners):
    """Ecken so drehen, dass die erste Kante (Ecke 0 -> 1) eine LANGE Seite ist.
    Erwartet 4 Punkte im oder gegen den Uhrzeigersinn."""
    pts = [tuple(map(float, c)) for c in corners]
    d01 = np.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
    d12 = np.hypot(pts[2][0] - pts[1][0], pts[2][1] - pts[1][1])
    if d12 > d01:
        pts = pts[1:] + pts[:1]
    return pts


class Table:
    def __init__(self, corners=None, length_m: float = TABLE_SIZES["8ft"]):
        self.length_m = length_m
        self.H = self.Hinv = None
        self.corners = None
        if corners:
            self.set_corners(corners)

    def set_corners(self, corners):
        self.corners = order_corners(corners)
        src = np.float32(self.corners)
        dst = np.float32([[0, 0], [WARP_W, 0], [WARP_W, WARP_H], [0, WARP_H]])
        self.H = cv2.getPerspectiveTransform(src, dst)
        self.Hinv = np.linalg.inv(self.H)

    @property
    def calibrated(self) -> bool:
        return self.H is not None

    @property
    def ball_radius_px(self) -> float:
        return WARP_W * (BALL_DIAMETER_M / 2) / self.length_m

    @property
    def pockets(self):
        return [(0, 0), (WARP_W / 2, 0), (WARP_W, 0),
                (0, WARP_H), (WARP_W / 2, WARP_H), (WARP_W, WARP_H)]

    def warp(self, frame):
        return cv2.warpPerspective(frame, self.H, (WARP_W, WARP_H), flags=cv2.INTER_LINEAR)

    def to_camera(self, points):
        """Punkte aus der Draufsicht zurueck ins Kamerabild."""
        if not len(points):
            return np.zeros((0, 2), np.float32)
        p = np.float32(points).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(p, self.Hinv).reshape(-1, 2)
