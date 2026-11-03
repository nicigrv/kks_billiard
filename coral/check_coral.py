"""
Prueft, ob der Coral USB Accelerator funktioniert, und misst die Geschwindigkeit.

Nutzt ein fertiges Testmodell von Google (MobileNet V2, Bildklassifikation).
Das erkennt KEINE Billardkugeln - es beweist nur, dass Stick, Treiber und
Software zusammen laufen. Das Kugelmodell muss erst trainiert werden.
"""

import subprocess
import sys
import time

import numpy as np

MODEL = "/models/mobilenet_v2_1.0_224_quant_edgetpu.tflite"


def usb_info():
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return "lsusb fehlt"
    lines = [l for l in out.splitlines() if "18d1:9302" in l or "1a6e:089a" in l]
    if not lines:
        return None
    return "\n".join(lines)


def main():
    print("=== Coral USB Accelerator Test ===")
    usb = usb_info()
    if usb is None:
        print("[FEHLER] Kein Coral am USB gefunden.")
        print("  - Stick am BLAUEN USB-3-Port des Pi einstecken")
        print("  - Kabel tauschen (viele USB-C-Kabel koennen nur laden)")
        print("  - Mit 'lsusb' auf dem Pi pruefen: 1a6e:089a oder 18d1:9302 muss erscheinen")
        return 1
    print("USB:", usb)
    if "1a6e:089a" in usb:
        print("  (Stick noch ohne Firmware - wird beim ersten Laden des Modells initialisiert)")

    from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
    tpus = list_edge_tpus()
    print("Edge TPUs:", tpus or "keine")
    if not tpus:
        print("[FEHLER] Treiber sieht keinen Stick. Container mit Zugriff auf /dev/bus/usb gestartet?")
        return 1

    t0 = time.monotonic()
    interp = make_interpreter(MODEL)
    interp.allocate_tensors()
    print(f"Modell geladen in {time.monotonic() - t0:.1f} s")

    inp = interp.get_input_details()[0]
    data = np.random.randint(0, 255, inp["shape"], dtype=np.uint8)
    interp.set_tensor(inp["index"], data)
    interp.invoke()                      # erster Lauf: Modell auf den Stick kopieren
    times = []
    for _ in range(50):
        t = time.monotonic()
        interp.set_tensor(inp["index"], data)
        interp.invoke()
        times.append((time.monotonic() - t) * 1000)
    ms = float(np.median(times))
    print(f"[OK] Coral funktioniert: {ms:.1f} ms pro Bild (Median aus 50), also ca. {1000 / ms:.0f} Bilder/s")
    if ms > 15:
        print("  Hinweis: langsamer als ueblich (~3-5 ms). Laeuft der Stick am USB-2-Port?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
