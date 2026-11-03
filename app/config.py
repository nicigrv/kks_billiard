"""Einstellungen dauerhaft in /data/config.json (Docker-Volume), ueberleben Neustarts."""

import json
import os
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PATH = os.path.join(DATA_DIR, "config.json")

DEFAULTS = {
    "corners": None,            # 4 Ecken der Spielflaeche im Kamerabild
    "table_size": "8ft",
    "record_dataset": False,    # Trainingsbilder fuer das Coral-Modell sammeln
}

_lock = threading.Lock()


def load():
    cfg = dict(DEFAULTS)
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
