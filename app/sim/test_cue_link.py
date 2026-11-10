"""
Tests: Queue koppeln (Castcode), im Netz finden, Lage per HTTP-API abfragen.
Laeuft gegen den simulierten Queue (sim/fake_cue.py), ohne Hardware.

    python3 -m sim.test_cue_link
"""

import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

os.environ["CUE_DISCOVERY_ADDR"] = "127.0.0.1"      # im Test: Suche lokal statt Broadcast

from config import UserError
from cue_sensor import CueSensor, clean_castcode
from sim import fake_cue

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def wait_for(cond, s=6.0):
    end = time.monotonic() + s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def start_fake(code, api_port, disc_port):
    cue = fake_cue.Cue(code)
    srv = ThreadingHTTPServer(("127.0.0.1", api_port), fake_cue.make_handler(cue))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=fake_cue.discovery, args=(cue, disc_port, api_port), daemon=True).start()
    return cue, srv


def main():
    print("Castcode")
    check(clean_castcode(" k7p-2qm ") == "K7P2QM", "Eingabe vereinheitlicht (klein, Leer- und Bindestriche)")
    check(clean_castcode(fake_cue.Cue("SMQ234").castcode) == "SMQ234", "Castcode des Simulators ist gueltig")
    for bad in ("K7P2Q", "K7P2QMX", "K0P2QM", "K7P2Q1"):
        try:
            clean_castcode(bad)
            check(False, f"ungueltig: {bad}")
        except UserError:
            check(True, f"ungueltig abgelehnt: {bad}")

    print("Koppeln und abfragen")
    disc = 5191
    cue, srv = start_fake("SMQ234", 8193, disc)
    found = []
    s = CueSensor("", discovery_port=disc, on_found=found.append, poll_hz=30).start()
    time.sleep(0.5)
    check(not s.connected and s.status()["state"] == "kein Castcode eingetragen", "ohne Castcode: wartet")
    s.pair("SMQ234")
    check(wait_for(lambda: s.connected), "Castcode eingetragen -> Queue gefunden und verbunden")
    check(found and found[0] == ("127.0.0.1", 8193), f"Adresse gemeldet ({found[:1]})")
    n0 = s.packets
    time.sleep(1.0)
    rate = s.packets - n0
    check(15 <= rate <= 35, f"fragt ca. 25-30x pro Sekunde ab ({rate}/s)")
    with cue.lock:
        cue.yaw = 10.0
    time.sleep(0.2)
    s.offset = 10.0
    a = s.table_angle()
    check(a is not None and abs(a) < 5, f"Winkel kommt an ({a})")
    st = s.status()
    check(st["castcode"] == "SMQ234" and st["address"] == "127.0.0.1:8193" and st["battery"] == 3.95,
          "Status: Castcode, Adresse, Akku")

    print("Falscher Castcode")
    s.pair("ABCDEF")
    time.sleep(2.5)
    check(not s.connected and "suche" in s.status()["state"], "anderer Castcode: kein Queue antwortet")
    s.address = ("127.0.0.1", 8193)                  # falsche Zuordnung erzwingen
    check(wait_for(lambda: s.last_error == "Castcode passt nicht zum Queue"), "API lehnt fremden Castcode ab (403)")

    print("Neue IP nach Neustart des Queues")
    s.pair("SMQ234")
    check(wait_for(lambda: s.connected), "wieder verbunden")
    srv.shutdown()
    srv.server_close()
    cue2, srv2 = start_fake("SMQ235", 8194, 5192)     # anderer Queue stoert nicht
    srv3 = ThreadingHTTPServer(("127.0.0.1", 8195), fake_cue.make_handler(cue))
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    # gleicher Queue, neuer Port (wie neue IP): Suche liefert die neue Adresse
    threading.Thread(target=fake_cue.discovery, args=(fake_cue.Cue("SMQ234"), disc + 10, 8195), daemon=True).start()
    s.discovery_port = disc + 10
    check(wait_for(lambda: s.address == ("127.0.0.1", 8195) and s.connected, 15),
          "Queue nicht mehr erreichbar -> neu gesucht und unter neuer Adresse verbunden")

    s.pair("")
    time.sleep(0.3)
    check(s.status()["state"] == "kein Castcode eingetragen", "entkoppelt")
    s.stop()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")


if __name__ == "__main__":
    main()
