"""
Verarbeitungskette pro Kamerabild:
    Kamerabild -> Draufsicht -> Kugelerkennung -> Bewegung/Verdeckung -> Stoss -> Spielregeln

Wird identisch vom echten Programm (main.py) und vom Simulator-Test benutzt.
"""

import threading
import time
from collections import Counter

from ballnum import ball_number
from detector import Ball, ClassicDetector
from game import Game
from geometry import Table
from shots import MotionMeter, ShotEvent, ShotMonitor

OCCLUSION_LIMIT = 0.006   # Anteil Tischflaeche, ab dem "etwas haengt über dem Tisch"


class Pipeline:
    def __init__(self, table: Table, game: Game = None, clock=time.monotonic, recorder=None,
                 detector_factory=None):
        self.lock = threading.RLock()
        self.clock = clock
        self.game = game or Game()
        self.recorder = recorder
        # Erzeugt den Kugeldetektor fuer einen Kugelradius (Standard: OpenCV,
        # main.py setzt den automatischen Wechsel Coral/OpenCV ein)
        self.detector_factory = detector_factory or ClassicDetector
        self.set_table(table)

    def set_table(self, table: Table):
        with self.lock:
            self.table = table
            self.detector = self.detector_factory(table.ball_radius_px) if table.calibrated else None
            self.motion = MotionMeter()
            self.monitor = ShotMonitor(table.ball_radius_px, clock=self.clock)
            self.balls = []
            self.info = None
            self.moving = False
            self.occluded = False
            self.last_warped = None

    def process(self, frame):
        """Ein Kamerabild verarbeiten. Gibt ein Ereignis zurueck (oder None)."""
        with self.lock:
            if self.detector is None:
                return None
            warped = self.table.warp(frame)
            balls, info = self.detector.detect(warped)
            moving = self.motion.update(warped)
            occluded = info.occlusion > OCCLUSION_LIMIT
            ev = self.monitor.update(balls, moving, occluded)
            self.balls, self.info, self.moving, self.occluded = balls, info, moving, occluded
            self.last_warped = warped

            if isinstance(ev, ShotEvent):
                diff = ev.diff(self.table.ball_radius_px)
                msg = self.game.apply_shot(ev.before.counts(), ev.after.counts(),
                                           diff.cue_before, diff.cue_after, diff=diff)
                self._collect(warped, ev.after.balls)
                return ("shot", msg, ev)
            if ev == "reference":
                self._collect(warped, self.monitor.reference.balls)
            return ev

    def _collect(self, warped, balls):
        """Trainingsbild nach einem Spielzug (Tisch ruhig) mit der automatischen Zuordnung."""
        if self.recorder:
            self.recorder.save_pending(warped, balls, self.table.ball_radius_px,
                                       [ball_number(b.kind, b.color) for b in balls])

    def restart_tracking(self):
        """Nach Spielstart / neuem Rack: der naechste ruhige Tisch ist der neue Ausgangszustand,
        egal was vorher war (Aufbauen ist kein Stoss)."""
        with self.lock:
            self.monitor.reset()

    def status_text(self):
        """Kurzer Systemzustand fuer Anzeige und Portal."""
        if self.detector is None:
            return "Tisch nicht kalibriert - Admin-Portal oeffnen"
        if self.occluded:
            return "Tisch wird verdeckt"
        if self.monitor.state == "warte":
            return "Warte auf ruhigen Tisch ..."
        if self.monitor.state == "bewegung" or self.moving:
            return "Kugeln rollen ..."
        return "Bereit"

    def current_balls(self):
        """Stabiler Zustand (ueber viele Bilder abgestimmt), sonst das letzte Einzelbild."""
        ref = self.monitor.reference
        return list(ref.balls) if ref else list(self.balls)

    def correct_balls(self, corrections):
        """Korrekturen aus dem Portal in den stabilen Zustand uebernehmen.
        corrections: [{"x", "y", "kind", "color"}], kind "none" = keine Kugel.
        Die Korrektur bekommt sehr viele Stimmen, damit sie die naechsten
        Abstimmungen ueberlebt (siehe shots.inherit_votes)."""
        ref = self.monitor.reference
        if ref is None:
            return 0
        r = self.table.ball_radius_px
        changed = 0
        for c in corrections:
            near = min(ref.balls, key=lambda b: (b.x - c["x"]) ** 2 + (b.y - c["y"]) ** 2, default=None)
            hit = near is not None and (near.x - c["x"]) ** 2 + (near.y - c["y"]) ** 2 <= (1.2 * r) ** 2
            if c["kind"] == "none":
                if hit:
                    ref.balls.remove(near)
                    changed += 1
                continue
            if not hit:
                near = Ball(float(c["x"]), float(c["y"]), r, c["kind"])
                ref.balls.append(near)
            near.kind = c["kind"]
            near.votes = Counter({c["kind"]: 300})
            if c.get("color"):
                near.color = c["color"]
                near.color_votes = Counter({c["color"]: 300})
            changed += 1
        self.monitor.ref_id += 1
        return changed

    def stable_counts(self):
        ref = self.monitor.reference
        return ref.counts() if ref else None
