"""
Kugeln: Datentyp und Nummern.

Der Coral erkennt jede Kugel direkt mit ihrer Nummer (0 = Weisse, 1-7 Volle, 8, 9-15 Halbe).
Typ und Farbe werden daraus abgeleitet. Jede Farbe gibt es genau einmal voll und einmal halb.
"""

from dataclasses import dataclass

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


@dataclass
class Ball:
    x: float
    y: float
    r: float
    kind: str              # cue | eight | solid | stripe
    color: str = ""
    score: float = 0.0     # Sicherheit des Modells (0..1)
    votes: object = None   # Counter der Typ-Stimmen ueber viele Bilder (nur in stabilen Zustaenden)
    number: int = None

    @classmethod
    def from_number(cls, x, y, r, number, score=0.0):
        return cls(x, y, r, kind_of_number(number), color_of_number(number), score, number=number)
