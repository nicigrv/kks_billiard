"""
Gyroskop im Queue: Der Pi fragt die Lage des Queues per WLAN ab (Firmware: cue_sensor/)
und vergleicht sie mit dem Stossvorschlag.

Koppeln ueber den CASTCODE (6 Zeichen, zeigt der Queue in seinem Einrichtungs-Hotspot):
  1. Im Admin-Panel unter "Queue mit Gyroskop" den Castcode eintragen.
  2. Der Pi sucht den Queue per UDP-Broadcast {"suche": "<castcode>"} an Port 5005;
     der Queue mit diesem Code antwortet mit seiner IP.
  3. Danach fragt der Pi ca. 25x pro Sekunde  GET http://<queue>/api/lage  ab
     (Kopfzeile X-Castcode). Antwort:
     {"castcode": "K7P2QM", "seq": 123, "yaw": 37.5, "pitch": -4.2, "still": 0, "bat": 3.91}
     yaw   = Richtung in der Waagerechten, Grad, gegen den Uhrzeigersinn positiv
     pitch = Neigung (Spitze nach unten negativ)
  Ist der Queue nicht mehr erreichbar (neue IP nach Neustart), sucht der Pi ihn neu.
  Der Queue ist optional: ausgeschaltet (enabled=False) sucht und fragt der Pi nichts ab.

Ein Gyroskop kennt nur Drehungen, nicht die Richtung zum Tisch. Deshalb einmal
kalibrieren: Queue an die lange Bande legen, Spitze zeigt in der Grafik nach RECHTS,
im Spiel- oder Admin-Panel "Queue kalibrieren" druecken. Ab dann: Tischwinkel = Abweichung davon.
Die Position des Queues kann ein Gyroskop nicht messen - der helle Queue wird in
der Grafik deshalb immer hinter die Weisse gezeichnet, gedreht um den gemessenen Winkel.
"""

import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from config import UserError

STALE_S = 1.0          # aelter = Queue gilt als nicht verbunden
CASTCODE_RE = re.compile(r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}")


def norm_angle(a):
    """Auf (-180, 180] bringen."""
    a = math.fmod(a, 360.0)
    if a <= -180:
        a += 360
    elif a > 180:
        a -= 360
    return a


def angle_diff(a, b):
    return norm_angle(a - b)


def clean_castcode(code):
    """Eingabe vereinheitlichen: Grossbuchstaben, ohne Leer-/Bindestriche, O->0 gibt es nicht."""
    c = re.sub(r"[\s\-]", "", str(code or "")).upper()
    if not CASTCODE_RE.fullmatch(c):
        raise UserError("Castcode: 6 Zeichen, wie sie der Queue im Einrichtungs-Hotspot zeigt")
    return c


class CueSensor:
    def __init__(self, castcode="", offset=0.0, flip=False, discovery_port=5005, address=None,
                 on_found=None, poll_hz=25.0, clock=time.monotonic, enabled=True):
        self.castcode = castcode or ""
        self.enabled = bool(enabled)
        self.offset = float(offset)
        self.flip = bool(flip)
        self.discovery_port = discovery_port
        # Broadcast reicht im Heimnetz; 127.0.0.1 zusaetzlich fuer den Simulator auf demselben Rechner
        self.discovery_addrs = [a for a in os.environ.get("CUE_DISCOVERY_ADDR", "255.255.255.255,127.0.0.1").split(",") if a]
        self.address = tuple(address) if address else None    # (ip, port), zuletzt bekannt
        self.on_found = on_found
        self.poll_s = 1.0 / poll_hz
        self.clock = clock
        self.lock = threading.Lock()
        self.data = None
        self.t = 0.0
        self.addr = None
        self.packets = 0
        self.last_error = ""
        self.state = self._idle_state()
        self._stop = threading.Event()
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="queue", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _idle_state(self):
        if not self.castcode:
            return "kein Castcode eingetragen"
        return "suche Queue ..." if self.enabled else "ausgeschaltet"

    def pair(self, castcode):
        """Neuen Castcode eintragen (leer = entkoppeln)."""
        with self.lock:
            self.castcode = castcode or ""
            self.address = None
            self.data = None
            self.state = self._idle_state()
        self._wake.set()

    def set_enabled(self, enabled):
        """Queue benutzen ja/nein. Aus: keine Suche, keine Abfragen, gilt als nicht verbunden."""
        with self.lock:
            self.enabled = bool(enabled)
            if not self.enabled:
                self.data = None
            self.state = self._idle_state()
        self._wake.set()

    # ------------------------------------------------------------- Suche
    def discover(self, code, timeout=1.5):
        """Queue mit diesem Castcode im Netz suchen -> (ip, port) oder None."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.3)
            msg = json.dumps({"suche": code}).encode()
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                for a in self.discovery_addrs:
                    try:
                        sock.sendto(msg, (a, self.discovery_port))
                    except OSError:
                        pass
                try:
                    raw, src = sock.recvfrom(512)
                    d = json.loads(raw)
                except (socket.timeout, ValueError):
                    continue
                if str(d.get("castcode", "")).upper() == code:
                    return str(d.get("ip") or src[0]), int(d.get("port", 80))
            return None
        finally:
            sock.close()

    def fetch(self, address, code, timeout=0.6):
        req = urllib.request.Request(f"http://{address[0]}:{address[1]}/api/lage", headers={"X-Castcode": code})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def _loop(self):
        fails = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            code, address = self.castcode, self.address
            if not code or not self.enabled:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if address is None:
                self.state = "suche Queue ..."
                found = self.discover(code)
                if found is None:
                    self._wake.wait(2.0)
                    self._wake.clear()
                    continue
                with self.lock:
                    if self.castcode != code:
                        continue
                    self.address = address = found
                fails = 0
                print(f"[queue] Queue {code} gefunden: {found[0]}:{found[1]}", flush=True)
                if self.on_found:
                    self.on_found(found)
            try:
                raw = self.fetch(address, code)
                if self.enabled and self.castcode == code and self.feed(raw, address):
                    fails = 0
                    self.state = "verbunden"
                    self.last_error = ""
            except urllib.error.HTTPError as e:
                self.last_error = "Castcode passt nicht zum Queue" if e.code == 403 else f"HTTP {e.code}"
                fails += 1
            except (OSError, ValueError) as e:
                self.last_error = str(e)
                fails += 1
            if not self.enabled:
                continue
            if fails >= 10:                     # ~2-6 s nichts: neue IP? neu suchen
                with self.lock:
                    if self.castcode == code:
                        self.address = None
                fails = 0
                self.state = "Queue nicht erreichbar - suche neu ..."
            self._stop.wait(max(0.0, self.poll_s - (time.monotonic() - t0)))

    def feed(self, raw, addr=None):
        """Ein Paket verarbeiten (auch direkt aus Tests aufrufbar)."""
        try:
            d = json.loads(raw)
            yaw = float(d["yaw"])
            if not math.isfinite(yaw):
                raise ValueError("yaw")
        except (ValueError, KeyError, TypeError):
            return False
        with self.lock:
            self.data = d
            self.t = self.clock()
            self.addr = addr[0] if addr else None
            self.packets += 1
        return True

    @property
    def connected(self):
        return self.data is not None and self.clock() - self.t < STALE_S

    def raw_yaw(self):
        with self.lock:
            return float(self.data["yaw"]) if self.data else None

    def table_angle(self):
        """Richtung des Queues im Koordinatensystem der Grafik (0 = rechts, 90 = unten)."""
        if not self.connected:
            return None
        a = self.raw_yaw() - self.offset
        # Gyro: gegen den Uhrzeigersinn positiv; Grafik: y nach unten -> Vorzeichen drehen
        return norm_angle(a if self.flip else -a)

    def calibrate(self):
        """Queue liegt an der langen Bande, Spitze zeigt nach rechts -> das ist 0 Grad."""
        if not self.connected:
            raise UserError("Queue nicht verbunden - ist das Gyroskop eingeschaltet?")
        self.offset = self.raw_yaw()
        return self.offset

    def status(self):
        with self.lock:
            d = dict(self.data) if self.data else {}
        return {
            "castcode": self.castcode,
            "enabled": self.enabled,
            "state": "verbunden" if self.connected else self.state,
            "connected": self.connected,
            "angle": None if self.table_angle() is None else round(self.table_angle(), 1),
            "pitch": d.get("pitch"),
            "battery": d.get("bat"),
            "id": d.get("id"),
            "address": None if not self.address else f"{self.address[0]}:{self.address[1]}",
            "packets": self.packets,
            "flip": self.flip,
            "error": self.last_error,
        }


class AlignChecker:
    """Passt der Queue zum Vorschlag? Erst nach kurzer ruhiger Zeit 'ok' (kein Flackern),
    und erst bei deutlich groesserer Abweichung wieder 'nicht ok' (Hysterese)."""

    def __init__(self, tolerance=2.0, hold_s=0.4, release_factor=1.75, clock=time.monotonic):
        self.tolerance = float(tolerance)
        self.hold_s = hold_s
        self.release_factor = release_factor
        self.clock = clock
        self.ok = False
        self.err = None
        self._since = None

    def update(self, actual, target):
        now = self.clock()
        if actual is None or target is None:
            self.ok, self.err, self._since = False, None, None
            return self.ok
        self.err = angle_diff(actual, target)
        e = abs(self.err)
        if self.ok:
            if e > self.tolerance * self.release_factor:
                self.ok, self._since = False, None
        elif e <= self.tolerance:
            if self._since is None:
                self._since = now
            if now - self._since >= self.hold_s:
                self.ok = True
        else:
            self._since = None
        return self.ok
