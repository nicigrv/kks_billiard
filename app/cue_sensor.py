"""
Gyroskop im Queue: empfaengt die Lage des Queues per WLAN (UDP) vom ESP32 im Queue
(Firmware: cue_sensor/) und vergleicht sie mit dem Stossvorschlag.

Paket (JSON, ca. 30x pro Sekunde, Broadcast an Port 5005):
    {"id": "koe1", "seq": 123, "yaw": 37.5, "pitch": -4.2, "still": 0, "bat": 3.91}
    yaw   = Richtung des Queues in der Waagerechten, Grad, gegen den Uhrzeigersinn positiv
    pitch = Neigung (Spitze nach unten negativ)

Ein Gyroskop kennt nur Drehungen, nicht die Richtung zum Tisch. Deshalb einmal
kalibrieren: Queue an die lange Bande legen, Spitze zeigt in der Grafik nach RECHTS,
im Portal "Queue kalibrieren" druecken. Ab dann: Tischwinkel = Abweichung davon.
Die Position des Queues kann ein Gyroskop nicht messen - der helle Queue wird in
der Grafik deshalb immer hinter die Weisse gezeichnet, gedreht um den gemessenen Winkel.
"""

import json
import math
import socket
import threading
import time

from config import UserError

STALE_S = 1.0          # aelter = Queue gilt als nicht verbunden


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


class CueSensor:
    def __init__(self, port=5005, offset=0.0, flip=False, clock=time.monotonic):
        self.port = port
        self.offset = float(offset)
        self.flip = bool(flip)
        self.clock = clock
        self.lock = threading.Lock()
        self.data = None
        self.t = 0.0
        self.addr = None
        self.packets = 0
        self.last_error = ""
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="queue", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", self.port))
                sock.settimeout(1.0)
                print(f"[queue] warte auf Gyroskop-Daten auf UDP-Port {self.port}", flush=True)
                while not self._stop.is_set():
                    try:
                        raw, addr = sock.recvfrom(1024)
                    except socket.timeout:
                        continue
                    self.feed(raw, addr)
            except OSError as e:
                self.last_error = str(e)
                print(f"[queue] Fehler: {e}", flush=True)
                time.sleep(5)

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
            "connected": self.connected,
            "angle": None if self.table_angle() is None else round(self.table_angle(), 1),
            "pitch": d.get("pitch"),
            "battery": d.get("bat"),
            "id": d.get("id"),
            "address": self.addr,
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
