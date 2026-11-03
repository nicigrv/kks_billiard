"""
Simulator: erzeugt Kamerabilder eines Pooltisches mit bekannten Kugelpositionen.

Bewusst NICHT geschoent: Schatten, Glanzlichter, ungleichmaessige Beleuchtung,
schraeger Kamerawinkel, Sensorrauschen und JPEG-Artefakte wie bei der ESP32-CAM.
Dient zum Testen der Erkennung und der Spiellogik ohne echten Tisch.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

# BGR-Farben der Kugeln 1-7 (9-15 gleiche Farbe als Streifen)
BALL_COLORS = {
    1: (0, 200, 250),    # gelb
    2: (190, 70, 10),    # blau
    3: (40, 40, 215),    # rot
    4: (130, 40, 110),   # violett
    5: (0, 120, 250),    # orange
    6: (50, 130, 10),    # gruen (schwierig auf gruenem Tuch!)
    7: (30, 30, 120),    # weinrot
}
CLOTHS = {"gruen": (40, 115, 25), "blau": (150, 90, 20)}

TOP_W, TOP_H = 1600, 800        # interne Aufloesung der Draufsicht (2x Warp)


@dataclass
class SimBall:
    number: int          # 0 = weisse, 8 = schwarze, 1-7 volle, 9-15 halbe
    x: float             # Position in Metern auf der Spielflaeche
    y: float
    look: int = -1       # bestimmt Drehung/Lage (Streifen, Nummer); -1 = zufaellig

    def __post_init__(self):
        if self.look < 0:
            self.look = int(np.random.default_rng().integers(1 << 30))

    @property
    def kind(self):
        if self.number == 0:
            return "cue"
        if self.number == 8:
            return "eight"
        return "solid" if self.number < 8 else "stripe"


def rack_positions(length_m, width_m, d=0.05715):
    """15 Kugeln im Dreieck, Spitze auf dem Fusspunkt, korrekt gemischt (8 in der Mitte)."""
    foot_x = length_m * 0.75
    cy = width_m / 2
    order = [1, 9, 2, 10, 8, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15]
    pos, i = [], 0
    for row in range(5):
        for k in range(row + 1):
            x = foot_x + row * d * math.sqrt(3) / 2
            y = cy + (k - row / 2) * d
            pos.append(SimBall(order[i], x, y))
            i += 1
    return pos


def random_layout(rng, numbers, length_m, width_m, min_gap=0.002, cluster_prob=0.0):
    """Kugeln zufaellig verteilen, ohne Ueberlappung. cluster_prob: Anteil Kugeln,
    die direkt an eine andere Kugel gelegt werden (beruehrend)."""
    d = 0.05715
    balls = []
    for n in numbers:
        for _ in range(500):
            if balls and rng.random() < cluster_prob:
                ref = balls[rng.integers(len(balls))]
                a = rng.random() * 2 * math.pi
                x, y = ref.x + math.cos(a) * (d + min_gap), ref.y + math.sin(a) * (d + min_gap)
            else:
                x = rng.uniform(d / 2 + 0.005, length_m - d / 2 - 0.005)
                y = rng.uniform(d / 2 + 0.005, width_m - d / 2 - 0.005)
            if not (d / 2 < x < length_m - d / 2 and d / 2 < y < width_m - d / 2):
                continue
            if all(math.hypot(x - b.x, y - b.y) >= d + min_gap for b in balls):
                balls.append(SimBall(n, x, y))
                break
    return balls


def _draw_ball(img, cx, cy, r, number, rng):
    yy, xx = np.mgrid[-r - 2:r + 3, -r - 2:r + 3]
    x0, y0 = int(cx) - r - 2, int(cy) - r - 2
    h, w = yy.shape
    if x0 < 0 or y0 < 0 or x0 + w > img.shape[1] or y0 + h > img.shape[0]:
        return
    fx, fy = xx + (int(cx) - cx), yy + (int(cy) - cy)
    dist = np.sqrt(fx ** 2 + fy ** 2)
    inside = dist <= r
    white = np.array((235, 235, 230), np.float32)

    base = np.zeros((h, w, 3), np.float32)
    if number == 0:
        base[:] = white
    elif number == 8:
        base[:] = (22, 22, 22)
    else:
        color = np.array(BALL_COLORS[number if number < 8 else number - 8], np.float32)
        if number < 8:
            base[:] = color
        else:
            # Streifenband in zufaelliger Lage: je nach Drehung sieht man viel oder wenig Weiss
            ang = rng.random() * math.pi
            off = rng.uniform(-0.55, 0.55) * r
            band = np.abs(fx * math.cos(ang) + fy * math.sin(ang) - off) < 0.55 * r
            base[:] = white
            base[band] = color
    if number != 0:
        # weisser Nummernkreis, der von oben je nach Lage sichtbar ist
        a = rng.random() * 2 * math.pi
        rr = rng.uniform(0, 0.6) * r
        nc = (dist <= r) & (np.hypot(fx - math.cos(a) * rr, fy - math.sin(a) * rr) < 0.3 * r)
        base[nc] = white

    # Kugel-Schattierung (Rand dunkler) + Glanzlicht
    shade = 1.0 - 0.45 * np.clip(dist / r, 0, 1) ** 2
    base *= shade[..., None]
    hl = np.exp(-(((fx + 0.3 * r) ** 2 + (fy + 0.3 * r) ** 2) / (0.12 * r * r)))
    base = base * (1 - 0.8 * hl[..., None]) + 255 * 0.8 * hl[..., None]

    roi = img[y0:y0 + h, x0:x0 + w].astype(np.float32)
    aa = np.clip(r + 0.5 - dist, 0, 1)[..., None]          # weiche Kante
    roi = roi * (1 - aa) + base * aa
    img[y0:y0 + h, x0:x0 + w] = np.clip(roi, 0, 255).astype(np.uint8)


def render_topdown(balls, length_m, width_m, cloth="gruen", rng=None, occluder=None):
    """Draufsicht der Spielflaeche. occluder: (x, y, radius_m) z. B. ein Arm ueber dem Tisch."""
    rng = rng or np.random.default_rng()
    img = np.zeros((TOP_H, TOP_W, 3), np.float32)
    img[:] = CLOTHS[cloth]
    # Tuchstruktur + ungleichmaessiges Licht (Lampe ueber der Mitte)
    img += rng.normal(0, 4, img.shape)
    yy, xx = np.mgrid[0:TOP_H, 0:TOP_W]
    light = 1.08 - 0.30 * (((xx - TOP_W * 0.55) / TOP_W) ** 2 + ((yy - TOP_H * 0.45) / TOP_H) ** 2) * 2.2
    img *= light[..., None]
    img = np.clip(img, 0, 255).astype(np.uint8)

    sx, sy = TOP_W / length_m, TOP_H / width_m
    r = int(round(0.05715 / 2 * sx))
    # Schatten
    shadow = np.zeros((TOP_H, TOP_W), np.float32)
    for b in balls:
        cv2.circle(shadow, (int(b.x * sx + r * 0.25), int(b.y * sy + r * 0.25)), int(r * 1.05), 1.0, -1)
    shadow = cv2.GaussianBlur(shadow, (0, 0), r * 0.35)
    img = (img.astype(np.float32) * (1 - 0.45 * shadow[..., None])).astype(np.uint8)
    for b in balls:
        _draw_ball(img, b.x * sx, b.y * sy, r, b.number, np.random.default_rng(b.look))
    if occluder:
        ox, oy, orad = occluder
        cv2.ellipse(img, (int(ox * sx), int(oy * sy)), (int(orad * sx), int(orad * sy * 0.45)),
                    20, 0, 360, (90, 120, 170), -1)       # hautfarbener Arm
    return img


def to_camera(topdown, corners, cam_size=(800, 600), border_color=(30, 50, 90)):
    """Draufsicht schraeg ins Kamerabild projizieren, Bande + Rauschen + JPEG-Artefakte."""
    W, H = cam_size
    src = np.float32([[0, 0], [TOP_W, 0], [TOP_W, TOP_H], [0, TOP_H]])
    M = cv2.getPerspectiveTransform(src, np.float32(corners))
    cam = np.zeros((H, W, 3), np.uint8)
    cam[:] = border_color
    # Bande/Holz um die Spielflaeche
    c = np.float32(corners)
    center = c.mean(axis=0)
    outer = (c - center) * 1.10 + center
    cv2.fillPoly(cam, [outer.astype(np.int32)], (20, 70, 15))
    warped = cv2.warpPerspective(topdown, M, (W, H), flags=cv2.INTER_AREA)
    mask = cv2.warpPerspective(np.full((TOP_H, TOP_W), 255, np.uint8), M, (W, H))
    cam[mask > 0] = warped[mask > 0]
    noise = np.random.default_rng().normal(0, 3, cam.shape)
    cam = np.clip(cam.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    ok, jpg = cv2.imencode(".jpg", cam, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return cv2.imdecode(jpg, cv2.IMREAD_COLOR)


# Typische Kamerasicht: leicht schraeg, Tisch fuellt das Bild nicht ganz
DEFAULT_CORNERS = [(70, 150), (740, 120), (765, 470), (45, 480)]
