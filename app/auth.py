"""
Anmeldung am Portal.

  * Admin: PIN (ADMIN_PIN in docker-compose.yml, z. B. 1902) -> Admin-Panel /admin und alles andere.
    Ist keine PIN gesetzt, ist jeder Admin.
  * Spiel: PIN (GAME_PIN, z. B. 1212) -> Spiel-Panel unter / (Spiel starten, Korrekturen,
    Schlagverbesserung, Queue an/aus). Ohne GAME_PIN ist das Spiel-Panel offen.
  (Pruefer mit eigenen Codes gibt es am Pi nicht mehr - festgelegt wird auf der Trainingsseite im Netz.)

Nach der Anmeldung merkt sich der Browser ein Sitzungs-Cookie (30 Tage). So funktionieren
auch Bilder und Download-Links, die keine Kopfzeilen mitschicken koennen. Sitzungen
ueberleben einen Neustart (data/sitzungen.json). Zu viele Fehlversuche von einem Geraet
sperren die Anmeldung kurz.
"""

import hmac
import json
import os
import secrets
import threading
import time

COOKIE = "billard"
MAX_AGE = 30 * 24 * 3600
MAX_FAILS = 5             # so viele Fehlversuche je Geraet ...
FAIL_WINDOW_S = 60        # ... innerhalb dieser Zeit sperren die Anmeldung kurz


def same(a, b):
    """Vergleich ohne Zeitunterschied je nach Zeichen (PIN nicht per Stoppuhr erratbar)."""
    return bool(a) and hmac.compare_digest(str(a).encode(), str(b).encode())


class LoginError(ValueError):
    pass


class Auth:
    ROLES = ("admin", "spiel")

    def __init__(self, pin, data_dir, clock=time.time, game_pin=""):
        self.pin = pin or ""
        self.game_pin = game_pin or ""
        self.path = os.path.join(data_dir, "sitzungen.json")
        self.clock = clock
        self.lock = threading.Lock()
        self.fails = {}
        self.sessions = self._load()

    # ------------------------------------------------------------ Sitzungen
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            now = self.clock()
            # alte Pruefer-Sitzungen (fruehere Versionen) gelten nicht mehr
            return {t: s for t, s in data.items() if now - s.get("t", 0) < MAX_AGE and s.get("role") in self.ROLES}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.sessions, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def login(self, secret, ip=""):
        """Admin- oder Spiel-PIN pruefen. Liefert (sitzung, token)."""
        secret = str(secret or "").strip()
        now = self.clock()
        with self.lock:
            recent = self._recent_fails(ip, now)
            if len(recent) >= MAX_FAILS:
                raise LoginError("Zu viele Fehlversuche - bitte eine Minute warten")
            sess = None
            if self.pin and same(secret, self.pin):
                sess = {"role": "admin", "name": "Admin"}
            elif self.game_pin and same(secret, self.game_pin):
                sess = {"role": "spiel", "name": "Spiel"}
            if sess is None:
                self.fails[ip] = recent + [now]
                raise LoginError("Falsche PIN")
            self.fails.pop(ip, None)
            token = secrets.token_urlsafe(24)
            self.sessions[token] = dict(sess, t=now)
            self._save()
            return sess, token

    def _recent_fails(self, ip, now):
        """Fehlversuche dieses Geraets in der letzten Minute (alte Eintraege werden vergessen)."""
        recent = [t for t in self.fails.get(ip, []) if now - t < FAIL_WINDOW_S]
        if recent:
            self.fails[ip] = recent
        else:
            self.fails.pop(ip, None)
        return recent

    def logout(self, token):
        with self.lock:
            if self.sessions.pop(token, None) is not None:
                self._save()

    # --------------------------------------------------------------- Abfrage
    def who(self, cookie_header, pin_header="", ip=""):
        """-> {"role": "admin"|"spiel"|None, "name": ...} fuer eine Anfrage.
        pin_header: Admin-PIN als Kopfzeile X-Admin-Pin (fuer Skripte, z. B. sim/fake_cue.py) -
        Fehlversuche zaehlen wie bei der Anmeldung, sonst liesse sich die PIN durchprobieren."""
        if not self.pin:
            return {"role": "admin", "name": "Admin", "open": True}
        token = self.token_from(cookie_header)
        with self.lock:
            if pin_header:
                now = self.clock()
                recent = self._recent_fails(ip, now)
                if len(recent) < MAX_FAILS and same(pin_header, self.pin):
                    return {"role": "admin", "name": "Admin"}
                self.fails[ip] = recent + [now]
            s = self.sessions.get(token)
            if s is not None and self.clock() - s.get("t", 0) >= MAX_AGE:
                self.sessions.pop(token, None)                # abgelaufen (auch ohne Neustart)
                s = None
            if s is None:
                if not self.game_pin:                         # Spiel-Panel ohne PIN: offen
                    return {"role": "spiel", "name": "Spiel", "open": True}
                return {"role": None, "name": None}
            return {"role": s["role"], "name": s["name"]}

    @staticmethod
    def can(role, need):
        """Darf diese Rolle das? Admin darf alles, was das Spiel-Panel darf."""
        return role == need or (role == "admin" and need == "spiel")

    @staticmethod
    def token_from(cookie_header):
        for part in (cookie_header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    @staticmethod
    def set_cookie(token):
        return f"{COOKIE}={token}; Path=/; Max-Age={MAX_AGE}; HttpOnly; SameSite=Lax"

    @staticmethod
    def clear_cookie():
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
