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
"""

import math
import threading
import time

import cv2
import numpy as np

from ballnum import kind_of_number, color_of_number

NAMES = {0: "Weiße", 8: "schwarze 8"}


def ball_name(n):
    if n is None:
        return "-"
    return NAMES.get(n, f"Kugel {n}")


class SingleBall:
    STEADY_PX = 2.5          # Kugel gilt als ruhend, wenn sie sich zwischen zwei Blicken weniger bewegt
    MAX_COUNT = 500

    def __init__(self, pipe, recorder, notes=None, clock=time.monotonic):
        self.pipe = pipe
        self.recorder = recorder
        self.notes = notes
        self.clock = clock
        self.lock = threading.Lock()
        self.enabled = False
        self.number = None
        self.ref = None                 # leerer Tisch (Lab, float32, geglaettet)
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
        return cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32), (5, 5), 0)

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
                frames.append(self._lab(img))
            time.sleep(0.12)
        if not frames:
            raise ValueError("Kein Kamerabild - ist der Tisch eingerichtet?")
        with self.lock:
            self.ref = np.median(np.stack(frames), axis=0).astype(np.float32)
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
            ref = np.median(lab.reshape(-1, 3), axis=0)[None, None, :]
        diff = np.sqrt(((lab - ref) ** 2).sum(axis=2))
        mask = (diff > 22).astype(np.uint8)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        h, w = mask.shape
        area_ball = math.pi * radius * radius
        balls, big = [], 0
        for i in range(1, n):
            x0, y0, bw, bh, area = stats[i]
            if area < 0.3 * area_ball:
                continue                                   # Rauschen
            cx, cy = cents[i]
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
            name = self.recorder.save_pending(img, [ball], r, [n], model="Einzelkugel-Serie")
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
                "preview": self.preview is not None}
