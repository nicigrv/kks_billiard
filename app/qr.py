"""
QR-Code fuer die Beamer-Ecke. Erzeugt mit OpenCV (cv2.QRCodeEncoder), also ohne Zusatzpaket.
"""

from functools import lru_cache

import cv2
import numpy as np

MAX_LEN = 300


@lru_cache(maxsize=8)
def qr_matrix(text):
    """-> 2D-Array (True = dunkles Modul), ohne Rand; None, wenn es nicht geht."""
    text = (text or "").strip()[:MAX_LEN]
    if not text:
        return None
    try:
        img = cv2.QRCodeEncoder.create().encode(text)
    except (cv2.error, AttributeError):
        return None
    if img is None or img.size == 0:
        return None
    dark = img < 128
    ys, xs = np.nonzero(dark)
    if not len(ys):
        return None
    # OpenCV legt einen schmalen Rand um den Code - abschneiden, den Rand zeichnen wir selbst
    m = dark[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    m.flags.writeable = False
    return m


def qr_png(text, module=8, border=4):
    """QR-Code als PNG (Vorschau im Admin-Panel)."""
    m = qr_matrix(text)
    if m is None:
        return None
    img = np.where(m, 0, 255).astype(np.uint8)
    img = np.pad(img, border, constant_values=255)
    img = cv2.resize(img, None, fx=module, fy=module, interpolation=cv2.INTER_NEAREST)
    ok, png = cv2.imencode(".png", img)
    return png.tobytes() if ok else None
