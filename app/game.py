"""
8-Ball-Spiellogik (vereinfachte WPA-Regeln) fuer zwei Mannschaften.

Was automatisch erkannt wird:
  * versenkte volle/halbe Kugeln, versenkte 8, versenkte Weisse (Foul)
  * Gruppenzuteilung (erster Stoss nach dem Break, bei dem nur eine Gruppe faellt)
  * Zugwechsel (nichts Eigenes versenkt oder Foul)
  * Sieg/Niederlage bei der 8

Stand je Kugel: Das Spiel weiss, welche Nummern noch auf dem Tisch liegen (on_table). "Noch x Volle /
Halbe" ergibt sich daraus. Eine Kugel, die die Kamera im Spiel auf MISS_FRAMES (4) Bildern hintereinander
nicht mehr sieht, zaehlt sofort als versenkt (ball_gone, siehe pipeline.PocketTracker). Taucht sie im
naechsten ruhigen Zustand wieder auf, kommt sie zurueck. Im Spiel-Panel kann jede Kugel von Hand ins Loch
gelegt oder zurueckgeholt werden (pocket_ball / restore_ball).

Manueller Modus (manual): Die Kamera aendert den Spielstand nicht mehr - wer spielt, tippt die versenkten
Kugeln im Spiel-Panel an und wechselt den Zug selbst. Fuer den Fall, dass die KI versagt.

Was NICHT automatisch erkannt wird (Admin-Knopf "Foul"):
  * falsche Kugel zuerst getroffen, keine Bande beruehrt, Kugel vom Tisch gesprungen

Schutz gegen Erkennungsfehler:
  * Gezaehlt wird die Differenz zwischen beobachtetem Zustand VOR und NACH dem Stoss.
    Eine Kugel, die nie erkannt wurde, kann also auch nicht "versenkt" werden.
  * Die Gesamtzahl verschwundener Kugeln begrenzt, wie viele als versenkt gelten.
    Kippt nur der erkannte Typ einer Kugel (halbe sieht ploetzlich voll aus),
    wird nichts gezaehlt.
  * Der Spielstand kann nie mehr Kugeln verlieren, als laut Spiel noch liegen.
  * Jede Änderung ist per "Rückgängig" umkehrbar.
"""

import copy
import time

GROUP_NAMES = {"solid": "Volle", "stripe": "Halbe", None: "offen"}
OTHER = {"solid": "stripe", "stripe": "solid"}
NUMBERS = {"solid": tuple(range(1, 8)), "stripe": tuple(range(9, 16))}
ALL_BALLS = frozenset(range(1, 16))          # 1-7 volle, 8, 9-15 halbe (die Weisse zaehlt nicht)
NOT_SAVED = ("_history", "manual")           # gehoert nicht zum Spielstand (Rueckgaengig aendert es nicht)


def kind_of(number):
    return "cue" if number == 0 else "eight" if number == 8 else "solid" if number < 8 else "stripe"


def ball_label(number):
    return "die 8" if number == 8 else f"Kugel {number}"


def pocketed_from_states(before_counts: dict, after_counts: dict):
    """Welche Objektkugeln sind verschwunden? Robust gegen Typ-Verwechslungen."""
    kinds = ("solid", "stripe", "eight")
    dec = {k: max(0, before_counts.get(k, 0) - after_counts.get(k, 0)) for k in kinds}
    total_before = sum(before_counts.get(k, 0) for k in kinds)
    total_after = sum(after_counts.get(k, 0) for k in kinds)
    total_drop = max(0, total_before - total_after)
    uncertain = False
    if sum(dec.values()) > total_drop:
        # Typ-Verwechslung im Spiel: nur so viele Kugeln zaehlen, wie wirklich fehlen.
        uncertain = True
        budget = total_drop
        result = {k: 0 for k in kinds}
        # 8 zuerst (eindeutigster Typ), dann der Typ mit der groessten Abnahme
        for k in sorted(kinds, key=lambda k: (k != "eight", -dec[k])):
            take = min(dec[k], budget)
            result[k] = take
            budget -= take
        dec = result
    return dec, uncertain


class Game:
    def __init__(self):
        self.teams = ["Team A", "Team B"]
        self.frames_won = [0, 0]
        self.phase = "idle"            # idle | playing | finished
        self.reset_rack(0)
        self.log = []
        self.message = "Kein Spiel aktiv"
        self.manual = False            # manueller Modus: Kamera aendert den Spielstand nicht
        self._history = []

    # ------------------------------------------------------------- Verwaltung
    def reset_rack(self, breaker):
        self.turn = breaker
        self.breaker = breaker
        self.groups = [None, None]
        self.on_table = set(ALL_BALLS)   # welche Nummern noch liegen
        self.live = None                 # im laufenden Stoss schon als versenkt erkannt (ball_gone)
        self.breaking = True
        self.winner = None
        self.ball_in_hand = False
        self.shots = 0

    # ------------------------------------------------------- Stand je Kugel
    @property
    def remaining(self):
        """Noch liegende volle/halbe Kugeln - ergibt sich aus on_table."""
        return {g: sum(1 for n in nums if n in self.on_table) for g, nums in NUMBERS.items()}

    @remaining.setter
    def remaining(self, counts):
        for g, want in counts.items():
            diff = int(want) - self.remaining[g]
            if diff < 0:
                self._take(g, -diff)
            elif diff > 0:
                self._give(g, diff)

    @property
    def eight_on_table(self):
        return 8 in self.on_table

    @eight_on_table.setter
    def eight_on_table(self, on):
        if on:
            self.on_table.add(8)
        else:
            self.on_table.discard(8)

    def _take(self, group, n, hint=(), seen=frozenset()):
        """n Kugeln der Gruppe als versenkt markieren -> [Nummern]. Welche? Zuerst die genannten
        (Kamera kennt die Farbe), dann die, die die Kamera gerade nicht sieht, dann die hoechsten."""
        nums = [k for k in NUMBERS[group] if k in self.on_table]
        order = sorted(nums, key=lambda k: (k not in hint, k in seen, -k))
        out = order[:max(0, n)]
        self.on_table.difference_update(out)
        return out

    def _give(self, group, n, seen=frozenset()):
        """n Kugeln der Gruppe zurueck auf den Tisch - zuerst die, die die Kamera sieht."""
        nums = [k for k in NUMBERS[group] if k not in self.on_table]
        order = sorted(nums, key=lambda k: (k not in seen, k))
        out = order[:max(0, n)]
        self.on_table.update(out)
        return out

    def _save(self, tag="admin"):
        state = {k: copy.deepcopy(v) for k, v in self.__dict__.items() if k not in NOT_SAVED}
        self._history.append((tag, state))
        if len(self._history) > 50:
            self._history.pop(0)

    def _restore(self, index):
        """Zustand vor Eintrag 'index' wiederherstellen, alles danach verwerfen."""
        _, state = self._history[index]
        hist = self._history[:index]
        self.__dict__.update(copy.deepcopy(state))
        self._history = hist

    def undo(self):
        if not self._history:
            return "Nichts zum Rückgängigmachen"
        self._restore(len(self._history) - 1)
        self._event("Letzte Änderung rückgängig gemacht")
        return self.message

    @property
    def last_shot_index(self):
        return next((i for i in range(len(self._history) - 1, -1, -1) if self._history[i][0] == "shot"), None)

    def undo_shot(self):
        """Faelschlich erkannten Stoss verwerfen: Spielstand wie vor diesem Stoss
        (spaetere Korrekturen werden mit verworfen)."""
        i = self.last_shot_index
        if i is None:
            return "Kein erkannter Stoß zum Verwerfen"
        later = len(self._history) - 1 - i
        self._restore(i)
        self._event("Erkannter Stoß verworfen" + (f" (inkl. {later} späterer Änderung(en))" if later else "")
                    + f". {self.shooter} ist dran.")
        return self.message

    def _event(self, text):
        self.message = text
        self.log.append((time.strftime("%H:%M:%S"), text))
        self.log = self.log[-100:]

    @property
    def shooter(self):
        return self.teams[self.turn]

    # ----------------------------------------------------------- Admin-Aktionen
    def start_match(self, team_a, team_b, breaker=0):
        self._save()
        self.teams = [team_a.strip() or "Team A", team_b.strip() or "Team B"]
        self.frames_won = [0, 0]
        self.reset_rack(breaker)
        self.phase = "playing"
        self._event(f"Spiel gestartet: {self.teams[0]} gegen {self.teams[1]}. {self.shooter} bricht an.")

    def new_rack(self, breaker=None):
        self._save()
        if breaker is None:
            breaker = 1 - self.breaker      # Abwechselnd anstossen
        self.reset_rack(breaker)
        self.phase = "playing"
        self._event(f"Neues Rack. {self.shooter} bricht an.")

    def abort(self):
        self._save()
        self.phase = "idle"
        self._event("Spiel abgebrochen")

    def switch_turn(self, reason="Zugwechsel (Admin)"):
        self._save()
        self.turn = 1 - self.turn
        self.ball_in_hand = False
        self._event(f"{reason}. {self.shooter} ist dran.")

    def foul(self):
        self._save()
        self.turn = 1 - self.turn
        self.ball_in_hand = True
        self.breaking = False
        self._event(f"Foul! {self.shooter} hat Ball in der Hand.")

    def set_group(self, team, group):
        self._save()
        self.groups[team] = group
        self.groups[1 - team] = OTHER[group]
        self._event(f"{self.teams[team]} spielt {GROUP_NAMES[group]}.")

    def adjust(self, group, delta):
        self._save()
        if delta < 0:
            self._take(group, -delta)
        else:
            self._give(group, delta)
        self._event(f"Korrektur: noch {self.remaining[group]} {GROUP_NAMES[group]}.")

    def set_manual(self, on):
        self.manual = bool(on)
        self._event("Manueller Modus: versenkte Kugeln im Spiel-Panel antippen, Zug selbst wechseln."
                    if self.manual else "Automatik an: die Kamera zählt wieder mit.")

    def pocket_ball(self, number):
        """Spiel-Panel: Kugel von Hand ins Loch. Im manuellen Modus wie ein Treffer behandelt:
        offener Tisch -> der Schuetze bekommt diese Gruppe, die 8 entscheidet das Rack."""
        number = int(number)
        if number not in ALL_BALLS:
            raise ValueError("Kugel 1 bis 15")
        if self.phase != "playing":
            return "Kein Spiel aktiv"
        if number not in self.on_table:
            return f"{ball_label(number).capitalize()} ist schon versenkt"
        self._save()
        self.on_table.discard(number)
        if number == 8:
            own = self.groups[self.turn]
            if own is not None and self.remaining[own] == 0:
                return self._finish(self.turn, f"8 versenkt - {self.shooter} gewinnt!") or self.message
            if self.breaking:
                return self._finish(self.turn, f"8 beim Anstoß versenkt - {self.shooter} gewinnt!") or self.message
            return self._finish(1 - self.turn, f"8 zu früh versenkt - {self.teams[1 - self.turn]} gewinnt.") \
                or self.message
        g = kind_of(number)
        if self.manual and self.groups[self.turn] is None and not self.breaking:
            self.groups[self.turn] = g
            self.groups[1 - self.turn] = OTHER[g]
            self._event(f"{ball_label(number).capitalize()} im Loch - {self.shooter} spielt {GROUP_NAMES[g]}.")
            return self.message
        self._event(f"{ball_label(number).capitalize()} im Loch. Noch {self.remaining[g]} {GROUP_NAMES[g]}.")
        return self.message

    def restore_ball(self, number):
        """Spiel-Panel: Kugel war doch nicht im Loch - zurueck auf den Tisch."""
        number = int(number)
        if number not in ALL_BALLS:
            raise ValueError("Kugel 1 bis 15")
        if self.phase == "finished":
            return "Rack ist beendet - zum Weiterspielen „Rückgängig“"
        if self.phase != "playing":
            return "Kein Spiel aktiv"
        if number in self.on_table:
            return f"{ball_label(number).capitalize()} liegt schon auf dem Tisch"
        self._save()
        self.on_table.add(number)
        g = kind_of(number)
        self._event(f"{ball_label(number).capitalize()} zurück auf dem Tisch."
                    + (f" Noch {self.remaining[g]} {GROUP_NAMES[g]}." if g in NUMBERS else ""))
        return self.message

    # ------------------------------------------------- Kamera: Kugel fehlt
    def ball_gone(self, kind, number=None, seen=frozenset()):
        """Kamera: eine Kugel fehlt auf MISS_FRAMES Bildern hintereinander -> sofort versenkt.
        Gehoert zum laufenden Stoss: Rueckgaengig ("Stoß verwerfen") nimmt sie mit zurueck,
        apply_shot zaehlt sie nicht doppelt. Nicht beim Anstoss (dort verdecken sich die Kugeln)."""
        if self.phase != "playing" or self.manual or self.breaking or kind not in ("solid", "stripe", "eight"):
            return None
        if kind == "eight":
            if not self.eight_on_table:
                return None
            taken = [8]
        else:
            taken = self._take(kind, 1, hint=(number,) if number else (), seen=seen) if self.remaining[kind] else []
        if not taken:
            return None
        if self.live is None:
            self.on_table.update(taken)          # Zustand VOR dem Stoss sichern ...
            self._save("shot")
            self.on_table.difference_update(taken)
            self.live = []
        if kind == "eight":
            self.on_table.discard(8)
        self.live.append(taken[0])
        self._event(f"{ball_label(taken[0]).capitalize()} im Loch (4 Bilder nicht gesehen).")
        return self.message

    def settled(self, seen):
        """Kamera: Tisch ist ruhig, ohne dass ein Stoss erkannt wurde. Kugeln, die gerade als
        versenkt galten, aber wieder zu sehen sind (waren nur verdeckt), kommen zurueck."""
        if not self.live:
            return None
        back = [n for n in self.live if n in seen]
        if not back:
            return None
        self.on_table.update(back)
        self.live = [n for n in self.live if n not in back] or None
        self._event("Doch nicht im Loch: " + ", ".join(ball_label(n) for n in back) + " wieder erkannt.")
        return self.message

    def declare_winner(self, team):
        self._save()
        self._finish(team, f"{self.teams[team]} gewinnt das Rack (Admin).")

    def _finish(self, team, text):
        self.winner = team
        self.frames_won[team] += 1
        self.phase = "finished"
        self._event(text + f" Stand {self.frames_won[0]}:{self.frames_won[1]}.")

    # ------------------------------------------------------------ Automatik
    def apply_shot(self, before_counts: dict, after_counts: dict, cue_before: bool, cue_after: bool,
                   diff=None, seen=None):
        """Wird nach jedem erkannten Stoss aufgerufen.
        diff: Ergebnis von shots.diff_states (nur bewegte Kugeln). Ohne diff wird ueber
        die Gesamtzahlen gerechnet (fuer Tests und als Rueckfallebene).
        seen: Nummern, die die Kamera nach dem Stoss sieht (falls bekannt)."""
        if self.phase != "playing" or self.manual:
            self.live = None
            return None
        seen = frozenset(seen or ())
        live = self.live or []
        self.live = None
        if live:
            # waehrend des Stosses schon als versenkt gezaehlt - wieder zu sehen? Dann war sie nur verdeckt.
            back = [n for n in live if n in seen]
            self.on_table.update(back)
            live = [n for n in live if n not in back]
        else:
            self._save("shot")
        if self.breaking:
            # Im Rack verdecken sich die Kugeln gegenseitig, die Erkennung ist dort am
            # schwaechsten. Die Aufstellung ist aber bekannt. Nur die 8 bleibt beobachtet,
            # damit eine uebersehene 8 nicht als "beim Anstoss versenkt" gewertet wird.
            before_counts = dict(before_counts, solid=self.remaining["solid"],
                                 stripe=self.remaining["stripe"])
            dec, uncertain = pocketed_from_states(before_counts, after_counts)
            if diff is not None and diff.break_estimate is not None:
                # Besser: ueber Farben (robust gegen voll/halb-Verwechslungen)
                dec = dict(diff.break_estimate, eight=dec["eight"])
                uncertain = diff.break_uncertain
        elif diff is not None:
            dec, uncertain = dict(diff.pocketed), diff.uncertain
        else:
            dec, uncertain = pocketed_from_states(before_counts, after_counts)
        # Schon waehrend des Stosses erkannte Kugeln zaehlen mit, aber nicht doppelt
        schon = {g: sum(1 for n in live if kind_of(n) == g) for g in ("solid", "stripe", "eight")}
        # Nie mehr abziehen, als laut Spiel noch liegt
        extra = {g: min(max(0, dec[g] - schon[g]), self.remaining[g]) for g in ("solid", "stripe")}
        pocket = {g: schon[g] + extra[g] for g in ("solid", "stripe")}
        eight = schon["eight"] > 0 or (dec["eight"] > 0 and self.eight_on_table)
        hints = tuple(getattr(diff, "pocketed_numbers", ()) or ()) if diff is not None else ()
        scratch = cue_before and not cue_after
        shooter = self.turn
        own = self.groups[shooter]
        own_left_before = self.remaining[own] + schon[own] if own else None     # Stand VOR dem Stoss
        was_break = self.breaking

        for g in extra:
            self._take(g, extra[g], hint=hints, seen=seen)
        self._resync(after_counts, seen)
        self.shots += 1
        self.breaking = False
        self.ball_in_hand = False

        parts = []
        if pocket["solid"]:
            parts.append(f"{pocket['solid']}x Volle")
        if pocket["stripe"]:
            parts.append(f"{pocket['stripe']}x Halbe")
        what = ", ".join(parts) if parts else "nichts"
        hint = " (unsicher - bitte prüfen)" if uncertain else ""

        if eight:
            self.on_table.discard(8)
            if scratch:
                return self._finish(1 - shooter, f"8 und Weiße versenkt - {self.teams[1 - shooter]} gewinnt.") or self.message
            if was_break:
                return self._finish(shooter, f"8 beim Anstoß versenkt - {self.shooter} gewinnt!") or self.message
            if own is not None and own_left_before == 0:
                return self._finish(shooter, f"8 versenkt - {self.shooter} gewinnt!") or self.message
            return self._finish(1 - shooter, f"8 zu früh versenkt - {self.teams[1 - shooter]} gewinnt.") or self.message

        if scratch:
            self.turn = 1 - shooter
            self.ball_in_hand = True
            self._event(f"Weiße versenkt (Foul), versenkt: {what}{hint}. {self.shooter} hat Ball in der Hand.")
            return self.message

        if own is None:
            # Offener Tisch: Gruppe wird beim ersten Stoss nach dem Break vergeben,
            # bei dem genau eine Gruppe faellt.
            if not was_break and (pocket["solid"] > 0) != (pocket["stripe"] > 0):
                g = "solid" if pocket["solid"] else "stripe"
                self.groups[shooter] = g
                self.groups[1 - shooter] = OTHER[g]
                self._event(f"{self.shooter} versenkt {what} und spielt {GROUP_NAMES[g]}{hint}. Weiter.")
                return self.message
            if pocket["solid"] + pocket["stripe"] > 0:
                self._event(f"{self.shooter} versenkt {what}{hint}, Tisch bleibt offen. Weiter.")
                return self.message
        elif pocket[own] > 0:
            self._event(f"{self.shooter} versenkt {what}{hint}. Weiter.")
            return self.message

        self.turn = 1 - shooter
        self._event(f"{self.teams[shooter]}: {what} versenkt{hint}. {self.shooter} ist dran.")
        return self.message

    def _resync(self, after_counts, seen=frozenset()):
        """Selbstheilung: wurde frueher eine Kugel faelschlich als versenkt gezaehlt,
        die Kamera sieht aber eindeutig mehr Kugeln als das Spiel erwartet, wird der
        Zaehler wieder angehoben. Nur wenn auch die GESAMTZAHL hoeher ist - sonst koennte
        eine reine Typ-Verwechslung (halbe sieht voll aus) die Zaehler verfaelschen."""
        seen_total = after_counts.get("solid", 0) + after_counts.get("stripe", 0)
        expected_total = self.remaining["solid"] + self.remaining["stripe"]
        if seen_total <= expected_total:
            return
        for g in ("solid", "stripe"):
            sichtbar = min(7, after_counts.get(g, 0))
            if sichtbar > self.remaining[g]:
                self.log.append((time.strftime("%H:%M:%S"),
                                 f"Korrektur: {sichtbar - self.remaining[g]}x {GROUP_NAMES[g]} wieder erkannt"))
                self._give(g, sichtbar - self.remaining[g], seen)

    # ------------------------------------------------------------------ Export
    def to_dict(self):
        return {
            "phase": self.phase,
            "teams": self.teams,
            "frames_won": self.frames_won,
            "turn": self.turn,
            "groups": self.groups,
            "group_names": [GROUP_NAMES[g] for g in self.groups],
            "remaining": self.remaining,
            "eight_on_table": self.eight_on_table,
            "on_table": sorted(self.on_table),
            "manual": self.manual,
            "breaking": self.breaking,
            "ball_in_hand": self.ball_in_hand,
            "winner": self.winner,
            "message": self.message,
            "log": self.log[-15:][::-1],
            "can_undo": bool(self._history),
            "can_undo_shot": self.last_shot_index is not None,
        }
