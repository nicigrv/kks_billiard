"""
Meldungen oben rechts - auf dem Beamer und in den Panels (im Stil der Mitteilungen von iPhone/Mac).

Zwei Arten:
  * dauerhafte: solange ein Problem besteht (Kamera weg, KI-Chip nicht bereit, ...).
    App.notices() prueft das bei jedem Bild und meldet sie mit festem Schluessel (key).
    Erst wenn ein Problem `delay` Sekunden ansteht, wird es gezeigt (kein Flackern beim Start).
  * kurze: Ereignisse wie "Queue verbunden" (push), verschwinden nach `ttl` Sekunden.

Jede Meldung: {"key", "level", "title", "body", "t"}  (t = seit wann, Unix-Zeit)
level: error | warning | info | success
"""

import threading
import time

LEVELS = ("error", "warning", "info", "success")
MAX_SHOWN = 3


class Notices:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self._events = []          # kurze Meldungen
        self._since = {}           # dauerhafte: key -> seit wann
        self._n = 0

    def push(self, title, body="", level="info", ttl=6.0, key=None):
        """Kurze Meldung. Gleicher key ersetzt die alte (z. B. mehrfach 'Queue verbunden')."""
        if level not in LEVELS:
            level = "info"
        now = self.clock()
        with self.lock:
            self._n += 1
            key = key or f"e{self._n}"
            self._events = [e for e in self._events if e["key"] != key]
            self._events.append({"key": key, "level": level, "title": str(title), "body": str(body or ""),
                                 "t": now, "until": now + ttl})

    def current(self, problems, now=None):
        """problems: [(key, level, title, body, delay_s)] - was gerade nicht stimmt.
        Liefert die anzuzeigenden Meldungen: Fehler zuerst, dann die neuesten Ereignisse."""
        now = self.clock() if now is None else now
        out = []
        with self.lock:
            keys = set()
            for key, level, title, body, delay in problems:
                keys.add(key)
                t = self._since.setdefault(key, now)
                if now - t >= delay:
                    out.append({"key": key, "level": level, "title": title, "body": body or "", "t": t + delay})
            for k in list(self._since):
                if k not in keys:
                    del self._since[k]
            self._events = [e for e in self._events if e["until"] > now]
            events = sorted(self._events, key=lambda e: -e["t"])
        order = {lv: i for i, lv in enumerate(LEVELS)}
        out.sort(key=lambda n: (order[n["level"]], n["t"]))
        out += [{k: e[k] for k in ("key", "level", "title", "body", "t")} for e in events]
        return out[:MAX_SHOWN]


def age_text(seconds):
    """'jetzt', 'vor 3 Min.', 'vor 2 Std.' - wie bei den Mitteilungen."""
    if seconds < 60:
        return "jetzt"
    if seconds < 3600:
        return f"vor {int(seconds // 60)} Min."
    return f"vor {int(seconds // 3600)} Std."
