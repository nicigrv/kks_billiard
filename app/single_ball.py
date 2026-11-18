"""
Einzelkugel-Serie fuers KI-Training.

Ablauf (Admin-Panel, KI-Training):
  1. Einzelkugel-Modus einschalten, Tisch leer raeumen -> "Leeren Tisch merken".
  2. Kugel waehlen (0 = Weisse, 1-15) - damit ist sie fuer alle Bilder der Serie festgelegt.
  3. Kugel hinlegen, Serie starten: in festem Abstand wird ein Bild genommen. Gespeichert wird
     nur, wenn genau EINE ruhende Kugel zu sehen ist (Hand/Arm im Bild, rollende Kugel -> nicht).
     Zwischen den Bildern die Kugel an andere Stellen legen.
  4. Die Position findet der Pi selbst (Vergleich mit dem leeren Tisch), die Nummer kommt aus
     Schritt 2 -> das Bild landet direkt bei den festgelegten Bildern (dataset/geprueft/).
     Ist die Position unsicher, kommt das Bild zu den offenen Bildern (mit Vorschlag) und wird
     wie gewohnt festgelegt.

Schneller: "Bild jetzt aufnehmen" (Knopf, jederzeit) - sofort ein Bild, ohne Warten und ohne
Erkennung am Pi. Es geht mit Nummer und Bild des leeren Tisches zur Trainingsseite (VPS), dort
zieht eine einfache Bilderkennung den Kreis (training_server/einzelkugel.py). Ohne Trainingsseite
findet der Pi die Kugel selbst wie bei der Serie.
"""

import math
import threading
import time

import cv2
import numpy as np

from ballnum import kind_of_number, color_of_number
from recorder import SINGLE_MODEL
from shots import lab_far_mask

NAMES = {0: "Weiße", 8: "schwarze 8"}


def ball_name(n):
    if n is None:
        return "-"
    return NAMES.get(n, f"Kugel {n}")


class SingleBall:
    STEADY_PX = 2.5          # Kugel gilt als ruhend, wenn sie sich zwischen zwei Blicken weniger bewegt
    MAX_COUNT = 500

    def __init__(self, pipe, recorder, notes=None, clock=time.monotonic, has_upload=None):
        self.pipe = pipe
        self.recorder = recorder
        self.notes = notes
        self.clock = clock
        self.lock = threading.Lock()
        self.enabled = False
        self.number = None
        self.has_upload = has_upload or (lambda: False)   # Trainingsseite eingerichtet?
        self.ref = None                 # leerer Tisch (Lab, uint8, geglaettet)
        self.ref_jpg = None             # leerer Tisch als JPEG (geht mit den Knopf-Bildern zum VPS)
        self.shots = 0                  # Bilder per Knopf in dieser Sitzung
        self.ref_time = None
        self.active = False
        self.target = 0
        self.saved = 0
        self.saved_open = 0
        self.skipped = 0
        self.interval = 1.0
        self.only_new = True
        self.message = ""
        self.last_pos = None            # zuletzt gespeicherte Position
        self.preview = None             # JPEG des zuletzt gespeicherten Bildes (mit Markierung)
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------- Bild holen
    def _grab(self):
        with self.pipe.lock:
            w = self.pipe.last_warped
            r = self.pipe.table.ball_radius_px if self.pipe.table is not None else None
            return (None if w is None else w.copy()), r

    @staticmethod
    def _lab(img):
        return cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2LAB), (5, 5), 0)     # uint8

    # ------------------------------------------------------------- Einstellungen
    def set_enabled(self, on):
        with self.lock:
            self.enabled = bool(on)
        if not on:
            self.stop()
        return "Einzelkugel-Modus an" if on else "Einzelkugel-Modus aus"

    def set_number(self, n):
        n = int(n)
        if not 0 <= n <= 15:
            raise ValueError("Kugelnummer 0 bis 15")
        with self.lock:
            if self.active:
                raise ValueError("Erst die laufende Serie stoppen")
            self.number = n
            self.last_pos = None
        return f"Festgelegt: {ball_name(n)} - alle Bilder der Serie bekommen diese Nummer"

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
            self.ref = self._lab(empty)
            self.ref_jpg = jpg.tobytes() if ok else None
            self.ref_time = time.strftime("%H:%M:%S")
            self.last_pos = None
        return "Leerer Tisch gemerkt - jetzt die Kugel hinlegen und die Serie starten"

    # ------------------------------------------------------------- Kugel finden
    def locate(self, img, radius):
        """Liefert (Liste der Kandidaten [(x, y)], Diagnose-Text)."""
        lab = self._lab(img)
        with self.lock:
            ref = self.ref
        if ref is None:
            # Ohne leeren Tisch: Abweichung von der mittleren Tuchfarbe
            ref = np.median(lab[::3, ::3].reshape(-1, 3), axis=0)
        mask = lab_far_mask(lab, ref, 22)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        h, w = mask.shape
        area_ball = math.pi * radius * radius
        parts = [(int(stats[i][4]), float(cents[i][0]), float(cents[i][1]), int(stats[i][2]), int(stats[i][3]))
                 for i in range(1, n) if stats[i][4] >= 0.05 * area_ball]
        balls, big = [], 0
        for area, cx, cy, bw, bh in self._merge(parts, radius):
            if area < 0.3 * area_ball:
                continue                                   # Rauschen
            if area > 3.0 * area_ball or bw > 4.5 * radius or bh > 4.5 * radius:
                big += 1                                   # Hand, Arm, Queue ...
                continue
            if min(cx, cy, w - cx, h - cy) < 0.4 * radius:
                continue                                   # Taschen-/Randschatten
            if not 0.45 < bw / max(bh, 1) < 2.2:
                continue
            balls.append((float(cx), float(cy)))
        why = ""
        if big:
            why = "etwas Großes im Bild (Hand/Arm?)"
        elif not balls:
            why = "keine Kugel gefunden"
        elif len(balls) > 1:
            why = f"{len(balls)} Kugeln gefunden - es darf nur eine auf dem Tisch liegen"
        return (balls if not big else []), why

    @staticmethod
    def _merge(parts, radius):
        """Teile einer Kugel zusammenfassen (halbe Kugel, deren Band so aussieht wie das Tuch)."""
        groups = []
        for f in parts:
            for g in groups:
                if any(math.dist((f[1], f[2]), (q[1], q[2])) < 1.6 * radius for q in g):
                    g.append(f)
                    break
            else:
                groups.append([f])
        out = []
        for g in groups:
            a = sum(f[0] for f in g)
            cx = sum(f[0] * f[1] for f in g) / a
            cy = sum(f[0] * f[2] for f in g) / a
            bw = max(f[1] + f[3] / 2 for f in g) - min(f[1] - f[3] / 2 for f in g)
            bh = max(f[2] + f[4] / 2 for f in g) - min(f[2] - f[4] / 2 for f in g)
            out.append((a, cx, cy, bw, bh))
        return out

    # ------------------------------------------------------------- Knopf: sofort ein Bild
    def shot(self):
        """Sofort ein Bild der gewaehlten Kugel - ohne Warten. Mit Trainingsseite: Kreis zieht der VPS."""
        with self.lock:
            if not self.enabled:
                raise ValueError("Einzelkugel-Modus ist aus")
            if self.number is None:
                raise ValueError("Zuerst die Kugel wählen")
        img, r = self._grab()
        if img is None or not r:
            raise ValueError("Kein Kamerabild - ist der Tisch eingerichtet?")
        if self.has_upload():
            name = self.recorder.save_single(img, self.number, r, self.ref_jpg)
            if name is None:
                raise ValueError("Speicher fast voll - es wird nichts mehr aufgenommen")
            where = "geht zur Trainingsseite, dort wird der Kreis gezogen"
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                self.preview = jpg.tobytes()
        else:
            balls, why = self.locate(img, r)
            if len(balls) != 1:
                raise ValueError(f"Nicht gespeichert: {why} (ohne Trainingsseite sucht der Pi die Kugel selbst)")
            self._save(img, r, balls[0])
            self.saved -= 1                                 # zaehlt als Knopf-Bild, nicht zur Serie
            where = "Kugel am Pi gefunden und festgelegt"
        self.shots += 1
        self.message = f"Knopf-Bild {self.shots}: {ball_name(self.number)} - {where}"
        return f"Bild {self.shots} von {ball_name(self.number)} aufgenommen"

    # ------------------------------------------------------------- Serie
    def start(self, count, interval, only_new=True):
        count = int(count)
        interval = float(interval)
        if not 1 <= count <= self.MAX_COUNT:
            raise ValueError(f"Anzahl 1 bis {self.MAX_COUNT}")
        if not 0.3 <= interval <= 30:
            raise ValueError("Abstand 0,3 bis 30 Sekunden")
        with self.lock:
            if not self.enabled:
                raise ValueError("Einzelkugel-Modus ist aus")
            if self.number is None:
                raise ValueError("Zuerst die Kugel wählen")
            if self.active:
                raise ValueError("Serie läuft bereits")
            if not self.recorder.disk_ok():
                raise ValueError("Speicher fast voll - es wird nichts mehr aufgenommen")
            self.active, self.target, self.interval, self.only_new = True, count, interval, bool(only_new)
            self.saved = self.saved_open = self.skipped = 0
            self.message = "Serie läuft ..."
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="einzelkugel", daemon=True)
            self._thread.start()
        hint = "" if self.ref is not None else " (Tipp: vorher „Leeren Tisch merken“ - dann klappt es mit jeder Kugelfarbe)"
        return f"Serie gestartet: {count} Bilder von {ball_name(self.number)}{hint}"

    def stop(self):
        with self.lock:
            was = self.active
            self.active = False
        self._stop.set()
        if was:
            self.message = f"Gestoppt: {self.saved} gespeichert"
        return f"Serie gestoppt ({self.saved} Bilder gespeichert)" if was else "Es läuft keine Serie"

    def _run(self):
        prev = None
        try:
            while not self._stop.is_set() and self.saved < self.target:
                img, r = self._grab()
                if img is None or not r:
                    self.message = "Kein Kamerabild"
                    self._stop.wait(1.0)
                    continue
                balls, why = self.locate(img, r)
                if len(balls) != 1:
                    prev = None
                    self.skipped += 1
                    self.message = f"Warte: {why}"
                    self._stop.wait(0.3)
                    continue
                pos = balls[0]
                if prev is None or math.dist(prev, pos) > self.STEADY_PX:
                    prev = pos                              # erst nochmal hinschauen: ruht sie?
                    self.message = "Warte: Kugel rollt noch"
                    self._stop.wait(0.25)
                    continue
                if self.only_new and self.last_pos is not None and math.dist(self.last_pos, pos) < 1.5 * r:
                    self.message = "Warte: Kugel an eine andere Stelle legen"
                    self._stop.wait(0.3)
                    continue
                self._save(img, r, pos)
                prev = None
                self._stop.wait(self.interval)
        except Exception as e:                              # Serie darf das Programm nie stoppen
            self.message = f"Fehler: {e}"
            print(f"[einzelkugel] {e}", flush=True)
        finally:
            done = self.saved >= self.target
            with self.lock:
                self.active = False
            if done:
                self.message = (f"Fertig: {self.saved} Bilder von {ball_name(self.number)}"
                                + (f" ({self.saved_open} davon zum Nachprüfen offen)" if self.saved_open else ""))
                if self.notes:
                    self.notes.push("Einzelkugel-Serie fertig", self.message, "success", ttl=6.0, key="einzelkugel")

    def _save(self, img, r, pos):
        n = self.number
        ball = {"x": round(pos[0], 1), "y": round(pos[1], 1), "kind": kind_of_number(n),
                "color": color_of_number(n), "number": n}
        if self.ref is not None:
            name = self.recorder.save_verified(img, [ball], r, [n], extra={"single_ball": n})
            if name is None:
                raise RuntimeError("Speicher fast voll")
        else:
            # ohne leeren Tisch ist die Position nicht sicher genug -> zum Nachpruefen
            name = self.recorder.save_pending(img, [ball], r, [n], model=SINGLE_MODEL)
            if name is None:
                raise RuntimeError("Speicher fast voll")
            self.saved_open += 1
        self.saved += 1
        self.last_pos = pos
        self.message = f"Bild {self.saved}/{self.target} gespeichert"
        prev = img.copy()
        cv2.circle(prev, (int(pos[0]), int(pos[1])), int(r * 1.6), (80, 230, 120), 2)
        ok, jpg = cv2.imencode(".jpg", prev, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.preview = jpg.tobytes()

    # ------------------------------------------------------------- Anzeige
    def status(self):
        return {"enabled": self.enabled, "number": self.number, "name": ball_name(self.number),
                "empty": self.ref_time, "active": self.active, "target": self.target,
                "saved": self.saved, "saved_open": self.saved_open, "skipped": self.skipped,
                "interval": self.interval, "only_new": self.only_new, "message": self.message,
                "preview": self.preview is not None, "shots": self.shots, "upload": bool(self.has_upload()),
                "stored": self.recorder.single if self.recorder else 0,
                "preview_id": self.shots * 100000 + self.saved}
