"""
Einzelkugel-Bilder fuers KI-Training (Admin-Panel, Reiter "Bilder & Modell").

  1. Einzelkugel-Modus einschalten, Tisch leer raeumen -> "Leeren Tisch merken".
  2. Kugel waehlen (0 = Weisse, 1-15) - die Nummer gilt fuer alle folgenden Bilder.
  3. Kugel hinlegen, "Bild jetzt aufnehmen", Kugel woanders hinlegen, wieder aufnehmen ...

Der Pi rechnet dabei nichts: Bild + Nummer + Bild des leeren Tisches gehen zur Trainingsseite (VPS),
dort zieht eine einfache Bilderkennung den Kreis (training_server/einzelkugel.py). Deshalb geht das
nur, wenn die Trainingsseite eingerichtet ist (TRAINING_UPLOAD_URL/KEY).
"""

import threading
import time

import cv2
import numpy as np

NAMES = {0: "Weiße", 8: "schwarze 8"}


def ball_name(n):
    if n is None:
        return "-"
    return NAMES.get(n, f"Kugel {n}")


class SingleBall:
    def __init__(self, pipe, recorder, notes=None, has_upload=None):
        self.pipe = pipe
        self.recorder = recorder
        self.notes = notes
        self.lock = threading.Lock()
        self.enabled = False
        self.number = None
        self.has_upload = has_upload or (lambda: False)   # Trainingsseite eingerichtet?
        self.ref_jpg = None             # leerer Tisch als JPEG (geht mit jedem Bild zum VPS)
        self.ref_time = None
        self.shots = 0                  # Bilder per Knopf in dieser Sitzung
        self.message = ""
        self.preview = None             # JPEG des zuletzt aufgenommenen Bildes

    def _grab(self):
        with self.pipe.lock:
            w = self.pipe.last_warped
            r = self.pipe.table.ball_radius_px if self.pipe.table is not None else None
            return (None if w is None else w.copy()), r

    # ------------------------------------------------------------- Einstellungen
    def set_enabled(self, on):
        with self.lock:
            self.enabled = bool(on)
        return "Einzelkugel-Modus an" if on else "Einzelkugel-Modus aus"

    def set_number(self, n):
        n = int(n)
        if not 0 <= n <= 15:
            raise ValueError("Kugelnummer 0 bis 15")
        with self.lock:
            self.number = n
        return f"Festgelegt: {ball_name(n)} - alle folgenden Bilder bekommen diese Nummer"

    def remember_empty(self):
        """Mehrere Bilder des leeren Tisches mitteln (Rauschen, Flackern)."""
        frames = []
        for _ in range(5):
            img, _ = self._grab()
            if img is not None:
                frames.append(img)
            time.sleep(0.12)
        if not frames:
            raise ValueError("Kein Kamerabild - ist der Tisch eingerichtet?")
        empty = np.median(np.stack(frames), axis=0).astype(np.uint8)
        ok, jpg = cv2.imencode(".jpg", empty, [cv2.IMWRITE_JPEG_QUALITY, 92])
        with self.lock:
            self.ref_jpg = jpg.tobytes() if ok else None
            self.ref_time = time.strftime("%H:%M:%S")
        return "Leerer Tisch gemerkt - jetzt die Kugel hinlegen und Bilder aufnehmen"

    # ------------------------------------------------------------- Knopf: ein Bild
    def shot(self):
        """Sofort ein Bild der gewaehlten Kugel. Den Kreis zieht die Trainingsseite (VPS)."""
        with self.lock:
            if not self.enabled:
                raise ValueError("Einzelkugel-Modus ist aus")
            if self.number is None:
                raise ValueError("Zuerst die Kugel wählen")
        if not self.has_upload():
            raise ValueError("Nur mit Trainingsseite: TRAINING_UPLOAD_URL und TRAINING_UPLOAD_KEY eintragen "
                             "(dort wird der Kreis um die Kugel gezogen)")
        img, r = self._grab()
        if img is None or not r:
            raise ValueError("Kein Kamerabild - ist der Tisch eingerichtet?")
        name = self.recorder.save_single(img, self.number, r, self.ref_jpg)
        if name is None:
            raise ValueError("Speicher fast voll - es wird nichts mehr aufgenommen")
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.preview = jpg.tobytes()
        self.shots += 1
        self.message = f"Bild {self.shots}: {ball_name(self.number)} - geht zur Trainingsseite"
        return f"Bild {self.shots} von {ball_name(self.number)} aufgenommen"

    # ------------------------------------------------------------- Anzeige
    def status(self):
        return {"enabled": self.enabled, "number": self.number, "name": ball_name(self.number),
                "empty": self.ref_time, "message": self.message, "preview": self.preview is not None,
                "shots": self.shots, "upload": bool(self.has_upload()),
                "stored": self.recorder.single if self.recorder else 0, "preview_id": self.shots}
