"""Einstellungen dauerhaft in /data/config.json (Docker-Volume), ueberleben Neustarts."""

import copy
import json
import os
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "camera": None,             # Kameraquelle (picam | usb | demo), siehe cameras.py
    "corners_by_source": None,  # 4 Ecken der Spielflaeche je Kameraquelle
    "table_size": "8ft",
    "cloth": "blau",            # Tuchfarbe fuer die Grafik (blau | gruen)
    "assist": False,            # KI-Schlagverbesserung (Stossvorschlag + Queue-Lage)
    "cue_offset": 0.0,          # Gyroskop: Rohwinkel, wenn der Queue an der Bande liegt
    "cue_flip": False,          # Gyroskop: Drehrichtung umkehren
    "cue_tolerance": 2.0,       # Gyroskop: ab dieser Abweichung (Grad) gibt es den gruenen Haken
    "auto_capture": False,      # nach jedem Stoss automatisch ein Trainingsbild speichern
    "cue_enabled": True,        # Queue mit Gyroskop benutzen (optional, im Spiel-Panel schaltbar)
    # QR-Code unten rechts auf dem Beamer. Leerer Text = Adresse des Spiel-Panels.
    "qr": {"enabled": True, "text": "", "label": "Spiel-Panel"},
}

_lock = threading.Lock()


class UserError(ValueError):
    """Fehler mit verstaendlicher Meldung fuer das Portal."""


def load():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, PATH)   # atomar: kein halbes File bei Stromausfall
