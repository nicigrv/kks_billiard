"""
Test des KI-Chips (Coral USB Accelerator), laeuft im laufenden Container:

    docker compose exec billard python3 coral_check.py

Prueft: Stick am USB, Treiber, Modell laden, Geschwindigkeit (mit dem eigenen Modell
oder dem Startmodell).
"""

import glob
import os
import sys
import time

import numpy as np

from coral_detector import DATA_MODEL, EDGETPU_LIB, START_MODEL

CORAL_IDS = {("1a6e", "089a"): "Coral (noch ohne Firmware, wird beim ersten Laden initialisiert)",
             ("18d1", "9302"): "Coral (bereit)"}


def usb_coral():
    found = []
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            vid = open(os.path.join(dev, "idVendor")).read().strip()
            pid = open(os.path.join(dev, "idProduct")).read().strip()
            speed = open(os.path.join(dev, "speed")).read().strip()
        except OSError:
            continue
        if (vid, pid) in CORAL_IDS:
            found.append(f"{CORAL_IDS[(vid, pid)]}, USB {'3' if int(speed) >= 5000 else '2'} ({speed} Mbit/s)")
    return found


def main():
    print("=== KI-Chip (Coral USB Accelerator) ===")
    usb = usb_coral()
    if not usb:
        print("[FEHLER] Kein Coral am USB gefunden.")
        print("  - Stick am BLAUEN USB-3-Port des Pi einstecken, anderes Kabel probieren")
        return 1
    for u in usb:
        print("USB:", u)
        if "USB 2" in u:
            print("  Hinweis: am USB-2-Port deutlich langsamer - blauen Port nehmen")
    try:
        from tflite_runtime.interpreter import Interpreter, load_delegate
    except ImportError:
        print("[FEHLER] tflite_runtime fehlt - Container mit dem Dockerfile neu bauen (CORAL=1)")
        return 1
    try:
        delegate = load_delegate(EDGETPU_LIB)
    except (ValueError, OSError) as e:
        print(f"[FEHLER] Treiber findet den Stick nicht: {e}")
        return 1
    print("[OK] Treiber findet den Stick")
    model = DATA_MODEL if os.path.isfile(DATA_MODEL) else START_MODEL
    if not os.path.isfile(model):
        print("Noch kein Modell - Stick und Treiber sind in Ordnung. Nach dem Training das Modell")
        print("im Admin-Portal hochladen und den Test wiederholen, dann wird auch die Geschwindigkeit gemessen.")
        return 0
    print("Modell:", "eigenes Modell" if model == DATA_MODEL else "Startmodell")
    t0 = time.monotonic()
    it = Interpreter(model_path=model, experimental_delegates=[delegate])
    it.allocate_tensors()
    inp = it.get_input_details()[0]
    data = np.zeros(inp["shape"], inp["dtype"])
    it.set_tensor(inp["index"], data)
    it.invoke()
    print(f"Geladen in {time.monotonic() - t0:.1f} s")
    times = []
    for _ in range(30):
        t = time.monotonic()
        it.set_tensor(inp["index"], data)
        it.invoke()
        times.append((time.monotonic() - t) * 1000)
    ms = float(np.median(times))
    print(f"[OK] KI-Chip arbeitet: {ms:.0f} ms pro Bild, also bis zu {1000 / ms:.0f} Bilder/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
