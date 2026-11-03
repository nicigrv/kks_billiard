"""UI-Hilfsfunktionen fuer die Anzeige."""


def _clamp_count(value, lo=0, hi=7):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    return max(lo, min(hi, n))


def game_bar_state(game_state):
    """Bereitet den Zustand fuer die untere Game-Bar auf."""
    remaining = game_state.get("remaining", {}) if isinstance(game_state, dict) else {}
    solids_left = _clamp_count(remaining.get("solid", 0))
    stripes_left = _clamp_count(remaining.get("stripe", 0))
    solids = [{"number": n, "active": i < solids_left} for i, n in enumerate(range(1, 8))]
    stripes = [{"number": n, "active": i < stripes_left} for i, n in enumerate(range(9, 16))]
    return {
        "solids": solids,
        "stripes": stripes,
        "eight_on_table": bool(game_state.get("eight_on_table", True)) if isinstance(game_state, dict) else True,
        "solid_points": 7 - solids_left,
        "stripe_points": 7 - stripes_left,
    }
