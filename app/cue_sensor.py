"""
Gyroskop im Queue: Der Queue sendet seine Lage per WLAN an den Pi (Firmware: cue_sensor/),
der Pi vergleicht sie mit dem Stossvorschlag.

Koppeln ueber den CASTCODE (6 Zeichen, zeigt der Queue in seinem Einrichtungs-Hotspot):
  1. Im Admin-Panel unter "Queue mit Gyroskop" den Castcode eintragen.
  2. Der Pi sucht den Queue per UDP-Broadcast {"suche": "<castcode>"} an Port 5005;
     der Queue mit diesem Code antwortet mit seiner IP.
  3. Danach abonniert der Pi die Lage: {"abo": "<castcode>", "farbe": [r, g, b] | null} an
     den Queue (UDP 5005). Der Queue SENDET dann von selbst ca. 50x pro Sekunde an den Pi:
     {"castcode": "K7P2QM", "seq": 123, "yaw": 37.5, "pitch": -4.2, "still": 0, "bat": 3.91,
      "rssi": -55, "knopf": 0}
     yaw   = Richtung in der Waagerechten, Grad, gegen den Uhrzeigersinn positiv
     pitch = Neigung (Spitze nach unten negativ)
     knopf = zaehlt Knopfdruecke am Queue (bei gruener LED) -> der Pi startet ein Spiel
  Das Abo wiederholt der Pi jede Sekunde (Lebenszeichen + Teamfarbe fuer die LED; bei
  Farbwechsel sofort). Kommen 3 s keine Daten, sucht der Pi den Queue neu (neue IP?).
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

from config import UserError

STALE_S = 1.0          # aelter = Queue gilt als nicht verbunden
ABO_EVERY_S = 1.0      # Abo wiederholen (Lebenszeichen fuer den Queue, sonst LED rot)
LOST_S = 3.0           # so lange keine Daten -> Queue neu suchen
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
                 on_found=None, poll_hz=25.0, clock=time.monotonic, enabled=True,
                 on_button=None, led_color=None):
        self.castcode = castcode or ""
        self.enabled = bool(enabled)
        self.offset = float(offset)
        self.flip = bool(flip)
        self.discovery_port = discovery_port
        # Broadcast reicht im Heimnetz; 127.0.0.1 zusaetzlich fuer den Simulator auf demselben Rechner
        self.discovery_addrs = [a for a in os.environ.get("CUE_DISCOVERY_ADDR", "255.255.255.255,127.0.0.1").split(",") if a]
        self.address = tuple(address) if address else None    # (ip, port), zuletzt bekannt
        self.on_found = on_found
        self.on_button = on_button        # Knopf am Queue gedrueckt (bei gruener LED)
        self.led_color = led_color        # -> [r, g, b] des Teams, das dran ist, oder None (kein Spiel)
        self.poll_s = 1.0 / poll_hz       # nur noch fuer Tests/Kompatibilitaet; der Queue sendet selbst
        self.listen_port = int(os.environ.get("CUE_LISTEN_PORT", "0"))   # 0 = beliebig
        self.clock = clock
        self.lock = threading.Lock()
        self.data = None
        self.t = 0.0
        self.addr = None
        self.packets = 0
        self.lost = 0
        self.rate = 0.0
        self.button = None                # zuletzt gesehener Zaehler "knopf"
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

    # ------------------------------------------------------------- Netz
    def _socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self.listen_port))
        return sock

    def discover(self, code, timeout=1.5, sock=None):
        """Queue mit diesem Castcode im Netz suchen -> (ip, port) oder None."""
        own = sock is None
        sock = sock or self._socket()
        try:
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
                    raw, src = sock.recvfrom(1024)
                    d = json.loads(raw)
                except (socket.timeout, ValueError):
                    continue
                if str(d.get("castcode", "")).upper() == code and "ip" in d:
                    return str(d.get("ip") or src[0]), self.discovery_port
            return None
        finally:
            if own:
                sock.close()

    def _color(self):
        if self.led_color is None:
            return None
        try:
            c = self.led_color()
            return None if c is None else [int(v) for v in c][:3]
        except Exception:
            return None

    def _subscribe(self, sock, address, code, color):
        msg = json.dumps({"abo": code, "farbe": color}).encode()
        try:
            sock.sendto(msg, address)
        except OSError as e:
            self.last_error = str(e)

    def _loop(self):
        sock = None
        while not self._stop.is_set():
            code = self.castcode
            if not code or not self.enabled:
                if sock is not None:
                    sock.close()
                    sock = None
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if sock is None:
                try:
                    sock = self._socket()
                except OSError as e:
                    self.last_error = str(e)
                    self._stop.wait(2.0)
                    continue
            address = self.address
            if address is None:
                self.state = "suche Queue ..."
                found = self.discover(code, sock=sock)
                if found is None:
                    self._wake.wait(2.0)
                    self._wake.clear()
                    continue
                with self.lock:
                    if self.castcode != code:
                        continue
                    self.address = address = found
                print(f"[queue] Queue {code} gefunden: {found[0]}", flush=True)
                if self.on_found:
                    self.on_found(found)
            self._run_abo(sock, address, code)
        if sock is not None:
            sock.close()

    def _run_abo(self, sock, address, code):
        """Abonnieren und empfangen, bis der Queue weg ist, ein anderer gekoppelt wird oder aus."""
        color = self._color()
        self._subscribe(sock, address, code, color)
        sent, got = time.monotonic(), time.monotonic()
        win_t, win_n = time.monotonic(), 0
        sock.settimeout(0.05)
        while not self._stop.is_set() and self.enabled and self.castcode == code and self.address == address:
            now = time.monotonic()
            c = self._color()
            if c != color or now - sent >= ABO_EVERY_S:      # Lebenszeichen; neue Teamfarbe sofort
                color, sent = c, now
                self._subscribe(sock, address, code, color)
            try:
                raw, src = sock.recvfrom(1024)
            except socket.timeout:
                raw = None
            except OSError as e:
                self.last_error = str(e)
                raw = None
            if raw is not None and src[0] == address[0] and self.enabled and self.castcode == code \
                    and self.feed(raw, src):
                got = time.monotonic()
                win_n += 1
                self.state = "verbunden"
                self.last_error = ""
            if now - win_t >= 1.0:
                self.rate, win_t, win_n = win_n / (now - win_t), now, 0
            if time.monotonic() - got > LOST_S:          # neue IP nach Neustart? neu suchen
                with self.lock:
                    if self.address == address:
                        self.address = None
                self.state = "Queue nicht erreichbar - suche neu ..."
                self.rate = 0.0
                return
        with self.lock:                             # aus/entkoppelt: Status nicht vom letzten Paket ueberschreiben
            if not self.enabled or self.castcode != code:
                self.data = None if not self.enabled else self.data
                self.state = self._idle_state()
                self.rate = 0.0

    def feed(self, raw, addr=None):
        """Ein Paket verarbeiten (auch direkt aus Tests aufrufbar)."""
        try:
            d = json.loads(raw)
            yaw = float(d["yaw"])
            if not math.isfinite(yaw):
                raise ValueError("yaw")
            if self.castcode and str(d.get("castcode", self.castcode)).upper() != self.castcode:
                return False                        # fremder Queue
        except (ValueError, KeyError, TypeError):
            return False
        pressed = False
        with self.lock:
            old = self.data
            if old and "seq" in d and "seq" in old:
                gap = int(d["seq"]) - int(old["seq"]) - 1
                if 0 < gap < 1000:
                    self.lost += gap
            self.data = d
            self.t = self.clock()
            self.addr = addr[0] if addr else None
            self.packets += 1
            k = d.get("knopf")
            if isinstance(k, int):
                pressed = self.button is not None and k > self.button
                self.button = k
        if pressed and self.on_button:
            try:
                self.on_button()
            except Exception as e:                  # Knopf darf den Empfang nie stoppen
                print(f"[queue] Knopf: {e}", flush=True)
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
            "rate": round(self.rate, 1),
            "lost": self.lost,
            "rssi": d.get("rssi"),
            "fw": d.get("fw"),
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
