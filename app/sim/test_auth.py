"""
Tests: Anmeldung am Portal (auth.py) - Admin-PIN, Spiel-PIN, Sitzungen, Sperre nach Fehlversuchen.
Pruefer mit eigenen Codes gibt es am Pi nicht mehr (festgelegt wird auf der Trainingsseite im Netz).

    python3 -m sim.test_auth
"""

import json
import os
import sys
import tempfile

from auth import MAX_AGE, Auth, LoginError

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def test_auth():
    print("Anmeldung")
    with tempfile.TemporaryDirectory() as d:
        now = [1000.0]
        a = Auth("1902", d, clock=lambda: now[0], game_pin="1212")
        sess, tok = a.login("1902", "ip1")
        check(sess["role"] == "admin", "Admin-PIN 1902")
        check(a.who(f"billard={tok}")["role"] == "admin", "Cookie -> Admin")
        check(a.who(f"x=1; billard={tok}")["role"] == "admin", "Cookie unter mehreren")
        check(a.who("")["role"] is None and a.who("", "1902")["role"] == "admin", "ohne Anmeldung nichts, PIN-Kopfzeile geht")
        sess, tok3 = a.login("1212", "ip1")
        check(sess["role"] == "spiel" and a.who(f"billard={tok3}")["role"] == "spiel", "Spiel-PIN 1212 -> Spiel-Panel")
        check(Auth.can("admin", "spiel") and Auth.can("spiel", "spiel") and not Auth.can("spiel", "admin")
              and not Auth.can(None, "spiel"), "Admin darf alles vom Spiel-Panel, sonst niemand")
        check(Auth("1902", d).who("")["role"] == "spiel", "ohne Spiel-PIN ist das Spiel-Panel offen")
        check(Auth("1902", d, clock=lambda: now[0], game_pin="1212").who(f"billard={tok}")["role"] == "admin",
              "Sitzung überlebt Neustart")
        try:
            a.login("4821", "ip2")
            check(False, "Prüfer-Codes gibt es nicht mehr")
        except LoginError:
            check(True, "Prüfer-Codes gibt es nicht mehr")

        # Sitzung eines Pruefers aus einer frueheren Version
        with open(os.path.join(d, "sitzungen.json")) as f:
            alt = json.load(f)
        alt["alterpruefer"] = {"role": "pruefer", "name": "Anna", "t": now[0]}
        with open(os.path.join(d, "sitzungen.json"), "w") as f:
            json.dump(alt, f)
        b = Auth("1902", d, clock=lambda: now[0], game_pin="1212")
        check(b.who("billard=alterpruefer")["role"] is None, "alte Prüfer-Sitzung gilt nicht mehr")

        now[0] += MAX_AGE + 1
        check(a.who(f"billard={tok}")["role"] is None, "abgelaufene Sitzung gilt nicht mehr (auch ohne Neustart)")

        for _ in range(5):
            try:
                a.login("0000", "ip9")
            except LoginError:
                pass
        try:
            a.login("1902", "ip9")
            check(False, "nach 5 Fehlversuchen kurz gesperrt")
        except LoginError as e:
            check("Fehlversuche" in str(e), "nach 5 Fehlversuchen kurz gesperrt")
        check(Auth("", d).who("")["role"] == "admin", "ohne PIN offen wie früher")


def main():
    test_auth()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")


if __name__ == "__main__":
    main()
