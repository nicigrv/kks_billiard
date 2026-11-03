"""
8-Ball-Spiellogik (vereinfachte WPA-Regeln) fuer zwei Mannschaften.

Was automatisch erkannt wird:
  * versenkte volle/halbe Kugeln, versenkte 8, versenkte Weisse (Foul)
  * Gruppenzuteilung (erster Stoss nach dem Break, bei dem nur eine Gruppe faellt)
  * Zugwechsel (nichts Eigenes versenkt oder Foul)
  * Sieg/Niederlage bei der 8

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
        self._history = []

    # ------------------------------------------------------------- Verwaltung
    def reset_rack(self, breaker):
        self.turn = breaker
        self.breaker = breaker
        self.groups = [None, None]
        self.remaining = {"solid": 7, "stripe": 7}
        self.eight_on_table = True
        self.breaking = True
        self.winner = None
        self.ball_in_hand = False
        self.shots = 0

    def _save(self):
        state = {k: copy.deepcopy(v) for k, v in self.__dict__.items() if k != "_history"}
        self._history.append(state)
        if len(self._history) > 50:
            self._history.pop(0)

    def undo(self):
        if not self._history:
            return "Nichts zum Rückgängigmachen"
        state = self._history.pop()
        hist = self._history
        self.__dict__.update(state)
        self._history = hist
        self._event("Letzte Änderung rueckgaengig gemacht")
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
        self.remaining[group] = max(0, min(7, self.remaining[group] + delta))
        self._event(f"Korrektur: noch {self.remaining[group]} {GROUP_NAMES[group]}.")

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
                   diff=None):
        """Wird nach jedem erkannten Stoss aufgerufen.
        diff: Ergebnis von shots.diff_states (nur bewegte Kugeln). Ohne diff wird ueber
        die Gesamtzahlen gerechnet (fuer Tests und als Rueckfallebene)."""
        if self.phase != "playing":
            return None
        self._save()
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
        # Nie mehr abziehen, als laut Spiel noch liegt
        pocket = {g: min(dec[g], self.remaining[g]) for g in ("solid", "stripe")}
        eight = dec["eight"] > 0 and self.eight_on_table
        scratch = cue_before and not cue_after
        shooter = self.turn
        own = self.groups[shooter]
        own_left_before = self.remaining[own] if own else None
        was_break = self.breaking

        for g in pocket:
            self.remaining[g] -= pocket[g]
        self._resync(after_counts)
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
            self.eight_on_table = False
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

    def _resync(self, after_counts):
        """Selbstheilung: wurde frueher eine Kugel faelschlich als versenkt gezaehlt,
        die Kamera sieht aber eindeutig mehr Kugeln als das Spiel erwartet, wird der
        Zaehler wieder angehoben. Nur wenn auch die GESAMTZAHL hoeher ist - sonst koennte
        eine reine Typ-Verwechslung (halbe sieht voll aus) die Zaehler verfaelschen."""
        seen_total = after_counts.get("solid", 0) + after_counts.get("stripe", 0)
        expected_total = self.remaining["solid"] + self.remaining["stripe"]
        if seen_total <= expected_total:
            return
        for g in ("solid", "stripe"):
            seen = min(7, after_counts.get(g, 0))
            if seen > self.remaining[g]:
                self.log.append((time.strftime("%H:%M:%S"),
                                 f"Korrektur: {seen - self.remaining[g]}x {GROUP_NAMES[g]} wieder erkannt"))
                self.remaining[g] = seen

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
            "breaking": self.breaking,
            "ball_in_hand": self.ball_in_hand,
            "winner": self.winner,
            "message": self.message,
            "log": self.log[-15:][::-1],
            "can_undo": bool(self._history),
        }
