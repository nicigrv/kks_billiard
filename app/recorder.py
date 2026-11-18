"""
Trainingsbilder aufnehmen - festgelegt und gelernt wird NICHT auf dem Pi, sondern auf der
Trainingsseite im Netz (VPS, training_server/). So bleibt der Pi frei fuer Erkennung und Beamer.

Ablauf:
  1. Per Knopf "Bild speichern" (Spiel- oder Admin-Panel) - oder, wenn eingeschaltet,
     automatisch nach jedem Stoss - wird die entzerrte Draufsicht gespeichert, zusammen mit dem,
     was der KI-Chip erkannt hat (Position und Kugelnummer).
     -> dataset/pruefen/<name>.jpg + <name>.json
     Einzelkugel per Knopf (Nummer am Pi gewaehlt, Kreis zieht der VPS):
     -> dataset/einzel/<name>.jpg + <name>.json (+ einmal leer-<hash>.jpg = leerer Tisch)
  2. uploader.py schickt jedes Bild zum VPS und loescht es danach auf dem Pi.
     Ohne eingerichteten VPS bleiben die Bilder liegen (solange mehr als 1 GB frei ist).

Aeltere Versionen haben am Pi festgelegte Bilder in dataset/geprueft/ abgelegt; die schickt der
Uploader ebenfalls noch hoch (als "am Tisch festgelegt").
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time

import cv2

from ballnum import ball_number

NAME_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{3}")


def _ball_dict(b, number=None):
    n = number if number is not None else getattr(b, "number", None)
    if n is None:
        n = ball_number(b.kind, getattr(b, "color", ""))
    return {"x": float(b.x), "y": float(b.y), "kind": b.kind, "color": getattr(b, "color", "") or "",
            "number": n}


class Recorder:
    def __init__(self, base_dir, min_free_bytes=1_000_000_000):
        self.base_dir = base_dir
        self.pending_dir = os.path.join(base_dir, "pruefen")          # wartet auf den Upload
        self.single_dir = os.path.join(base_dir, "einzel")            # Einzelkugel per Knopf
        # nur noch aus aelteren Versionen (am Pi festgelegt / verworfen) - wird hochgeladen bzw. geloescht
        self.ok_img = os.path.join(base_dir, "geprueft", "images")
        self.ok_lbl = os.path.join(base_dir, "geprueft", "labels")
        self.discard_dir = os.path.join(base_dir, "verworfen")
        self.min_free_bytes = min_free_bytes
        self.lock = threading.Lock()

    # ------------------------------------------------------------- Zaehler
    @staticmethod
    def _count(folder, ext):
        try:
            return sum(1 for n in os.listdir(folder) if n.endswith(ext) and NAME_RE.fullmatch(n[:-len(ext)]))
        except OSError:
            return 0

    @property
    def pending(self):
        """Bilder, die noch auf dem Pi liegen (warten auf den Upload)."""
        return self._count(self.pending_dir, ".json")

    @property
    def single(self):
        return self._count(self.single_dir, ".json")

    @property
    def verified(self):
        return self._count(self.ok_img, ".jpg")

    @property
    def waiting(self):
        """Alles, was noch zum VPS muss."""
        return self.pending + self.single + self.verified

    def _new_name(self):
        """Zeitstempel als Name; ist er schon vergeben (gleiche Millisekunde), eins weiter."""
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        ms = int(now * 1000) % 1000
        while True:
            name = f"{stamp}-{ms:03d}"
            if not any(os.path.exists(os.path.join(d, name + ".jpg")) for d in
                       (self.pending_dir, self.ok_img, self.single_dir)):
                return name
            ms += 1
            if ms > 999:
                time.sleep(0.001)
                return self._new_name()

    def _write(self, folder, name, warped, data):
        cv2.imwrite(os.path.join(folder, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
        tmp = os.path.join(folder, name + ".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(folder, name + ".json"))   # .json zuletzt: erst dann ist es fertig

    # ------------------------------------------------------------ aufnehmen
    def save_pending(self, warped, balls, radius, numbers=None, model=None):
        """Knopf "Bild aufnehmen": Bild + Vorschlag des KI-Chips (kann leer sein) fuer den VPS."""
        if not self.disk_ok():
            return None
        numbers = numbers or [None] * len(balls)
        with self.lock:
            os.makedirs(self.pending_dir, exist_ok=True)
            name = self._new_name()
            h, w = warped.shape[:2]
            self._write(self.pending_dir, name, warped,
                        {"width": w, "height": h, "radius": float(radius), "model": model,
                         "balls": [_ball_dict(b, n) for b, n in zip(balls, numbers)]})
        return name

    def save_single(self, warped, number, radius, empty_jpg=None):
        """Einzelkugel-Bild per Knopf: nur Bild + Kugelnummer, die Position findet die Bilderkennung
        auf dem Trainingsserver. Das Bild des leeren Tisches (falls gemerkt) liegt einmal daneben
        (leer-<hash>.jpg) und geht mit jedem Bild mit hoch."""
        if not self.disk_ok():
            return None
        with self.lock:
            os.makedirs(self.single_dir, exist_ok=True)
            empty = None
            if empty_jpg:
                empty = "leer-" + hashlib.sha256(empty_jpg).hexdigest()[:16] + ".jpg"
                p = os.path.join(self.single_dir, empty)
                if not os.path.exists(p):
                    with open(p, "wb") as f:
                        f.write(empty_jpg)
            name = self._new_name()
            h, w = warped.shape[:2]
            self._write(self.single_dir, name, warped,
                        {"width": w, "height": h, "radius": float(radius), "model": None, "balls": [],
                         "single_ball": int(number), "empty": empty})
        return name

    # ------------------------------------------------------------ nach dem Upload
    def remove(self, jpg, js):
        """Bild ist auf dem VPS angekommen: auf dem Pi loeschen (Speicher sparen)."""
        with self.lock:
            for p in (jpg, js):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
            if os.path.dirname(js) == self.ok_lbl:                     # alte YOLO-Datei daneben
                try:
                    os.remove(js[:-5] + ".txt")
                except FileNotFoundError:
                    pass

    def delete_all(self):
        """ALLE Trainingsbilder auf dem Pi loeschen (auch die, die noch nicht hochgeladen sind).
        Die Modelle bleiben. Liefert die Anzahl geloeschter Bilder."""
        with self.lock:
            n = self.pending + self.verified + self._count(self.discard_dir, ".jpg") + self.single
            for folder in (self.pending_dir, os.path.dirname(self.ok_img), self.discard_dir, self.single_dir):
                shutil.rmtree(folder, ignore_errors=True)
            try:
                os.remove(os.path.join(self.base_dir, "modell-statistik.json"))   # aus aelteren Versionen
            except OSError:
                pass
        return n

    def disk_ok(self):
        """Genug Platz auf der Speicherkarte? Darunter wird nichts mehr gesammelt."""
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            st = os.statvfs(self.base_dir)
            return st.f_bavail * st.f_frsize >= self.min_free_bytes
        except OSError:
            return False
