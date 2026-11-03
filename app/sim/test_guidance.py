"""
Tests fuer Stoßplanung + Gyro-Ausrichtung.

    python3 -m sim.test_guidance
"""

import sys
from dataclasses import dataclass

from guidance import angle_diff_deg, is_aligned, plan_shot

FAILS = []


@dataclass
class Ball:
    x: float
    y: float
    r: float
    kind: str


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def test_angles():
    print("Teil 1: Winkel")
    check(abs(angle_diff_deg(179, -179) - (-2.0)) < 1e-6, "Winkeldifferenz ueber 180°-Grenze")
    check(is_aligned(10, 13.5, tolerance_deg=4.0), "Ausrichtung innerhalb Toleranz")
    check(not is_aligned(10, 18.5, tolerance_deg=4.0), "Ausrichtung ausserhalb Toleranz")


def test_planner():
    print("\nTeil 2: Stoßplanung")
    balls = [
        Ball(140, 200, 10, "cue"),
        Ball(360, 200, 10, "solid"),
        Ball(420, 120, 10, "stripe"),
    ]
    game = {
        "phase": "playing",
        "turn": 0,
        "groups": ["solid", "stripe"],
        "remaining": {"solid": 4, "stripe": 5},
    }
    plan = plan_shot(balls, game)
    check(plan is not None, "Plan fuer aktive Gruppe vorhanden")
    check(plan and abs(plan["object"][0] - 360) < 1e-6, "Eigene Objektkugel gewaehlt")
    check(plan and -180.0 <= plan["angle_deg"] <= 180.0, "Gueltiger Zielwinkel")


if __name__ == "__main__":
    test_angles()
    test_planner()
    print(f"\n{'ALLES OK' if not FAILS else f'{len(FAILS)} FEHLER'}")
    sys.exit(1 if FAILS else 0)
