"""
Verarbeitungskette pro Kamerabild:
    Kamerabild -> Draufsicht -> Kugelerkennung (KI-Chip) -> Bewegung/Verdeckung -> Stoss -> Spielregeln

Die Kugeln erkennt nur der Coral (coral_detector.py). Ist er nicht bereit (kein Stick, kein
Modell), wird nichts ausgewertet - es gibt keine Ersatz-Erkennung auf dem Prozessor.
Wird identisch vom echten Programm (main.py) und vom Simulator-Test benutzt (dort mit einem
Detektor, der die Kugeln aus dem Simulator kennt).
"""

import threading
import time
from collections import Counter

from ballnum import Ball, ball_number
from coral_detector import CoralNotReady
from game import Game
from geometry import Table
from shots import MotionMeter, OcclusionMeter, ShotEvent, ShotMonitor

OCCLUSION_LIMIT = 0.006   # Anteil Tischflaeche, ab dem "etwas haengt über dem Tisch"


class Pipeline:
    def __init__(self, table: Table, detector_factory, game: Game = None, clock=time.monotonic,
                 recorder=None):
        self.lock = threading.RLock()
        self.clock = clock
        self.game = game or Game()
        self.recorder = recorder
        # Erzeugt den Kugeldetektor fuer einen Kugelradius (im Programm: Coral)
        self.detector_factory = detector_factory
        self.set_table(table)

    def set_table(self, table: Table):
        with self.lock:
            self.table = table
            self.detector = self.detector_factory(table.ball_radius_px) if table.calibrated else None
            self.motion = MotionMeter()
            self.occlusion = OcclusionMeter(table.ball_radius_px)
            self.not_ready = ""
            self._moved = False         # seit dem letzten Trainingsbild hat sich etwas bewegt
            self._still_since = None
            self.monitor = ShotMonitor(table.ball_radius_px, clock=self.clock)
            self.balls = []
            self.moving = False
            self.occluded = False
            self.last_warped = None

    def process(self, frame):
        """Ein Kamerabild verarbeiten. Gibt ein Ereignis zurueck (oder None)."""
        with self.lock:
            if self.detector is None:
                return None
            warped = self.table.warp(frame)
            self.last_warped = warped
            moving = self.motion.update(warped)
            try:
                balls = self.detector.detect(warped)
            except CoralNotReady as e:
                # Ohne KI-Chip wird nichts gezaehlt. Trainingsbilder entstehen trotzdem:
                # nach jeder Bewegung, sobald der Tisch wieder ruhig ist - die Kugeln
                # legt ihr dann auf dem iPad fest.
                self.not_ready = str(e)
                self.balls, self.moving = [], moving
                self._capture_when_still(warped, moving)
                return None
            self.not_ready = ""
            occluded = self.occlusion.update(warped, balls) > OCCLUSION_LIMIT
            ev = self.monitor.update(balls, moving, occluded)
            self.balls, self.moving, self.occluded = balls, moving, occluded

            if isinstance(ev, ShotEvent):
                diff = ev.diff(self.table.ball_radius_px)
                msg = self.game.apply_shot(ev.before.counts(), ev.after.counts(),
                                           diff.cue_before, diff.cue_after, diff=diff)
                self._collect(warped, ev.after.balls)
                return ("shot", msg, ev)
            if ev == "reference":
                self._collect(warped, self.monitor.reference.balls)
            return ev

    def _capture_when_still(self, warped, moving, still_s=1.5):
        if moving:
            self._moved, self._still_since = True, None
            return
        now = self.clock()
        if self._still_since is None:
            self._still_since = now
        if self._moved and now - self._still_since >= still_s:
            self._moved = False
            self._collect(warped, [])

    def capture_now(self):
        """Knopf im Portal: aktuelles Bild (mit Vorschlag des Coral, falls bereit) zum Pruefen."""
        with self.lock:
            if self.last_warped is None:
                return None
            return self._collect(self.last_warped, list(self.balls), force=True)

    def _collect(self, warped, balls, force=False):
        """Trainingsbild nach einem Spielzug (Tisch ruhig) mit der automatischen Zuordnung."""
        if self.recorder:
            engine = getattr(self.detector, "engine", None)
            st = engine.status() if engine is not None and balls else {}
            model = f"{st['model']} vom {st['model_time']}" if st.get("model") else None
            return self.recorder.save_pending(warped, balls, self.table.ball_radius_px,
                                              [ball_number(b.kind, b.color) for b in balls], model, force)

    def restart_tracking(self):
        """Nach Spielstart / neuem Rack: der naechste ruhige Tisch ist der neue Ausgangszustand,
        egal was vorher war (Aufbauen ist kein Stoss)."""
        with self.lock:
            self.monitor.reset()

    def status_text(self):
        """Kurzer Systemzustand fuer Anzeige und Portal."""
        if self.detector is None:
            return "Tisch nicht kalibriert - Admin-Portal oeffnen"
        if self.not_ready:
            return f"KI-Chip nicht bereit: {self.not_ready}"
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
                near = Ball(float(c["x"]), float(c["y"]), r, c["kind"], c.get("color", ""))
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
