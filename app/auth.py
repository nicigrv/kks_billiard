"""
Anmeldung am Portal.

  * Admin: PIN (ADMIN_PIN in docker-compose.yml, z. B. 1902) -> Admin-Panel /admin und alles andere.
    Ist keine PIN gesetzt, ist jeder Admin.
  * Spiel: PIN (GAME_PIN, z. B. 1212) -> Spiel-Panel unter / (Spiel starten, Korrekturen,
    Schlagverbesserung, Queue an/aus). Ohne GAME_PIN ist das Spiel-Panel offen.
  * Pruefer: eigener 4-stelliger Code, den der Admin im Admin-Panel vergibt (config.json).

Nach der Anmeldung merkt sich der Browser ein Sitzungs-Cookie (30 Tage). So funktionieren
auch Bilder und Download-Links, die keine Kopfzeilen mitschicken koennen. Sitzungen
ueberleben einen Neustart (data/sitzungen.json). Zu viele Fehlversuche von einem Geraet
sperren die Anmeldung kurz.
"""

import json
import os
import secrets
import threading
import time

COOKIE = "billard"
MAX_AGE = 30 * 24 * 3600


class LoginError(ValueError):
    pass


class Auth:
    def __init__(self, pin, data_dir, reviewers, clock=time.time, game_pin=""):
        """reviewers: Funktion, die die aktuelle Pruefer-Liste [{"name", "code"}] liefert."""
        self.pin = pin or ""
        self.game_pin = game_pin or ""
        self.path = os.path.join(data_dir, "sitzungen.json")
        self.reviewers = reviewers
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
            return {t: s for t, s in data.items() if now - s.get("t", 0) < MAX_AGE}
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
        """PIN oder Pruefer-Code pruefen. Liefert (sitzung, token)."""
        secret = str(secret or "").strip()
        now = self.clock()
        with self.lock:
            recent = [t for t in self.fails.get(ip, []) if now - t < 60]
            if len(recent) >= 5:
                raise LoginError("Zu viele Fehlversuche - bitte eine Minute warten")
            sess = None
            if self.pin and secret == self.pin:
                sess = {"role": "admin", "name": "Admin"}
            elif self.game_pin and secret == self.game_pin:
                sess = {"role": "spiel", "name": "Spiel"}
            else:
                for r in self.reviewers():
                    if secret and secret == str(r.get("code")):
                        sess = {"role": "pruefer", "name": r["name"]}
                        break
            if sess is None:
                self.fails[ip] = recent + [now]
                raise LoginError("Falscher PIN bzw. Code")
            self.fails.pop(ip, None)
            token = secrets.token_urlsafe(24)
            self.sessions[token] = dict(sess, t=now)
            self._save()
            return sess, token

    def logout(self, token):
        with self.lock:
            if self.sessions.pop(token, None) is not None:
                self._save()

    def revoke(self, name):
        """Pruefer entfernt / neuer Code: seine Sitzungen beenden."""
        with self.lock:
            gone = [t for t, s in self.sessions.items() if s["role"] == "pruefer" and s["name"] == name]
            for t in gone:
                del self.sessions[t]
            if gone:
                self._save()

    # --------------------------------------------------------------- Abfrage
    def who(self, cookie_header, pin_header=""):
        """-> {"role": "admin"|"spiel"|"pruefer"|None, "name": ...} fuer eine Anfrage."""
        if not self.pin:
            return {"role": "admin", "name": "Admin", "open": True}
        if pin_header and pin_header == self.pin:
            return {"role": "admin", "name": "Admin"}
        token = self.token_from(cookie_header)
        with self.lock:
            s = self.sessions.get(token)
            if s is not None and s["role"] == "pruefer" and \
                    not any(r["name"] == s["name"] for r in self.reviewers()):
                s = None                                      # Pruefer wurde entfernt
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
