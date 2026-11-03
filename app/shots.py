"""
Stosserkennung: macht aus vielen wackeligen Einzelbild-Erkennungen verlaessliche
Zustaende "vor dem Stoss" und "nach dem Stoss".

Grundsaetze (damit Erkennungsfehler nicht zu falschen Punkten fuehren):
  * Ausgewertet wird NUR, wenn der Tisch ruhig ist UND nichts ueber dem Tisch haengt
    (Arm, Oberkoerper, Queue auf dem Tuch).
  * Ein ruhiger Zustand = Abstimmung ueber viele Bilder: eine Kugel zaehlt nur, wenn sie
    in den meisten Bildern an derselben Stelle gefunden wurde; ihr Typ per Mehrheit.
  * Ein "Stoss" liegt nur vor, wenn sich wirklich etwas bewegt hat (Weisse verschoben
    oder Kugeln weg). Nur Hinbeugen oder Weisse-Hinlegen loest nichts aus.
"""

import itertools
import time
from collections import Counter
from dataclasses import dataclass

import cv2
import numpy as np

from detector import Ball

OBJECT_KINDS = ("solid", "stripe", "eight")


@dataclass
class TableState:
    balls: list
    t: float

    def counts(self):
        c = Counter(b.kind for b in self.balls)
        return {k: c.get(k, 0) for k in ("cue", "eight", "solid", "stripe")}

    @property
    def cue(self):
        return next((b for b in self.balls if b.kind == "cue"), None)


@dataclass
class ShotEvent:
    before: TableState
    after: TableState

    def diff(self, radius):
        return diff_states(self.before, self.after, radius)


class MotionMeter:
    """Bewegung zwischen zwei Bildern (auf verkleinerter Draufsicht)."""

    def __init__(self, threshold=22, min_fraction=0.0015):
        self.prev = None
        self.threshold = threshold
        self.min_fraction = min_fraction
        self.value = 0.0

    def update(self, warped) -> bool:
        small = cv2.cvtColor(cv2.resize(warped, (200, 100), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        small = cv2.GaussianBlur(small, (3, 3), 0)
        moving = False
        if self.prev is not None:
            diff = cv2.absdiff(small, self.prev)
            self.value = float((diff > self.threshold).mean())
            moving = self.value > self.min_fraction
        self.prev = small
        return moving


def stabilize(frames, radius, min_presence=0.6):
    """Mehrere Einzelbild-Erkennungen eines ruhigen Tisches zu einem Zustand verdichten."""
    clusters = []  # [sum_x, sum_y, n, Counter(kind)]
    max_d2 = (radius * 0.8) ** 2
    for balls in frames:
        taken = set()
        for b in balls:
            best, bd = None, max_d2
            for i, c in enumerate(clusters):
                if i in taken:
                    continue
                cx, cy = c[0] / c[2], c[1] / c[2]
                d2 = (b.x - cx) ** 2 + (b.y - cy) ** 2
                if d2 <= bd:
                    best, bd = i, d2
            if best is None:
                clusters.append([b.x, b.y, 1, Counter([b.kind]), Counter([b.color])])
                taken.add(len(clusters) - 1)
            else:
                c = clusters[best]
                c[0] += b.x
                c[1] += b.y
                c[2] += 1
                c[3][b.kind] += 1
                c[4][b.color] += 1
                taken.add(best)
    n = len(frames)
    out = []
    for sx, sy, cnt, kinds, colors in clusters:
        if cnt >= min_presence * n:
            ball = Ball(sx / cnt, sy / cnt, radius, kinds.most_common(1)[0][0], votes=kinds)
            ball.color_votes = colors
            ball.color = colors.most_common(1)[0][0]
            out.append(ball)
    assign_kinds(out)
    return out


def assign_kinds(balls):
    """Typ je Kugel per Stimmenmehrheit. Es gibt nur eine Weisse und eine 8:
    bei mehreren Anwaertern gewinnt die mit den meisten Stimmen."""
    for b in balls:
        b.kind = b.votes.most_common(1)[0][0]
    # Es liegt immer genau eine Weisse und eine 8 auf dem Tisch (ausser versenkt).
    # Fehlt sie in der Mehrheit, aber eine Kugel wurde oft genug so erkannt, ist sie es.
    # Wichtig: eine uebersehene Weisse wuerde sonst als Foul gewertet.
    for special in ("cue", "eight"):
        if balls and not any(b.kind == special for b in balls):
            def share(b):
                return b.votes.get(special, 0) / max(1, sum(b.votes.values()))
            cand = max(balls, key=share)
            if share(cand) >= 0.3 and cand.kind not in ("cue", "eight"):
                cand.kind = special
    for special, fallback in (("cue", "stripe"), ("eight", "solid")):
        holders = [b for b in balls if b.kind == special]
        if len(holders) > 1:
            holders.sort(key=lambda b: b.votes[special] / max(1, sum(b.votes.values())), reverse=True)
            for b in holders[1:]:
                rest = b.votes.copy()
                del rest[special]
                b.kind = rest.most_common(1)[0][0] if rest else fallback


def match_states(before, after, radius, max_dist=0.6):
    """Kugeln, die (fast) an derselben Stelle liegen, sind dieselbe Kugel.
    Liefert (Paare, unbewegt-nicht-gefunden-vorher, neu-nachher)."""
    pairs = []
    free_after = list(after)
    left_before = []
    for b in sorted(before, key=lambda b: (b.x, b.y)):
        best, bd = None, (max_dist * radius) ** 2
        for a in free_after:
            d2 = (a.x - b.x) ** 2 + (a.y - b.y) ** 2
            if d2 <= bd:
                best, bd = a, d2
        if best is None:
            left_before.append(b)
        else:
            free_after.remove(best)
            pairs.append((b, best))
    return pairs, left_before, free_after


def inherit_votes(previous, current, radius, cap=300):
    """Unbewegte Kugeln uebernehmen die Typ-Stimmen aus frueheren Zustaenden.
    So wird die Einordnung einer liegenden Kugel mit der Zeit immer sicherer."""
    if previous is None:
        return
    pairs, _, _ = match_states(previous.balls, current.balls, radius)
    for old, new in pairs:
        cv = getattr(old, "color_votes", Counter()) + getattr(new, "color_votes", Counter())
        if cv:
            new.color_votes = cv
            new.color = cv.most_common(1)[0][0]
        merged = old.votes + new.votes
        total = sum(merged.values())
        if total > cap:
            merged = Counter({k: v * cap / total for k, v in merged.items()})
        new.votes = merged
    assign_kinds(current.balls)


@dataclass
class ShotDiff:
    pocketed: dict          # {"solid": n, "stripe": n, "eight": n}
    cue_before: bool
    cue_after: bool
    moved: int              # wie viele Kugeln sich bewegt haben (inkl. Weisse)
    uncertain: bool
    break_estimate: dict = None   # Auswertung ueber Farben, nur fuer den Break gedacht
    break_uncertain: bool = False


BALL_COLORS = ("gelb", "blau", "rot", "violett", "orange", "gruen", "weinrot")


def break_pocketed(after: "TableState"):
    """Break-Auswertung ueber Farben: Jede Farbe gibt es genau einmal voll und einmal halb.
    Fehlt eine Farbe ganz, sind beide gefallen - egal wie die uebrigen Kugeln eingeordnet
    werden. Ist eine Farbe noch einmal da, fehlt die mit dem anderen Typ.
    Liefert (dict, unsicher) oder (None, True), wenn die Farben nicht zusammenpassen."""
    objs = [b for b in after.balls if b.kind in ("solid", "stripe")]
    by_color = {c: [] for c in BALL_COLORS}
    for b in objs:
        if b.color not in by_color:
            return None, True          # Farbe nicht lesbar -> Rueckfall auf Zaehlung
        by_color[b.color].append(b)
    result = {"solid": 0, "stripe": 0}
    uncertain = False
    for c, seen in by_color.items():
        if len(seen) == 0:
            result["solid"] += 1
            result["stripe"] += 1
        elif len(seen) == 1:
            result["stripe" if seen[0].kind == "solid" else "solid"] += 1
        elif len(seen) == 2:
            if seen[0].kind == seen[1].kind:
                uncertain = True       # beide gleicher Typ: einer ist falsch eingeordnet
        else:
            return None, True          # drei gleiche Farben: Farben werden verwechselt
    return result, uncertain


def _match_cost(b, a):
    """Wie gut passt bewegte Kugel b (vorher) zu Kugel a (nachher)?
    Farbe ist das staerkste Merkmal: jede Farbe gibt es nur als eine volle und eine halbe."""
    same_color = bool(b.color) and b.color == a.color
    same_kind = b.kind == a.kind
    if same_color and same_kind:
        return 0.0
    if same_color:
        return 1.0          # Typ gekippt (z. B. halbe sieht nach dem Rollen voll aus)
    if same_kind:
        return 2.0          # Farbe anders gelesen
    return 3.0


def _pocketed_by_identity(gone, new, n_pocketed):
    """Ordnet die bewegten Kugeln vorher/nachher einander zu (kleinste Gesamtkosten).
    Die uebrigen Vorher-Kugeln sind versenkt - mit IHREM Typ von vorher, der auf
    vielen Bildern beruht."""
    if n_pocketed == 0:
        return [], False
    if not new:
        return list(gone), False
    best_cost, best_left = None, None
    if len(gone) <= 7:
        for chosen in itertools.permutations(range(len(gone)), len(new)):
            cost = sum(_match_cost(gone[i], a) for i, a in zip(chosen, new))
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_left = [g for i, g in enumerate(gone) if i not in chosen]
    else:  # sehr viele bewegte Kugeln (selten): gierig zuordnen
        remaining = list(range(len(gone)))
        best_cost = 0.0
        for a in new:
            i = min(remaining, key=lambda i: _match_cost(gone[i], a))
            best_cost += _match_cost(gone[i], a)
            remaining.remove(i)
        best_left = [gone[i] for i in remaining]
    # unsicher, wenn die beste Zuordnung Typ- oder Farbwechsel braucht
    return best_left, best_cost >= 1.0


def diff_states(before: TableState, after: TableState, radius: float) -> ShotDiff:
    """Was ist beim Stoss passiert? Bewertet nur Kugeln, die sich bewegt haben -
    unbewegte Kugeln koennen keine Fehler in die Zaehlung bringen."""
    ob = [b for b in before.balls if b.kind in OBJECT_KINDS]
    oa = [b for b in after.balls if b.kind in OBJECT_KINDS]
    pairs, gone_or_moved, new_or_moved = match_states(ob, oa, radius)
    n_pocketed = max(0, len(gone_or_moved) - len(new_or_moved))
    pocketed_balls, uncertain = _pocketed_by_identity(gone_or_moved, new_or_moved, n_pocketed)
    dec = Counter(b.kind for b in pocketed_balls)
    pocketed = {k: int(dec.get(k, 0)) for k in OBJECT_KINDS}
    moved = len(gone_or_moved) + (1 if before.cue and not (after.cue and np.hypot(
        before.cue.x - after.cue.x, before.cue.y - after.cue.y) < 0.6 * radius) else 0)
    be, bu = break_pocketed(after)
    return ShotDiff(pocketed, before.cue is not None, after.cue is not None, moved, uncertain, be, bu)


def shot_happened(before: TableState, after: TableState, radius: float, allow_growth=False) -> bool:
    nb = sum(1 for b in before.balls if b.kind in OBJECT_KINDS)
    na = sum(1 for b in after.balls if b.kind in OBJECT_KINDS)
    if na > nb + 2 and not allow_growth:
        return False                      # Kugeln wurden aufgelegt (Rack bauen), kein Stoss
    cb, ca = before.cue, after.cue
    if cb is not None and ca is None:
        return True                       # Weisse verschwunden = gestossen und versenkt
    if cb is not None and ca is not None and np.hypot(cb.x - ca.x, cb.y - ca.y) > 2 * radius:
        return True                       # Weisse hat sich bewegt
    return na < nb                        # Kugeln weg (z. B. Stoss ohne sichtbare Weisse)


class ShotMonitor:
    """Zustaende: 'warte' (noch kein Ausgangszustand), 'ruhig', 'bewegung'."""

    def __init__(self, radius, settle_s=1.2, min_frames=6, window=15, clock=time.monotonic):
        self.radius = radius
        self.settle_s = settle_s
        self.min_frames = min_frames
        self.window = window
        self.clock = clock
        self.reset()

    def reset(self):
        self.state = "warte"
        self.reference = None
        self.frames = []
        self.still_since = None
        self.blocked = False
        self._dirty = False         # seit dem Ausgangszustand gab es Bewegung
        self._fresh = True          # erster Stoss nach Reset (Break): Kugelzahl darf steigen
        self._since_refresh = 0

    def update(self, balls, moving: bool, occluded: bool):
        """Pro Bild aufrufen. Liefert ShotEvent, 'reference' oder None."""
        now = self.clock()
        self.blocked = occluded
        if moving or occluded:
            self.frames.clear()
            self.still_since = None
            if self.reference is not None:
                self.state = "bewegung"
                self._dirty = True
            return None

        if self.still_since is None:
            self.still_since = now
        self.frames.append(balls)
        if len(self.frames) > self.window:
            self.frames.pop(0)

        if now - self.still_since < self.settle_s or len(self.frames) < self.min_frames:
            return None

        if self.reference is None:
            self.reference = TableState(stabilize(self.frames, self.radius), now)
            self.state = "ruhig"
            self._since_refresh = 0
            return "reference"
        if self._dirty:
            self._dirty = False
            self.state = "ruhig"
            self._since_refresh = 0
            before = self.reference
            stable = TableState(stabilize(self.frames, self.radius), now)
            inherit_votes(before, stable, self.radius)
            self.reference = stable
            if shot_happened(before, stable, self.radius, allow_growth=self._fresh):
                self._fresh = False
                return ShotEvent(before, stable)
            return None
        # Tisch ist laenger ruhig: Ausgangszustand regelmaessig mit neuen Bildern
        # verfeinern (nur alle 'window' Bilder, damit Stimmen nicht doppelt zaehlen)
        self._since_refresh += 1
        if self._since_refresh >= self.window:
            self._since_refresh = 0
            stable = TableState(stabilize(self.frames, self.radius), now)
            inherit_votes(self.reference, stable, self.radius)
            self.reference = stable
        return None
