"""
Verarbeitungskette pro Kamerabild:
    Kamerabild -> Draufsicht -> Kugelerkennung -> Bewegung/Verdeckung -> Stoss -> Spielregeln

Wird identisch vom echten Programm (main.py) und vom Simulator-Test benutzt.
"""

import threading
import time

from detector import ClassicDetector
from game import Game
from geometry import Table
from shots import MotionMeter, ShotEvent, ShotMonitor

OCCLUSION_LIMIT = 0.006   # Anteil Tischflaeche, ab dem "etwas haengt über dem Tisch"


class Pipeline:
    def __init__(self, table: Table, game: Game = None, clock=time.monotonic, recorder=None):
        self.lock = threading.RLock()
        self.clock = clock
        self.game = game or Game()
        self.recorder = recorder
        self.set_table(table)

    def set_table(self, table: Table):
        with self.lock:
            self.table = table
            self.detector = ClassicDetector(table.ball_radius_px) if table.calibrated else None
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
                if self.recorder:
                    self.recorder.save(warped, ev.after.balls)
                return ("shot", msg, ev)
            if ev == "reference" and self.recorder:
                self.recorder.save(warped, self.monitor.reference.balls)
            return ev

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

    def stable_counts(self):
        ref = self.monitor.reference
        return ref.counts() if ref else None
