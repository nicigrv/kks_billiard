"""
Einfache Bilderkennung fuer Einzelkugel-Bilder: Wo liegt die (eine) Kugel?

Der Pi schickt das Bild (entzerrte Draufsicht) mit der Kugelnummer und - falls gemerkt - ein
Bild des LEEREN Tisches. Was sich gegenueber dem leeren Tisch veraendert hat und so gross wie
eine Kugel ist, ist die Kugel. Ohne leeren Tisch: alles, was deutlich anders aussieht als die
mittlere Tuchfarbe.

Ergebnis von finde_kugel(): (Liste [(x, y)], Grund) - genau ein Eintrag = sicher gefunden.
Braucht Pillow und numpy (im Dockerfile installiert).
"""

import io
import math
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SCHWELLE_LEER = 18     # Farbabstand (RGB) zum leeren Tisch, ab dem ein Pixel "Kugel" ist
SCHWELLE_OHNE = 30     # ohne leeren Tisch: Abstand zur Umgebung (weniger genau, daher hoeher)
MAX_PIXEL = 60000      # mehr veraenderte Pixel -> etwas Grosses im Bild (Hand, Arm)


def _laden(jpg):
    return Image.open(io.BytesIO(jpg)).convert("RGB")


def _arr(img):
    return np.asarray(img.filter(ImageFilter.BoxBlur(2)), dtype=np.float32)


def _maske(img, leer, radius):
    a = _arr(img)
    if leer is not None and leer.size == img.size:
        ref, schwelle = _arr(leer), SCHWELLE_LEER
    else:
        # Umgebung jedes Pixels (grosser Weichzeichner): gleicht ungleichmaessiges Licht aus
        ref, schwelle = np.asarray(img.filter(ImageFilter.BoxBlur(max(8, int(4 * radius)))), dtype=np.float32), SCHWELLE_OHNE
    diff = np.sqrt(((a - ref) ** 2).sum(axis=2))
    m = diff > schwelle
    # einzelne Ausreisser weg (Rauschen): Pixel braucht mindestens 3 gesetzte Nachbarn
    p = np.pad(m, 1)
    nb = sum(p[1 + dy:p.shape[0] - 1 + dy, 1 + dx:p.shape[1] - 1 + dx]
             for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
    return m & (nb >= 3)


def _zusammenfassen(flecken, radius):
    """Teile einer Kugel (z. B. halbe Kugel, Band so blau wie das Tuch) zu einem Fleck vereinen."""
    gruppen = []
    for f in flecken:
        for g in gruppen:
            if any(math.dist((f[1], f[2]), (h[1], h[2])) < 1.6 * radius for h in g):
                g.append(f)
                break
        else:
            gruppen.append([f])
    out = []
    for g in gruppen:
        a = sum(f[0] for f in g)
        cx = sum(f[0] * f[1] for f in g) / a
        cy = sum(f[0] * f[2] for f in g) / a
        x0 = min(f[1] - f[3] / 2 for f in g)
        x1 = max(f[1] + f[3] / 2 for f in g)
        y0 = min(f[2] - f[4] / 2 for f in g)
        y1 = max(f[2] + f[4] / 2 for f in g)
        out.append((a, cx, cy, x1 - x0, y1 - y0))
    return out


def _flecken(m):
    """Zusammenhaengende Flecken (8er-Nachbarschaft) -> [(flaeche, cx, cy, breite, hoehe)]."""
    h, w = m.shape
    ys, xs = np.nonzero(m)
    offen = set(zip(ys.tolist(), xs.tolist()))
    out = []
    while offen:
        start = offen.pop()
        q = deque([start])
        pts = [start]
        while q:
            y, x = q.popleft()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    n = (y + dy, x + dx)
                    if n in offen:
                        offen.remove(n)
                        q.append(n)
                        pts.append(n)
        a = np.array(pts)
        out.append((len(pts), float(a[:, 1].mean()), float(a[:, 0].mean()),
                    int(a[:, 1].max() - a[:, 1].min() + 1), int(a[:, 0].max() - a[:, 0].min() + 1)))
    return out


def finde_kugel(jpg, leer_jpg, radius):
    """Kugel(n) im Bild finden. radius in Pixeln (vom Pi). -> ([(x, y)], Grund)"""
    img = _laden(jpg)
    leer = _laden(leer_jpg) if leer_jpg else None
    m = _maske(img, leer, radius)
    if int(m.sum()) > MAX_PIXEL:
        return [], "zu viel verändert (Hand/Arm im Bild oder Licht anders als beim leeren Tisch)"
    h, w = m.shape
    flaeche_kugel = math.pi * radius * radius
    kugeln, gross = [], 0
    teile = [f for f in _flecken(m) if f[0] >= 0.05 * flaeche_kugel]
    for area, cx, cy, bw, bh in _zusammenfassen(teile, radius):
        if area < 0.3 * flaeche_kugel:
            continue                                   # Rauschen
        if area > 3.0 * flaeche_kugel or bw > 4.5 * radius or bh > 4.5 * radius:
            gross += 1                                 # Hand, Arm, Queue ...
            continue
        if min(cx, cy, w - cx, h - cy) < 0.4 * radius:
            continue                                   # Taschen-/Randschatten
        if not 0.45 < bw / max(bh, 1) < 2.2:
            continue
        kugeln.append((round(cx, 1), round(cy, 1)))
    if gross:
        return [], "etwas Großes im Bild (Hand/Arm?)"
    if not kugeln:
        return [], "keine Kugel gefunden"
    if len(kugeln) > 1:
        return kugeln, f"{len(kugeln)} Kugeln gefunden"
    return kugeln, ""


def mit_kreisen(jpg, kugeln, radius):
    """Vorschau fuer den Admin: Kreise um die festgelegten Kugeln, Nummer daneben."""
    img = Image.open(io.BytesIO(jpg)).convert("RGB")
    d = ImageDraw.Draw(img)
    r = radius * 1.6
    for b in kugeln:
        x, y = float(b["x"]), float(b["y"])
        d.ellipse((x - r, y - r, x + r, y + r), outline=(80, 230, 120), width=2)
        n = b.get("number")
        if n is not None:
            d.text((x + r + 2, y - r), "W" if n == 0 else str(n), fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()
