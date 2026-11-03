"""
Kleiner Test fuer die Aufbereitung der unteren Game-Bar.

    python3 -m sim.test_ui_state
"""

import sys

from ui_state import game_bar_state

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def test_game_bar_state():
    s = game_bar_state({"remaining": {"solid": 5, "stripe": 2}, "eight_on_table": False})
    check(sum(1 for b in s["solids"] if b["active"]) == 5, "5 volle aktiv")
    check(sum(1 for b in s["stripes"] if b["active"]) == 2, "2 halbe aktiv")
    check(s["solid_points"] == 2 and s["stripe_points"] == 5, "Punkte = versenkte Kugeln")
    check(not s["eight_on_table"], "8 als versenkt markiert")

    s = game_bar_state({"remaining": {"solid": 99, "stripe": -4}})
    check(sum(1 for b in s["solids"] if b["active"]) == 7, "volle werden nach oben begrenzt")
    check(sum(1 for b in s["stripes"] if b["active"]) == 0, "halbe werden nach unten begrenzt")
    check(s["eight_on_table"], "8 standardmaessig auf dem Tisch")


if __name__ == "__main__":
    test_game_bar_state()
    print(f"\n{'ALLES OK' if not FAILS else f'{len(FAILS)} FEHLER'}")
    sys.exit(1 if FAILS else 0)
