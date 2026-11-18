"""
Tests: Queue koppeln (Castcode), im Netz finden, Lage abonnieren (der Queue sendet selbst),
LED-Farbe und Knopf. Laeuft gegen den simulierten Queue (sim/fake_cue.py), ohne Hardware.

    python3 -m sim.test_cue_link
"""

import json
import os
import sys
import threading
import time

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


def start_fake(code, disc_port):
    cue = fake_cue.Cue(code)
    threading.Thread(target=fake_cue.discovery, args=(cue, disc_port, 80), daemon=True).start()
    time.sleep(0.1)
    return cue


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

    print("Pruefung der Werte")
    v = CueSensor("SMQ234")
    good = {"castcode": "SMQ234", "seq": 1, "yaw": 12.5, "pitch": -3.0, "bat": 3.9, "stoss": 4.2}
    check(v.feed(json.dumps(good)) and v.raw_yaw() == 12.5, "gueltiges Paket angenommen")
    for bad in (dict(good, yaw="nan"), dict(good, yaw=1e9), dict(good, pitch=135), dict(good, yaw=None)):
        check(not v.feed(json.dumps(bad)) and v.raw_yaw() == 12.5, f"unplausibel verworfen: {bad}")
    check(not v.feed(b"[1, 2]") and not v.feed(b"kein json"), "kein JSON-Objekt verworfen")
    check(v.rejected == 6, f"verworfene Pakete werden gezaehlt ({v.rejected})")
    check(v.feed(json.dumps(dict(good, bat=99, stoss=-5))) and v.status()["battery"] is None
          and v.status()["stroke"] is None, "unplausibler Akku/Stoss weggelassen, Lage trotzdem da")
    check(not v.feed(json.dumps(dict(good, castcode="ABCDEF"))) and v.rejected == 6, "fremder Queue ist kein Fehler")

    print("Koppeln und abonnieren")
    disc = 5191
    cue = start_fake("SMQ234", disc)
    found, presses, strokes = [], [], []
    team = {"color": None}
    s = CueSensor("", discovery_port=disc, on_found=found.append, on_stroke=strokes.append,
                  on_button=lambda: presses.append(1), led_color=lambda: team["color"]).start()
    time.sleep(0.5)
    check(not s.connected and s.status()["state"] == "kein Castcode eingetragen", "ohne Castcode: wartet")
    check(cue.led() == "rot", "LED rot, solange der Pi nicht abonniert hat")
    s.pair("SMQ234")
    check(wait_for(lambda: s.connected), "Castcode eingetragen -> Queue gefunden, abonniert, Daten kommen")
    check(found and found[0] == ("127.0.0.1", disc), f"Adresse gemeldet ({found[:1]})")
    check(wait_for(lambda: cue.led() == "gruen"), "LED gruen: verbunden, kein Spiel")
    n0 = s.packets
    time.sleep(1.0)
    rate = s.packets - n0
    check(12 <= rate <= 22, f"Queue sendet von selbst ca. 17x pro Sekunde, je 3 Messungen gemittelt ({rate}/s)")
    check(s.status()["rate"] > 10, f"Paketrate im Status ({s.status()['rate']}/s)")
    with cue.lock:
        cue.yaw = 10.0
    time.sleep(0.2)
    s.offset = 10.0
    a = s.table_angle()
    check(a is not None and abs(a) < 5, f"Winkel kommt an ({a})")
    st = s.status()
    check(st["castcode"] == "SMQ234" and st["address"] == f"127.0.0.1:{disc}" and st["battery"] == 3.95
          and st["rssi"] == -52, "Status: Castcode, Adresse, Akku, WLAN-Signal")
    check(st["stroke"] == 3.6 and st["strokes"] == 2, "Stoßstärke kommt an (letzter Stoß, Anzahl)")

    print("Knopf und Teamfarbe")
    check(cue.press(), "Knopf bei gruener LED wird gezaehlt")
    check(wait_for(lambda: len(presses) == 1, 2), "Pi bekommt den Knopfdruck genau einmal")
    team["color"] = [0, 90, 255]                      # Spiel laeuft, Team A dran
    check(wait_for(lambda: cue.led() == [0, 90, 255], 1), "Teamfarbe kommt sofort auf die LED")
    check(cue.press() and wait_for(lambda: len(presses) == 2, 2),
          "Knopf waehrend des Spiels wird gemeldet (= Stoss fertig)")
    team["color"] = [255, 110, 0]                     # Zugwechsel
    check(wait_for(lambda: cue.led() == [255, 110, 0], 1), "Zugwechsel: andere Teamfarbe")
    time.sleep(0.5)
    check(len(presses) == 2, "kein weiterer Knopfdruck gemeldet")
    cue.stroke()
    check(wait_for(lambda: len(strokes) == 1, 2) and strokes[0] == 3.6, "Stoss vom Gyroskop wird gemeldet (mit Staerke)")
    team["color"] = None
    check(wait_for(lambda: cue.led() == "gruen", 1), "Spiel vorbei: wieder gruen")

    print("Queue optional (aus/an)")
    s.set_enabled(False)
    time.sleep(0.5)
    n0 = s.packets
    check(not s.connected and s.status()["state"] == "ausgeschaltet", "aus: gilt als nicht verbunden")
    check(wait_for(lambda: cue.led() == "rot", 5), "aus: keine Lebenszeichen mehr -> LED am Queue rot")
    time.sleep(0.5)
    check(s.packets == n0, "aus: Queue hoert auf zu senden")
    s.set_enabled(True)
    check(wait_for(lambda: s.connected), "wieder an: verbunden")

    print("Falscher Castcode")
    s.pair("ABCDEF")
    time.sleep(2.5)
    check(not s.connected and "suche" in s.status()["state"], "anderer Castcode: kein Queue antwortet")

    print("Neue IP nach Neustart des Queues")
    s.pair("SMQ234")
    check(wait_for(lambda: s.connected), "wieder verbunden")
    start_fake("SMQ235", 5192)                        # anderer Queue stoert nicht
    # gleicher Queue, neuer Port (wie neue IP): alter sendet nicht mehr, Suche findet den neuen
    with cue.lock:
        cue.subscriber = None
        cue.castcode = "XXXXXX"                       # alter Queue "weg"
    cue_new = start_fake("SMQ234", disc + 10)
    s.discovery_port = disc + 10
    check(wait_for(lambda: s.address == ("127.0.0.1", disc + 10) and s.connected, 15),
          "keine Daten mehr -> neu gesucht und unter neuer Adresse verbunden")
    check(wait_for(lambda: cue_new.led() == "gruen", 2), "neuer Queue zeigt gruen")

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
