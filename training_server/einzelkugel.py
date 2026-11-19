"""
Bilderkennung fuer Einzelkugel-Bilder: Wo liegt die (eine) Kugel? Ohne Menschen.

Der Pi schickt das Bild (entzerrte Draufsicht), die Kugelnummer (am Pi gewaehlt) und - falls gemerkt -
ein Bild des LEEREN Tisches. Gesucht ist nur noch die Stelle der Kugel:

  1. Bezug: der leere Tisch, an das Bild angeglichen
       - Verschiebung (Kamera leicht verrutscht) per Phasenkorrelation,
       - Helligkeit und Schatten (Belichtung, Person am Tisch) per Verhaeltnis-Karte in grossen Zellen;
         Zellen mit fremder Farbe (Hand, Arm) zaehlen dabei nicht, sie werden aus den Nachbarn gefuellt;
     ohne leeren Tisch: die Tuchfarbe der Umgebung (Median in grossen Zellen, genauso ohne Hand).
  2. Was davon deutlich abweicht, ist "veraendert" (Schwelle passt sich dem Bildrauschen an).
  3. Kugel = eine Stelle, an der eine Kreisscheibe mit Kugelradius fast ganz veraendert ist und der
     Ring drumherum kaum. So zaehlen Hand, Arm, Queue oder Schattenkanten nicht als Kugel - und sie
     stoeren auch nicht mehr, wenn die Kugel daneben frei liegt.
  4. Mehrere kugelaehnliche Stellen: die Farbe entscheidet (die Nummer ist ja bekannt, Stufe 2 + 3
     aus kugel_merkmale). Bleibt es unklar, gilt das Bild als nicht erkannt.

finde_kugel() -> (Liste [(x, y)], Grund) - genau ein Eintrag = sicher gefunden, Grund dann "".
Braucht OpenCV + numpy (+ Pillow fuer die Vorschau), alles im Dockerfile installiert.
"""

import io
import math

import cv2
import numpy as np
from PIL import Image, ImageDraw

SCHWELLE_LEER = 18     # Farbabstand (RGB) zum angeglichenen leeren Tisch, ab dem ein Pixel "veraendert" ist
SCHWELLE_OHNE = 30     # ohne leeren Tisch: Abstand zur Tuchfarbe der Umgebung (weniger genau, daher hoeher)
MIN_FUELLUNG = 0.5     # so viel der Kugelscheibe muss veraendert sein
MAX_RING = 0.25        # so viel darf der Ring um die Kugel (1,6 - 2,3 Radien) veraendert sein
MAX_ANTEIL = 0.35      # mehr veraendert -> Bild passt gar nicht zum leeren Tisch
FARBE_VORSPRUNG = 3.0  # mehrere Stellen: die passende Farbe muss so viel wahrscheinlicher sein

_klassifizierer = None


def _start_klassifizierer():
    """Farben (Stufe 2 + 3) - Startwerte aus dem Simulator, nur zum Unterscheiden mehrerer Stellen."""
    global _klassifizierer
    if _klassifizierer is None:
        try:
            import os
            import kugel_merkmale as km
            hier = os.path.dirname(os.path.abspath(__file__))
            _klassifizierer = km.BallClassifier.load(os.path.join(hier, "start_merkmale.json"))
        except Exception:                                   # ohne Farben geht es auch, nur strenger
            _klassifizierer = False
    return _klassifizierer or None


def _bild(jpg):
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("kein lesbares Bild")
    return img


def _gitter(a, zelle):
    """Median je Zelle (eine Kugel fuellt hoechstens ein Fuenftel davon) -> (gitter, zelle)."""
    h, w = a.shape[:2]
    zelle = max(4, int(zelle))
    gh, gw = max(1, math.ceil(h / zelle)), max(1, math.ceil(w / zelle))
    p = np.pad(a, ((0, gh * zelle - h), (0, gw * zelle - w), (0, 0)), mode="edge")
    return np.median(p.reshape(gh, zelle, gw, zelle, a.shape[2]), axis=(1, 3)).astype(np.float32), zelle


def _fuellen(gitter, ok):
    """Zellen, die nicht zum Tuch passen (Hand, Arm, Queue), aus den Nachbarn auffuellen - sonst
    wuerde der Verlauf die Hand "wegrechnen" und um sie herum einen falschen Rand erzeugen."""
    if ok.all() or not ok.any():
        return gitter
    g, ok = gitter.copy(), ok.copy()
    kern = np.ones((3, 3), np.float32)
    while not ok.all():
        okf = ok.astype(np.float32)
        w = cv2.filter2D(okf, -1, kern, borderType=cv2.BORDER_CONSTANT)
        summe = np.stack([cv2.filter2D(g[..., c] * okf, -1, kern, borderType=cv2.BORDER_CONSTANT)
                          for c in range(g.shape[2])], axis=2)
        neu = ~ok & (w > 0)
        g[neu] = summe[neu] / w[neu][:, None]
        ok = ok | neu
    return g


def _hoch(gitter, zelle, h, w):
    """Gitter weich auf Bildgroesse bringen."""
    gh, gw = gitter.shape[:2]
    return cv2.resize(gitter, (gw * zelle, gh * zelle), interpolation=cv2.INTER_LINEAR)[:h, :w]


def _tuchfarbe(img, radius):
    """Ohne leeren Tisch: Tuchfarbe der Umgebung. Zellen mit fremder Farbe (Hand ...) zaehlen nicht."""
    a = img.astype(np.float32)
    gitter, zelle = _gitter(a, 4 * radius)
    tuch = np.median(gitter.reshape(-1, 3), axis=0)
    summe = max(float(tuch.sum()), 1.0)
    hell = gitter.sum(axis=2) / summe
    anteil = gitter / np.maximum(gitter.sum(axis=2, keepdims=True), 1.0)
    ok = (np.abs(anteil - tuch / summe).max(axis=2) < 0.08) & (hell > 0.35) & (hell < 1.8)
    return _hoch(_fuellen(gitter, ok), zelle, *img.shape[:2])


def _angleichen(img, leer, radius):
    """Leeren Tisch auf das Bild ausrichten (Verschiebung) und an Helligkeit/Schatten anpassen."""
    g1 = cv2.cvtColor(leer, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g2 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    fenster = cv2.createHanningWindow((g1.shape[1], g1.shape[0]), cv2.CV_32F)
    (dx, dy), guete = cv2.phaseCorrelate(g1, g2, fenster)
    if guete > 0.05 and math.hypot(dx, dy) <= 3 * radius:
        m = np.float32([[1, 0, dx], [0, 1, dy]])
        leer = cv2.warpAffine(leer, m, (leer.shape[1], leer.shape[0]), borderMode=cv2.BORDER_REPLICATE)
    a, b = img.astype(np.float32), leer.astype(np.float32)
    b = b * np.median(((a + 1) / (b + 1)).reshape(-1, 3), axis=0)   # Belichtung + Weissabgleich im Ganzen
    gitter, zelle = _gitter((a + 1) / (b + 1), 4 * radius)            # Schatten, ungleiches Licht: je Zelle
    # Licht und Schatten aendern alle Farben ungefaehr gleich - eine Hand auf blauem Tuch nicht
    m = gitter.mean(axis=2)
    ok = (gitter.max(axis=2) / np.maximum(gitter.min(axis=2), 1e-3) < 1.3) & (m > 0.35) & (m < 1.8)
    return b * _hoch(_fuellen(gitter, ok), zelle, *img.shape[:2])


def _maske(img, bezug, schwelle, radius):
    diff = np.sqrt(((img.astype(np.float32) - bezug) ** 2).sum(axis=2))
    mitte = float(np.median(diff))
    streu = 1.4826 * float(np.median(np.abs(diff - mitte)))
    m = (diff > max(schwelle, mitte + 8 * streu)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))           # Rauschen weg
    k = max(3, int(radius * 0.6) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))  # Kugel schliessen
    return m


def _scheibe(r_innen, r_aussen):
    n = int(math.ceil(r_aussen)) * 2 + 1
    c = n // 2
    yy, xx = np.mgrid[0:n, 0:n]
    d = np.hypot(xx - c, yy - c)
    k = ((d <= r_aussen) & (d >= r_innen)).astype(np.float32)
    return k / k.sum()


def _stellen(m, radius, bild=None):
    """Kugelaehnliche Flecken in der Maske -> [(x, y, fuellung, ring)], beste zuerst.
    Jeder Fleck fuer sich: so gross wie eine Kugel, rund (Scheibe fast voll, Ring drumherum fast leer).
    Grosses (Hand, Arm, Queue, Schattenkante) faellt weg - und stoert eine frei liegende Kugel nicht."""
    h, w = m.shape
    flaeche = math.pi * radius * radius
    scheibe, ringk = _scheibe(0, radius), _scheibe(1.6 * radius, 2.3 * radius)
    rand = int(math.ceil(2.4 * radius)) + 1
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        x0, y0, bw, bh, area = (int(v) for v in stats[i])
        if not 0.3 * flaeche <= area <= 3.2 * flaeche or bw > 4.5 * radius or bh > 4.5 * radius:
            continue
        if not 0.45 < bw / max(bh, 1) < 2.2:
            continue
        xa, ya = max(0, x0 - rand), max(0, y0 - rand)
        xb, yb = min(w, x0 + bw + rand), min(h, y0 + bh + rand)
        teil = (lab[ya:yb, xa:xb] == i).astype(np.float32)
        fuell = cv2.filter2D(teil, -1, scheibe, borderType=cv2.BORDER_CONSTANT)
        iy, ix = np.unravel_index(int(np.argmax(fuell)), fuell.shape)
        f = float(fuell[iy, ix])
        r = float(cv2.filter2D(teil, -1, ringk, borderType=cv2.BORDER_CONSTANT)[iy, ix])
        if f < MIN_FUELLUNG or r > MAX_RING:
            continue
        yy, xx = np.mgrid[0:teil.shape[0], 0:teil.shape[1]]
        x, y = float(ix), float(iy)
        for _ in range(2):                                  # Mitte genauer: Schwerpunkt der Kugelpixel
            sel = (np.hypot(xx - x, yy - y) <= radius) & (teil > 0)
            if sel.sum() < 5:
                break
            x, y = float(xx[sel].mean()), float(yy[sel].mean())
        x, y = x + xa, y + ya
        if bild is not None:
            x, y = _rand_mitte(bild, x, y, radius)
        if min(x, y, w - 1 - x, h - 1 - y) < 0.4 * radius:
            continue                                        # Taschen-/Randschatten
        out.append((x, y, f, r))
    out.sort(key=lambda s: s[3] - s[2])
    eigene = []                                             # dicht beieinander = dieselbe Kugel
    for s in out:
        if all(math.dist(s[:2], e[:2]) > 1.5 * radius for e in eigene):
            eigene.append(s)
    return eigene


def _rand_mitte(bild, x, y, radius):
    """Mitte am Rand der Kugel nachschaerfen (Kreissuche im Ausschnitt). Hilft, wenn die Kugel dem Tuch
    aehnelt (blaue 2 auf blauem Tuch) und ihr Schatten deutlicher ist als sie selbst."""
    s = int(math.ceil(2.2 * radius))
    x0, y0 = int(round(x)) - s, int(round(y)) - s
    if x0 < 0 or y0 < 0 or x0 + 2 * s + 1 > bild.shape[1] or y0 + 2 * s + 1 > bild.shape[0]:
        return x, y
    grau = cv2.GaussianBlur(cv2.cvtColor(bild[y0:y0 + 2 * s + 1, x0:x0 + 2 * s + 1], cv2.COLOR_BGR2GRAY), (3, 3), 0)
    kreise = cv2.HoughCircles(grau, cv2.HOUGH_GRADIENT, 1, radius, param1=60, param2=max(6, int(radius * 0.9)),
                              minRadius=max(2, int(radius * 0.8)), maxRadius=int(math.ceil(radius * 1.2)))
    if kreise is None:
        return x, y
    kx, ky, _ = kreise[0][0]
    if math.hypot(kx + x0 - x, ky + y0 - y) > 0.7 * radius:
        return x, y
    return float(kx + x0), float(ky + y0)


def _passt(klassifizierer, img, x, y, radius, nummer):
    """Wie gut passt die Stelle zur bekannten Nummer? Farbe (1-7, 9-15) bzw. Art (Weisse, 8)."""
    _, art, farbe = klassifizierer.classify(img, x, y, radius)
    if nummer == 0:
        return float(art["cue"])
    if nummer == 8:
        return float(art["eight"])
    import kugel_merkmale as km
    return float(farbe[km.split_number(nummer)[1]])


def _versuch(img, bezug, schwelle, radius, nummer, klassifizierer):
    m = _maske(img, bezug, schwelle, radius)
    if m.mean() > MAX_ANTEIL:
        return [], "Bild passt nicht zum leeren Tisch (Licht oder Kamera ganz anders)"
    st = _stellen(m, radius, img)
    if not st:
        return [], "keine Kugel gefunden"
    if len(st) == 1:
        return [(round(st[0][0], 1), round(st[0][1], 1))], ""
    if nummer is not None and klassifizierer is not None:
        p = sorted(((_passt(klassifizierer, img, s[0], s[1], radius, nummer), s) for s in st), key=lambda t: -t[0])
        if p[0][0] >= 0.3 and p[0][0] >= FARBE_VORSPRUNG * p[1][0]:
            return [(round(p[0][1][0], 1), round(p[0][1][1], 1))], ""
    return [(round(s[0], 1), round(s[1], 1)) for s in st], f"{len(st)} kugelähnliche Stellen – nicht eindeutig"


def finde_kugel(jpg, leer_jpg, radius, nummer=None, klassifizierer=None):
    """Kugel im Bild finden. radius in Pixeln (vom Pi), nummer 0-15 (fuer die Farbe). -> ([(x, y)], Grund)"""
    img = _bild(jpg)
    radius = max(2.0, float(radius))
    if klassifizierer is None:
        klassifizierer = _start_klassifizierer()
    versuche = []
    if leer_jpg:
        leer = _bild(leer_jpg)
        if leer.shape == img.shape:
            versuche.append((_angleichen(img, leer, radius), SCHWELLE_LEER))
    # ohne (passenden) leeren Tisch - oder als zweiter Versuch: Tuchfarbe der Umgebung
    versuche.append((_tuchfarbe(img, radius), SCHWELLE_OHNE))
    erstes = None
    for bezug, schwelle in versuche:
        kugeln, grund = _versuch(img, bezug, schwelle, radius, nummer, klassifizierer)
        if len(kugeln) == 1:
            return kugeln, ""
        erstes = erstes or (kugeln, grund)
    return erstes


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
