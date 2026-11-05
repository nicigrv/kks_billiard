"""
Trainingsbilder fuer das Coral-Modell.

Ablauf:
  1. Nach jedem Spielzug (Tisch wieder ruhig) wird die entzerrte Draufsicht gespeichert,
     zusammen mit dem, was die Erkennung zugeordnet hat (Position, Typ, Farbe/Nummer).
     -> dataset/pruefen/<name>.jpg + <name>.json
  2. Auf dem iPad wird jedes Bild angesehen: "passt alles" oder korrigieren (oder verwerfen).
     -> dataset/geprueft/images/<name>.jpg
        dataset/geprueft/labels/<name>.txt    YOLO-Format: Klasse x_mitte y_mitte breite hoehe (0..1)
        dataset/geprueft/labels/<name>.json   mit Kugelnummern
  3. Nur geprueft/ wird zum Training benutzt (training/train_coral.py).

Klassen (Reihenfolge ist fest, das Modell liefert dieselben Nummern): cue, eight, solid, stripe
"""

import json
import os
import re
import shutil
import threading
import time
import zipfile

import cv2

CLASSES = ["cue", "eight", "solid", "stripe"]
NAME_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{3}")


def _ball_dict(b, number=None):
    if isinstance(b, dict):
        return {"x": float(b["x"]), "y": float(b["y"]), "kind": b["kind"], "color": b.get("color", "") or "",
                "number": b.get("number")}
    return {"x": float(b.x), "y": float(b.y), "kind": b.kind, "color": getattr(b, "color", "") or "",
            "number": number}


def write_yolo(path, balls, radius, w, h):
    with open(path, "w") as f:
        for b in balls:
            if b["kind"] in CLASSES:
                f.write(f"{CLASSES.index(b['kind'])} {b['x'] / w:.5f} {b['y'] / h:.5f} "
                        f"{2 * radius / w:.5f} {2 * radius / h:.5f}\n")


class Recorder:
    def __init__(self, base_dir, max_pending=500, max_verified=5000, min_interval_s=2.0):
        self.base_dir = base_dir
        self.pending_dir = os.path.join(base_dir, "pruefen")
        self.ok_img = os.path.join(base_dir, "geprueft", "images")
        self.ok_lbl = os.path.join(base_dir, "geprueft", "labels")
        self.max_pending = max_pending
        self.max_verified = max_verified
        self.min_interval_s = min_interval_s
        self.enabled = True
        self.lock = threading.Lock()
        self._last = 0.0

    # ------------------------------------------------------------- Zaehler
    @staticmethod
    def _count(folder, ext):
        try:
            return sum(1 for n in os.listdir(folder) if n.endswith(ext))
        except OSError:
            return 0

    @property
    def pending(self):
        return self._count(self.pending_dir, ".json")

    @property
    def verified(self):
        return self._count(self.ok_img, ".jpg")

    def _new_name(self):
        """Zeitstempel als Name; ist er schon vergeben (gleiche Millisekunde), eins weiter."""
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        ms = int(now * 1000) % 1000
        while True:
            name = f"{stamp}-{ms:03d}"
            if not any(os.path.exists(os.path.join(d, name + ext)) for d, ext in
                       ((self.pending_dir, ".jpg"), (self.ok_img, ".jpg"))):
                return name
            ms += 1
            if ms > 999:
                time.sleep(0.001)
                return self._new_name()

    # ------------------------------------------------------ 1. automatisch sammeln
    def save_pending(self, warped, balls, radius, numbers=None):
        """Nach einem Spielzug: Bild + automatische Zuordnung in die Pruef-Liste."""
        if not self.enabled or not balls:
            return None
        now = time.time()
        if now - self._last < self.min_interval_s or self.pending >= self.max_pending:
            return None
        if self.verified >= self.max_verified:
            return None
        self._last = now
        numbers = numbers or [None] * len(balls)
        with self.lock:
            os.makedirs(self.pending_dir, exist_ok=True)
            name = self._new_name()
            h, w = warped.shape[:2]
            cv2.imwrite(os.path.join(self.pending_dir, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
            data = {"width": w, "height": h, "radius": float(radius),
                    "balls": [_ball_dict(b, n) for b, n in zip(balls, numbers)]}
            tmp = os.path.join(self.pending_dir, name + ".json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, os.path.join(self.pending_dir, name + ".json"))
        return name

    # --------------------------------------------------------- 2. auf dem iPad pruefen
    def next_pending(self, after=None):
        """Aeltestes noch nicht geprueftes Bild (oder das naechste nach 'after')."""
        try:
            names = sorted(n[:-5] for n in os.listdir(self.pending_dir) if n.endswith(".json"))
        except OSError:
            return None
        if after:
            names = [n for n in names if n > after] or names
        for name in names:
            try:
                with open(os.path.join(self.pending_dir, name + ".json")) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            data["name"] = name
            data["pending"] = len(names)
            return data
        return None

    def pending_image(self, name):
        if not NAME_RE.fullmatch(name or ""):
            return None
        p = os.path.join(self.pending_dir, name + ".jpg")
        return p if os.path.isfile(p) else None

    def review(self, name, balls, discard=False):
        """Ergebnis der Pruefung: Bild verwerfen oder mit (ggf. korrigierter) Zuordnung uebernehmen."""
        if not NAME_RE.fullmatch(name or ""):
            raise ValueError("Bildname")
        with self.lock:
            jpg = os.path.join(self.pending_dir, name + ".jpg")
            js = os.path.join(self.pending_dir, name + ".json")
            if not os.path.isfile(js):
                raise FileNotFoundError("Bild wurde schon geprüft")
            if discard:
                for p in (jpg, js):
                    if os.path.exists(p):
                        os.remove(p)
                return "verworfen"
            with open(js) as f:
                meta = json.load(f)
            changed = any(b.get("changed") for b in balls)
            balls = [_ball_dict(b) for b in balls if b["kind"] != "none"]
            os.makedirs(self.ok_img, exist_ok=True)
            os.makedirs(self.ok_lbl, exist_ok=True)
            self._write_verified(name, balls, meta["radius"], meta["width"], meta["height"], changed)
            shutil.move(jpg, os.path.join(self.ok_img, name + ".jpg"))
            os.remove(js)
        return "übernommen"

    def _write_verified(self, name, balls, radius, w, h, changed):
        write_yolo(os.path.join(self.ok_lbl, name + ".txt"), balls, radius, w, h)
        with open(os.path.join(self.ok_lbl, name + ".json"), "w") as f:
            json.dump({"radius": radius, "width": w, "height": h, "corrected": bool(changed),
                       "balls": balls}, f)

    def save_verified(self, warped, balls, radius, numbers=None):
        """Live-Bild, das im Portal korrigiert wurde: direkt als geprueft speichern."""
        numbers = numbers or [None] * len(balls)
        with self.lock:
            os.makedirs(self.ok_img, exist_ok=True)
            os.makedirs(self.ok_lbl, exist_ok=True)
            name = self._new_name()
            h, w = warped.shape[:2]
            cv2.imwrite(os.path.join(self.ok_img, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
            self._write_verified(name, [_ball_dict(b, n) for b, n in zip(balls, numbers)], radius, w, h, True)
        return name

    # ------------------------------------------------------------ 3. Export
    def export_zip(self, path):
        """Alle geprueften Bilder als ZIP fuer das Training (training/train_coral.py)."""
        n = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("classes.txt", "\n".join(CLASSES) + "\n")
            for fn in sorted(os.listdir(self.ok_img)) if os.path.isdir(self.ok_img) else []:
                if not fn.endswith(".jpg"):
                    continue
                stem = fn[:-4]
                lbl = os.path.join(self.ok_lbl, stem + ".txt")
                if not os.path.isfile(lbl):
                    continue
                z.write(os.path.join(self.ok_img, fn), f"images/{fn}")
                z.write(lbl, f"labels/{stem}.txt")
                js = os.path.join(self.ok_lbl, stem + ".json")
                if os.path.isfile(js):
                    z.write(js, f"labels/{stem}.json")
                n += 1
        return n
