"""
Stossvorschlag ("KI-Schlagverbesserung"): sucht fuer die Mannschaft am Zug den
einfachsten direkten Stoss und die passende Lage des Queues.

Ehrlich gesagt ist das kein trainiertes Netz, sondern Geometrie (Geisterkugel-Methode),
die alle Kombinationen Zielkugel x Tasche durchrechnet und bewertet:
  * Geisterkugel G: dort muss die Weisse die Zielkugel treffen, damit diese zur Tasche laeuft.
  * Weg Weisse -> G und Weg Zielkugel -> Tasche muessen frei sein.
  * Schnittwinkel ueber 75 Grad gilt als nicht spielbar.
  * Mitteltaschen nur bei steilem Anlauf (flach an der Bande entlang faellt nichts).
  * Bewertung: lange Wege und duenne Schnitte sind schwer.
Stellungsspiel (wo die Weisse danach liegt) wird nicht beruecksichtigt.

Koordinaten: Draufsicht WARP_W x WARP_H (siehe geometry.py), y zeigt nach unten.
Winkel in Grad, 0 = nach rechts, 90 = nach unten (Bildschirm-Konvention der Grafik).
"""

import math

from ballnum import ball_number
from geometry import WARP_H, WARP_W

POCKETS = [(0.0, 0.0), (WARP_W / 2, 0.0), (float(WARP_W), 0.0),
           (0.0, float(WARP_H)), (WARP_W / 2, float(WARP_H)), (float(WARP_W), float(WARP_H))]
POCKET_NAMES = ["Ecke oben links", "Mitte oben", "Ecke oben rechts",
                "Ecke unten links", "Mitte unten", "Ecke unten rechts"]
SIDE_POCKETS = (1, 4)
MAX_CUT_DEG = 75.0
SIDE_MIN_STEEPNESS = math.cos(math.radians(55))   # Anlauf zur Mitteltasche max. 55 Grad schraeg


def _seg_dist(ax, ay, bx, by, px, py):
    """Abstand Punkt P zur Strecke A-B."""
    vx, vy = bx - ax, by - ay
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
    return math.hypot(ax + t * vx - px, ay + t * vy - py)


def _path_clear(ax, ay, bx, by, obstacles, min_dist):
    return all(_seg_dist(ax, ay, bx, by, o.x, o.y) >= min_dist for o in obstacles)


def target_kinds(game):
    """Welche Kugeln darf die Mannschaft am Zug spielen?"""
    own = game["groups"][game["turn"]]
    if own is None:
        return {"solid", "stripe"}
    if game["remaining"][own] == 0:
        return {"eight"}
    return {own}


def difficulty_label(score):
    if score < 0.9:
        return "leicht"
    if score < 1.8:
        return "mittel"
    return "schwer"


def _evaluate(cue, t, pi, balls, R):
    px, py = POCKETS[pi]
    dx, dy = px - t.x, py - t.y
    d2 = math.hypot(dx, dy)
    if d2 < 1e-6:
        return None
    ux, uy = dx / d2, dy / d2
    if pi in SIDE_POCKETS and abs(uy) < SIDE_MIN_STEEPNESS:
        return None
    gx, gy = t.x - ux * 2 * R, t.y - uy * 2 * R           # Geisterkugel
    if not (0.8 * R <= gx <= WARP_W - 0.8 * R and 0.8 * R <= gy <= WARP_H - 0.8 * R):
        return None
    vx, vy = gx - cue.x, gy - cue.y
    d1 = math.hypot(vx, vy)
    if d1 < 0.5 * R:
        return None
    cos_cut = max(-1.0, min(1.0, (vx * ux + vy * uy) / d1))
    cut = math.degrees(math.acos(cos_cut))
    if cut > MAX_CUT_DEG:
        return None
    clearance = 2 * R * 0.97
    others = [b for b in balls if b is not cue and b is not t]
    if not _path_clear(cue.x, cue.y, gx, gy, others, clearance):
        return None
    if not _path_clear(t.x, t.y, px, py, [b for b in balls if b is not t], clearance):
        return None
    score = (d1 + 1.6 * d2) / WARP_W / max(0.15, cos_cut) ** 1.5
    if pi in SIDE_POCKETS:
        score += 0.3
    return {
        "type": "pot",
        "angle": math.degrees(math.atan2(vy, vx)),
        "cue": [cue.x, cue.y],
        "ghost": [gx, gy],
        "target": [t.x, t.y],
        "target_kind": t.kind,
        "target_number": ball_number(t.kind, getattr(t, "color", "")),
        "pocket": [px, py],
        "pocket_index": pi,
        "pocket_name": POCKET_NAMES[pi],
        "cut": cut,
        "score": score,
    }


def describe(s):
    if s["type"] == "break":
        return "Anstoß: voll auf die vordere Kugel, kräftig stoßen."
    n = s.get("target_number")
    if n:
        ball = f"die {n}"
    else:
        ball = {"solid": "eine Volle", "stripe": "eine Halbe", "eight": "die 8"}.get(s["target_kind"], "Kugel")
    return (f"Vorschlag: {ball} in die {s['pocket_name']} "
            f"(Schnitt {s['cut']:.0f}°, {difficulty_label(s['score'])})")


def plan_shot(balls, radius, game):
    """balls: erkannte Kugeln (x, y, kind, color) in Draufsicht-Koordinaten.
    Liefert ein dict mit Zielkugel, Tasche, Geisterkugel und Queue-Winkel - oder None."""
    if game.get("phase") != "playing":
        return None
    cue = next((b for b in balls if b.kind == "cue"), None)
    objs = [b for b in balls if b.kind in ("solid", "stripe", "eight")]
    if cue is None or not objs:
        return None
    R = float(radius)

    if game.get("breaking"):
        # Anstoss: auf die naechste Kugel (die Spitze des Dreiecks) zielen
        t = min(objs, key=lambda b: math.hypot(b.x - cue.x, b.y - cue.y))
        s = {"type": "break", "angle": math.degrees(math.atan2(t.y - cue.y, t.x - cue.x)),
             "cue": [cue.x, cue.y], "target": [t.x, t.y], "target_kind": t.kind,
             "target_number": ball_number(t.kind, getattr(t, "color", "")), "score": 0.0}
        s["text"] = describe(s)
        return s

    kinds = target_kinds(game)
    best = None
    for t in objs:
        if t.kind not in kinds:
            continue
        for pi in range(len(POCKETS)):
            s = _evaluate(cue, t, pi, balls, R)
            if s and (best is None or s["score"] < best["score"]):
                best = s
    if best is None:
        return {"type": "none", "cue": [cue.x, cue.y], "angle": None, "score": 99.0,
                "text": "Kein direkter Stoß frei - Sicherheitsstoß spielen."}
    best["text"] = describe(best)
    return best
