#!/usr/bin/env python3
"""
Firmware-Update fuer die Billard-ESP32-CAM ueber das Netzwerk.

Laeuft auf dem Raspberry Pi oder jedem PC im selben Netzwerk (Windows/Linux/Mac),
braucht nur Python 3, keine Zusatzpakete.

Ablauf:
  1. (optional) Firmware mit arduino-cli kompilieren   --build
  2. .bin-Datei finden und pruefen (richtiger Dateityp, Groesse, Chip)
  3. Pruefen, ob die Kamera erreichbar ist
  4. Hochladen mit Fortschrittsanzeige
  5. Warten, bis die Kamera neu gestartet ist, und Status pruefen

Beispiele:
  python3 update_cam.py                                  # neueste .bin suchen, an Standard-IP
  python3 update_cam.py --host 192.168.178.50 --bin pfad/zur/esp32cam_stream.ino.bin
  python3 update_cam.py --build                          # erst kompilieren, dann hochladen

Passwort: wird abgefragt, oder per Umgebungsvariable CAM_OTA_PASSWORD gesetzt.
"""

import argparse
import getpass
import http.client
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKETCH_DIR = ROOT / "esp32cam_stream"
DEFAULT_HOST = os.environ.get("CAM_HOST", "192.168.178.50")
# Muss zur Partition in der IDE passen ("Default 4MB with spiffs" = 1,25 MB App-Platz)
MAX_APP_SIZE = 0x140000
FQBN = "esp32:esp32:esp32cam:PartitionScheme=default"
ESP_IMAGE_MAGIC = 0xE9
CHIP_ID_ESP32 = 0x0000  # klassischer ESP32, wie auf der ESP32-CAM


def die(msg: str, code: int = 1):
    print(f"\n[FEHLER] {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- Build
def build_firmware() -> None:
    cli = shutil.which("arduino-cli")
    if not cli:
        die("arduino-cli nicht gefunden. Entweder installieren oder in der Arduino IDE "
            "'Sketch -> Kompilierte Binaerdatei exportieren' nutzen und ohne --build starten.")
    print(f"[build] Kompiliere {SKETCH_DIR.name} ({FQBN}) ...")
    r = subprocess.run([cli, "compile", "--fqbn", FQBN, "--export-binaries", str(SKETCH_DIR)])
    if r.returncode != 0:
        die("Kompilieren fehlgeschlagen, es wurde nichts hochgeladen.")


# ---------------------------------------------------------------- .bin finden + pruefen
def find_newest_bin() -> Path:
    # Sketch-Ordner (PC mit Arduino IDE) und ./firmware (Docker-Updater auf dem Pi)
    search = [SKETCH_DIR, ROOT / "firmware", Path(os.environ.get("FIRMWARE_DIR", "/firmware"))]
    candidates = [p for d in search if d.is_dir() for p in d.rglob("*.ino.bin")
                  if not any(x in p.name for x in ("merged", "bootloader", "partitions"))]
    if not candidates:
        die("Keine .ino.bin gefunden (gesucht in: " + ", ".join(str(d) for d in search) + ").\n"
            "        Erst exportieren bzw. in den Ordner 'firmware' legen, oder --bin angeben.")
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    age_min = (time.time() - newest.stat().st_mtime) / 60
    print(f"[bin] Neueste Datei: {newest} (vor {age_min:.0f} min erstellt)")
    if age_min > 60:
        print("      Achtung: Datei ist aelter als 1 Stunde. Hast du nach der letzten Aenderung neu exportiert?")
    return newest


def check_bin(path: Path) -> bytes:
    if not path.is_file():
        die(f"Datei nicht gefunden: {path}")
    for bad in ("merged", "bootloader", "partitions"):
        if bad in path.name:
            die(f"'{path.name}' ist die falsche Datei. Benoetigt wird esp32cam_stream.ino.bin.")
    data = path.read_bytes()
    if len(data) < 1024:
        die("Datei ist viel zu klein, das ist keine Firmware.")
    if data[0] != ESP_IMAGE_MAGIC:
        die("Datei ist kein ESP32-Programm (falsches Startbyte).")
    chip_id = struct.unpack_from("<H", data, 12)[0]
    if chip_id != CHIP_ID_ESP32:
        die(f"Firmware ist fuer einen anderen Chip kompiliert (Chip-ID {chip_id}), "
            "nicht fuer den ESP32 der ESP32-CAM. Board in der IDE pruefen!")
    if len(data) > MAX_APP_SIZE:
        die(f"Firmware ist {len(data)/1024:.0f} KB gross, erlaubt sind {MAX_APP_SIZE/1024:.0f} KB.")
    print(f"[bin] OK: {len(data)/1024:.0f} KB von max. {MAX_APP_SIZE/1024:.0f} KB")
    return data


# ---------------------------------------------------------------- Kamera ansprechen
def get_status(host: str, port: int, timeout: float = 3.0):
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/status")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status == 200:
            return json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return None


def password_ok(host: str, port: int, password: str) -> bool:
    """Leerer POST: die Firmware prueft zuerst das Passwort (403), lehnt dann die
    leere Datei ab (500). Es wird dabei nichts geschrieben."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("POST", "/update", body=b"",
                     headers={"X-OTA-Password": password, "Content-Length": "0"})
        status = conn.getresponse().status
        conn.close()
        return status != 403
    except (OSError, http.client.HTTPException):
        return True  # Unklar -> nicht blockieren, der echte Upload meldet es ohnehin


def upload(host: str, port: int, password: str, data: bytes) -> None:
    conn = http.client.HTTPConnection(host, port, timeout=30)
    conn.putrequest("POST", "/update")
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(len(data)))
    conn.putheader("X-OTA-Password", password)
    conn.endheaders()

    chunk = 4096
    sent = 0
    last_pct = -1
    t0 = time.monotonic()
    try:
        while sent < len(data):
            conn.send(data[sent:sent + chunk])
            sent += min(chunk, len(data) - sent)
            pct = sent * 100 // len(data)
            if pct != last_pct:
                last_pct = pct
                bar = "#" * (pct // 4)
                print(f"\r[upload] [{bar:<25}] {pct:3d}%  {sent/1024:.0f} KB", end="", flush=True)
        print(f"  ({time.monotonic() - t0:.1f} s)")
        resp = conn.getresponse()
        text = resp.read().decode(errors="replace").strip()
    except (OSError, http.client.HTTPException) as e:
        # Die Kamera kann nach falschem Passwort/Fehler die Verbindung frueh schliessen.
        die(f"Upload abgebrochen ({e}). Die alte Firmware laeuft weiter.")
    finally:
        conn.close()

    if resp.status == 403:
        die("Falsches Admin-Passwort.")
    if resp.status != 200:
        die(f"Kamera hat das Update abgelehnt ({resp.status}): {text}\n"
            "        Die alte Firmware laeuft weiter.")
    print(f"[upload] Kamera meldet: {text}")


def wait_for_reboot(host: str, port: int, timeout_s: int = 60):
    print("[reboot] Warte auf Neustart ", end="", flush=True)
    time.sleep(3)  # Kamera braucht mind. ein paar Sekunden
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = get_status(host, port, timeout=2)
        if st:
            print(" wieder online.")
            return st
        print(".", end="", flush=True)
        time.sleep(2)
    print()
    return None


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="ESP32-CAM Firmware-Update ueber WLAN")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"IP der Kamera (Standard {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--bin", type=Path, help="Pfad zur .ino.bin (Standard: neueste im Sketch-Ordner)")
    p.add_argument("--build", action="store_true", help="vorher mit arduino-cli kompilieren")
    p.add_argument("--yes", "-y", action="store_true", help="ohne Rueckfrage hochladen")
    args = p.parse_args()

    if args.build:
        build_firmware()

    bin_path = args.bin or find_newest_bin()
    data = check_bin(bin_path)

    print(f"[cam] Pruefe Kamera unter {args.host} ...")
    before = get_status(args.host, args.port)
    if before is None:
        die(f"Kamera unter {args.host} nicht erreichbar. IP richtig? Gleiches Netzwerk?\n"
            "        Notfall-Hotspot 'Billard-Cam-Setup' aktiv? Dann --host 192.168.4.1")
    print(f"[cam] Erreichbar. Kamera ok: {before.get('camera_ok')}, "
          f"WLAN: {before.get('rssi')} dBm, freier Speicher: {before.get('free_heap')} B")
    if not before.get("ap_mode") and before.get("rssi", 0) < -75:
        print("      Achtung: schwaches WLAN-Signal, der Upload kann abbrechen (ist aber ungefaehrlich).")

    password = os.environ.get("CAM_OTA_PASSWORD") or getpass.getpass("Admin-Passwort der Kamera: ")
    if not password_ok(args.host, args.port, password):
        die("Falsches Admin-Passwort (vor dem Upload geprueft, es wurde nichts geschrieben).")

    if not args.yes:
        ans = input(f"'{bin_path.name}' ({len(data)/1024:.0f} KB) auf {args.host} flashen? [j/N] ")
        if ans.strip().lower() not in ("j", "ja", "y", "yes"):
            print("Abgebrochen.")
            return 0

    upload(args.host, args.port, password, data)

    after = wait_for_reboot(args.host, args.port)
    if after is None:
        die("Kamera ist nach 60 s nicht wieder erreichbar.\n"
            "        1. Strom kurz trennen und 1 Minute warten.\n"
            "        2. Nach WLAN 'Billard-Cam-Setup' suchen (dann WLAN-Daten falsch)\n"
            "           damit verbinden und auf http://192.168.4.1 die WLAN-Daten korrigieren.")
    if after.get("ap_mode"):
        print("[WARNUNG] Kamera laeuft im Einrichtungs-Hotspot, WLAN-Daten auf der Einrichtungsseite pruefen.")
    elif not after.get("camera_ok"):
        print("[WARNUNG] Firmware laeuft, aber die Kamera wurde nicht erkannt (Flachbandkabel/Strom pruefen).")
    else:
        print(f"[fertig] Update erfolgreich. Stream: http://{args.host}:81/stream")
    print("         Der Viewer auf dem Pi verbindet sich automatisch neu.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        sys.exit(130)
