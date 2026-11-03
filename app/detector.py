"""
Kugelerkennung mit klassischer Bildverarbeitung (OpenCV) auf der entzerrten Draufsicht.

Vorgehen:
  1. Tuchfarbe automatisch schaetzen (Median, das Tuch ist der groesste Bereich).
  2. Alles, was deutlich anders aussieht als Tuch, ist "Nicht-Tuch" (Kugeln, Arme, Queue).
  3. Kreis-Kandidaten aus zwei Quellen: Hough-Kreise (gut bei beruehrenden Kugeln)
     und Spitzen der Distanztransformation (gut bei kontrastarmen Kugeln).
  4. Kandidat gilt nur, wenn der Kreis fast komplett aus Nicht-Tuch besteht.
  5. Klassifikation ueber Weiss-/Schwarz-/Farbanteil. Weisse und 8 werden NICHT per
     Schwellwert vergeben, sondern an die weisseste bzw. schwaerzeste Kugel -
     es gibt jede genau einmal. Das ist deutlich robuster.

Ausgabe-Schnittstelle (gleich fuer einen spaeteren Coral-Detektor):
    detect(warped_bgr) -> (list[Ball], DetectInfo)
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

KINDS = ("cue", "eight", "solid", "stripe")


@dataclass
class Ball:
    x: float
    y: float
    r: float
    kind: str
    color: str = ""
    white: float = 0.0     # Anteile, hilfreich fuer Debug/Training
    black: float = 0.0
    votes: object = None   # Counter der Typ-Stimmen ueber viele Bilder (nur in stabilen Zustaenden)


@dataclass
class DetectInfo:
    occlusion: float = 0.0           # Anteil Tischflaeche, der weder Tuch noch Kugel ist
    cloth_lab: tuple = (0, 0, 0)
    candidates: int = 0
    extra: dict = field(default_factory=dict)


def _hue_name(h, v):
    if v < 150 and (h < 12 or h > 165):   # Weinrot = dunkles Rot
        return "weinrot"
    if h < 8 or h > 168:
        return "rot"
    if h < 20:
        return "orange"
    if h < 36:
        return "gelb"
    if h < 85:
        return "gruen"
    if h < 128:
        return "blau"
    return "violett"


class ClassicDetector:
    name = "opencv"

    def __init__(self, radius_px: float, cloth_threshold: float = 22.0):
        self.r = float(radius_px)
        self.cloth_threshold = cloth_threshold
        r = int(round(self.r))
        k = np.zeros((2 * r + 1, 2 * r + 1), np.uint8)
        cv2.circle(k, (r, r), max(1, int(round(r * 0.8))), 1, -1)
        self._disk = k
        self._disk_area = float(k.sum())
        self._ring_angles = np.linspace(0, 2 * np.pi, 32, endpoint=False)
        # Grenzwerte (aus Simulator-Messungen, siehe sim/test_detector.py)
        self.fill_strong = 0.72     # klar sichtbar (anders als Tuch)
        self.fill_weak = 0.25       # tuchaehnlich, dann muss der Rand ueberzeugen
        self.ring_factor = 3.5      # Randkante x-mal staerker als typische Tuchkante
        # voll/halb: Weissanteil im AEUSSEREN RING (0,5-0,95 r). Die weissen Kappen einer
        # halben Kugel liegen am Rand, Nummernkreis und Glanzlicht einer vollen eher innen.
        # Systematisch gesucht (bestes von 30 Merkmalen): 98 % richtig im Simulator.
        self.stripe_white = 0.17
        self.white_sat = 100        # max. Saettigung, die noch als "weiss" gilt (Kantenmischung!)

    # ------------------------------------------------------------------ Masken
    def _noncloth_mask(self, blur):
        lab = cv2.cvtColor(blur, cv2.COLOR_BGR2LAB).astype(np.float32)
        # Tuchfarbe nur alle paar Bilder neu schaetzen (Licht aendert sich langsam, spart Zeit)
        self._frame_no = getattr(self, "_frame_no", 0) + 1
        if getattr(self, "_cloth", None) is None or self._frame_no % 15 == 1:
            self._cloth = np.median(lab[::6, ::6].reshape(-1, 3), axis=0)
        cloth = self._cloth
        # Helligkeit schwaecher gewichten: Schatten und Lichtabfall sind kein "Nicht-Tuch"
        d = lab - cloth
        dist = np.sqrt((0.55 * d[..., 0]) ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
        mask = (dist > self.cloth_threshold).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        return mask, dist, tuple(float(c) for c in cloth)

    # ------------------------------------------------------------- Kandidaten
    def _candidates(self, gray, mask):
        """Liefert (x, y, quelle). quelle 'h' = Hough-Kreis, 'd' = Distanz-Spitze."""
        r = self.r
        cands = []
        circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1, minDist=1.4 * r,
                                   param1=90, param2=11,
                                   minRadius=int(r * 0.75), maxRadius=int(np.ceil(r * 1.25)))
        if circles is not None:
            cands += [(float(x), float(y), "h") for x, y, _ in circles[0]]

        dt = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        k = int(r) | 1
        peaks = (dt == cv2.dilate(dt, np.ones((k, k), np.uint8))) & (dt >= 0.55 * r)
        ys, xs = np.nonzero(peaks)
        cands += [(float(x), float(y), "d") for x, y in zip(xs, ys)]

        # Helligkeits-Flecken in Kugelgroesse (Difference of Gaussians). Findet Kugeln
        # in dichten Gruppen (Rack), weil jede Kugel durch die Schattierung in der Mitte
        # anders hell ist als an ihrem Rand - auch wenn sie andere Kugeln beruehrt.
        # Nur HELLE Flecken: Kugelmitten sind heller als ihr Rand, Luecken zwischen
        # Kugeln dagegen dunkel (sonst landen Kandidaten in den Luecken).
        # Auf halber Aufloesung rechnen: die grossen Weichzeichner sind sonst der teuerste Schritt
        h0, w0 = gray.shape
        small = cv2.resize(gray, (w0 // 2, h0 // 2), interpolation=cv2.INTER_AREA).astype(np.float32)
        dog_s = cv2.GaussianBlur(small, (0, 0), 0.225 * r) - cv2.GaussianBlur(small, (0, 0), 0.65 * r)
        dog = cv2.resize(dog_s, (w0, h0), interpolation=cv2.INTER_LINEAR)
        self._dog = dog
        kk = int(r * 1.2) | 1
        peaks = (dog == cv2.dilate(dog, np.ones((kk, kk), np.uint8))) & (dog > 6) & (mask > 0)
        ys, xs = np.nonzero(peaks)
        cands += [(float(x), float(y), "b") for x, y in zip(xs, ys)]

        # Kugelgrosse Flecken, auch wenn sie loechrig sind (tuchaehnliche Kugelfarbe)
        area = np.pi * r * r
        loose = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n, _, stats, cents = cv2.connectedComponentsWithStats(loose, connectivity=8)
        for i in range(1, n):
            a = stats[i, cv2.CC_STAT_AREA]
            if 0.2 * area <= a <= 1.8 * area:
                cands.append((float(cents[i][0]), float(cents[i][1]), "c"))
        return cands

    def _ring_score(self, grad, x, y):
        """Kantenstaerke entlang des erwarteten Kugelrands. Kugeln haben dort
        immer eine Kante (Schattierung, Schatten), auch wenn die Farbe dem Tuch
        aehnelt - z. B. die gruene 6 auf gruenem Tuch."""
        ang = self._ring_angles
        px = np.clip((x + np.cos(ang) * self.r).astype(int), 0, grad.shape[1] - 1)
        py = np.clip((y + np.sin(ang) * self.r).astype(int), 0, grad.shape[0] - 1)
        vals = grad[py, px]
        # Median statt Mittelwert: eine einzelne Kante (Bande, Nachbarkugel) reicht nicht
        return float(np.median(vals))

    def _fill_ratio(self, mask, x, y):
        r = self._disk.shape[0] // 2
        xi, yi = int(round(x)), int(round(y))
        h, w = mask.shape
        x0, y0, x1, y1 = xi - r, yi - r, xi + r + 1, yi + r + 1
        if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
            # am Rand: nur den sichtbaren Teil werten
            sx0, sy0 = max(0, -x0), max(0, -y0)
            sx1, sy1 = self._disk.shape[1] - max(0, x1 - w), self._disk.shape[0] - max(0, y1 - h)
            disk = self._disk[sy0:sy1, sx0:sx1]
            patch = mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
            area = float(disk.sum())
            return float((patch * disk).sum()) / area if area > 0 else 0.0
        return float((mask[y0:y1, x0:x1] * self._disk).sum()) / self._disk_area

    def _refine(self, mask, x, y, iterations=2):
        """Mittelpunkt auf den Schwerpunkt der Kugelflaeche ziehen (Kandidaten liegen
        oft am Glanzlicht oder an einer Kante, nicht in der Mitte)."""
        r = self.r
        h, w = mask.shape
        for _ in range(iterations):
            x0, x1 = max(0, int(x - r)), min(w, int(x + r) + 1)
            y0, y1 = max(0, int(y - r)), min(h, int(y + r) + 1)
            yy, xx = np.mgrid[y0:y1, x0:x1]
            sel = ((xx - x) ** 2 + (yy - y) ** 2 <= r * r) & (mask[y0:y1, x0:x1] > 0)
            if sel.sum() < 0.3 * np.pi * r * r:
                break
            nx, ny = float(xx[sel].mean()), float(yy[sel].mean())
            # nur kleine Korrekturen: in dichten Gruppen nicht in die Nachbarkugel wandern
            if np.hypot(nx - x, ny - y) > 0.4 * r:
                break
            x, y = nx, ny
        return x, y

    # ---------------------------------------------------------- Klassifikation
    def _color_stats(self, hsv, x, y):
        """Weiss im aeusseren Ring (voll/halb), Schwarz/Farbe ueber den Kern."""
        R = self.r * 0.95
        r = self.r * 0.78
        h, w = hsv.shape[:2]
        x0, x1 = max(0, int(x - R)), min(w, int(x + R) + 1)
        y0, y1 = max(0, int(y - R)), min(h, int(y + R) + 1)
        patch = hsv[y0:y1, x0:x1]
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d2 = (xx - x) ** 2 + (yy - y) ** 2
        ring = patch[(d2 <= R * R) & (d2 >= (0.5 * self.r) ** 2)]
        if len(ring) == 0:
            return 0, 0, 0, ""
        white_all = float(((ring[:, 1] < self.white_sat) & (ring[:, 2] > 150)).mean())
        p = patch[d2 <= r * r]
        if len(p) == 0:
            return 0, 0, 0, ""
        H, S, V = p[:, 0].astype(int), p[:, 1].astype(int), p[:, 2].astype(int)
        white = (S < 70) & (V > 150)
        black = (V < 70)
        colored = ~white & ~black & (S >= 70)
        n = float(len(p))
        self._last_white_core = white.sum() / n
        name = ""
        if colored.sum() > 3:
            hc = H[colored]
            # Rot liegt um 0/180 -> zirkulaerer Median ueber Verschiebung
            shifted = (hc + 20) % 180
            hmed = (np.median(shifted) - 20) % 180
            name = _hue_name(hmed, np.median(V[colored]))
        return white_all, black.sum() / n, colored.sum() / n, name

    # ------------------------------------------------------------------- Haupt
    def detect(self, warped):
        blur = cv2.GaussianBlur(warped, (5, 5), 0)
        mask, _, cloth = self._noncloth_mask(blur)
        gray = cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY)
        cands = self._candidates(gray, mask)

        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        grad = cv2.magnitude(gx, gy)
        sub_g, sub_m = grad[::4, ::4], mask[::4, ::4]
        cloth_grad = float(np.median(sub_g[sub_m == 0])) if (sub_m == 0).any() else 1.0
        ring_min = self.ring_factor * max(cloth_grad, 2.0)

        dog = self._dog
        h, w = dog.shape

        def center_bonus(x, y):
            # In dichten Gruppen ist die Fuellung ueberall ~1; dann entscheidet,
            # wie sehr der Punkt nach Kugelmitte aussieht.
            v = dog[min(h - 1, max(0, int(y))), min(w - 1, max(0, int(x)))]
            return 0.004 * float(np.clip(v, -25, 25))

        scored = []
        for x, y, src in cands:
            fr = self._fill_ratio(mask, x, y)
            if fr >= self.fill_strong:
                scored.append((min(fr, 0.95) + center_bonus(x, y), x, y))
            elif fr >= self.fill_weak and self._ring_score(grad, x, y) >= ring_min:
                scored.append((fr * 0.9, x, y))   # leicht abgewertet gegenueber klaren Treffern
        scored.sort(reverse=True)

        accepted = []
        min_d2 = (1.45 * self.r) ** 2
        for fr, x, y in scored:
            if all((x - ax) ** 2 + (y - ay) ** 2 >= min_d2 for _, ax, ay in accepted):
                accepted.append((fr, x, y))

        accepted = [(fr, *self._refine(mask, x, y)) for fr, x, y in accepted]

        hsv = cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)
        balls = []
        for _, x, y in accepted:
            fw, fb, fc, name = self._color_stats(hsv, x, y)
            balls.append(Ball(x, y, self.r, "solid", name, fw, fb))
            balls[-1]._fc = fc

        self._assign_kinds(balls)

        # Verdeckung: Nicht-Tuch-Flaeche, die nicht durch Kugeln erklaert wird
        covered = np.zeros_like(mask)
        for b in balls:
            cv2.circle(covered, (int(b.x), int(b.y)), int(self.r * 1.35), 1, -1)
        unexplained = int((mask & (1 - covered)).sum())
        info = DetectInfo(occlusion=unexplained / mask.size, cloth_lab=cloth, candidates=len(cands))
        return balls, info

    def _assign_kinds(self, balls):
        if not balls:
            return
        # Weisse: hoechster Weissanteil, wenn klar ueberwiegend weiss
        cue = max(balls, key=lambda b: b.white - b._fc)
        if cue.white >= 0.45 and cue._fc < 0.25:
            cue.kind = "cue"
        else:
            cue = None
        # 8: hoechster Schwarzanteil
        rest = [b for b in balls if b is not cue]
        if rest:
            eight = max(rest, key=lambda b: b.black - 0.5 * b._fc)
            if eight.black >= 0.45:
                eight.kind = "eight"
                eight.color = "schwarz"
            else:
                eight = None
        for b in balls:
            if b.kind in ("cue", "eight"):
                continue
            b.kind = "stripe" if b.white >= self.stripe_white else "solid"
        for b in balls:
            if hasattr(b, "_fc"):
                del b._fc
