"""
Einfache Stoßplanung + Gyroskop-Ausrichtung fuer die Beamer-Anzeige.
"""

import math

from geometry import WARP_H, WARP_W

ALIGN_TOLERANCE_DEG = 4.0


def normalize_angle_deg(angle):
    return (float(angle) + 180.0) % 360.0 - 180.0


def angle_diff_deg(a, b):
    return normalize_angle_deg(a - b)


def is_aligned(actual_deg, target_deg, tolerance_deg=ALIGN_TOLERANCE_DEG):
    return abs(angle_diff_deg(actual_deg, target_deg)) <= float(tolerance_deg)


def _group_for_next_shot(game):
    if game["phase"] != "playing":
        return None
    turn = int(game["turn"]) % 2
    own = game["groups"][turn]
    if own in ("solid", "stripe"):
        if int(game["remaining"].get(own, 0)) == 0:
            return "eight"
        return own
    return "solid_or_stripe"


def _target_candidates(balls, wanted_group):
    if wanted_group == "solid_or_stripe":
        return [b for b in balls if b.kind in ("solid", "stripe")]
    return [b for b in balls if b.kind == wanted_group]


def _pockets():
    return [(0.0, 0.0), (WARP_W / 2.0, 0.0), (WARP_W, 0.0),
            (0.0, WARP_H), (WARP_W / 2.0, WARP_H), (WARP_W, WARP_H)]


def plan_shot(balls, game):
    cue = next((b for b in balls if b.kind == "cue"), None)
    if cue is None:
        return None
    wanted = _group_for_next_shot(game)
    if wanted is None:
        return None
    objects = _target_candidates(balls, wanted)
    if not objects:
        return None

    best = None
    margin = cue.r * 0.9
    for obj in objects:
        for px, py in _pockets():
            vx, vy = px - obj.x, py - obj.y
            dist_obj_pocket = math.hypot(vx, vy)
            if dist_obj_pocket < 1e-6:
                continue
            ux, uy = vx / dist_obj_pocket, vy / dist_obj_pocket
            tx, ty = obj.x - 2.0 * cue.r * ux, obj.y - 2.0 * cue.r * uy
            if not (margin <= tx <= WARP_W - margin and margin <= ty <= WARP_H - margin):
                continue
            dist_cue_target = math.hypot(tx - cue.x, ty - cue.y)
            score = dist_obj_pocket + dist_cue_target
            if best is None or score < best["score"]:
                best = {
                    "score": score,
                    "target": (tx, ty),
                    "object": (obj.x, obj.y),
                    "pocket": (px, py),
                    "angle_deg": math.degrees(math.atan2(ty - cue.y, tx - cue.x)),
                    "cue": (cue.x, cue.y),
                }
    return best

