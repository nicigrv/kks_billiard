"""
Tests fuer die Rundenaufzeichnung.

    python3 -m sim.test_round_recorder
"""

import os
import sys
import tempfile

from recorder import RoundRecorder

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def state(phase="playing", winner=None):
    return {
        "game": {
            "phase": phase,
            "teams": ["Team A", "Team B"],
            "frames_won": [1, 0],
            "turn": 0,
            "groups": ["solid", "stripe"],
            "group_names": ["Volle", "Halbe"],
            "remaining": {"solid": 4, "stripe": 6},
            "winner": winner,
            "message": "Testzug",
        }
    }


def test_round_video():
    print("Rundenaufzeichnung")
    with tempfile.TemporaryDirectory() as tmp:
        rr = RoundRecorder(tmp)
        rr.save_shot(state())
        check(rr.frame_count == 0, "ohne Aktivierung wird nichts gespeichert")

        rr.enabled = True
        rr.save_shot(state())
        rr.save_shot(state(phase="finished", winner=0))
        check(rr.frame_count == 2, "pro Zug wird ein GUI-Bild gespeichert")

        path = rr.finish_round()
        check(path is not None and os.path.isfile(path), "Video wird am Rundenende erzeugt")
        check(os.path.getsize(path) > 0, "Video ist nicht leer")
        check(rr.frame_count == 0, "Zwischenspeicher wird nach Export geleert")


if __name__ == "__main__":
    test_round_video()
    print(f"\n{'ALLES OK' if not FAILS else f'{len(FAILS)} FEHLER'}")
    sys.exit(1 if FAILS else 0)
