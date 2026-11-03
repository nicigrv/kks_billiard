"""
Kugelnummern aus erkanntem Typ + Farbe.

Jede Farbe gibt es genau einmal voll (1-7) und einmal halb (9-15), dazu die 8 (schwarz)
und die Weisse (0). Ist die Farbe nicht lesbar, bleibt die Nummer unbekannt (None).
"""

COLOR_NUMBER = {"gelb": 1, "blau": 2, "rot": 3, "violett": 4, "orange": 5, "gruen": 6, "weinrot": 7}
SOLIDS = tuple(range(1, 8))
STRIPES = tuple(range(9, 16))


def ball_number(kind, color):
    if kind == "cue":
        return 0
    if kind == "eight":
        return 8
    n = COLOR_NUMBER.get(color or "")
    if n is None or kind not in ("solid", "stripe"):
        return None
    return n if kind == "solid" else n + 8


def kind_of_number(n):
    if n == 0:
        return "cue"
    if n == 8:
        return "eight"
    return "solid" if n < 8 else "stripe"


def color_of_number(n):
    if n in (0, 8):
        return "schwarz" if n == 8 else ""
    base = n if n < 8 else n - 8
    return next(c for c, v in COLOR_NUMBER.items() if v == base)
