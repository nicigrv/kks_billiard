"""
Spielaufzeichnung auf dem VPS: Der Pi zeichnet jedes Rack als Daten auf (Kugeln + Spielstand je Zug,
replay.RoundRecorder) und schickt es hierher. Der Server baut daraus das Video - genau wie es frueher
der Pi gemacht hat (replay.build_video, gleiche Grafik wie auf dem Beamer), damit der Pi waehrend des
Spiels nicht rechnen muss.

  POST /api/aufzeichnung     (X-Upload-Key)  ein fertiges Rack als JSON
  GET  /api/aufzeichnungen   (X-Upload-Key)  Liste fuer das Spiel-Panel am Pi
  GET  /video/<schluessel>.mp4               das Video (ohne Anmeldung; die Adresse ist nicht zu erraten,
                                             nur wer die Liste sehen darf, kennt sie)

Videos werden nacheinander im Hintergrund gebaut (niedrigste Prioritaet). Es bleiben die neuesten
MAX_VIDEOS; aeltere werden geloescht. Ablage: DATA_DIR/videos/<name>.json (Daten), <name>.mp4, index.json.
"""

import json
import os
import queue
import re
import secrets
import threading
import time

MAX_VIDEOS = 200
MAX_SCHRITTE = 400
SIZE, FPS = (1280, 720), 20
NAME_RE = re.compile(r"runde-[0-9]{8}-[0-9]{6}(-[0-9]{1,3})?(_[A-Za-z0-9_-]{1,24}-[A-Za-z0-9_-]{1,24})?")   # wie replay.NAME_RE
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,40}")


class AufzeichnungFehler(ValueError):
    pass


def _bauen_mit_replay(schritte, pfad):
    """Video wie am Pi bauen (replay.py/scene.py sind Kopien aus app/, siehe scripts/kopien.sh)."""
    import replay
    replay.build_video(schritte, pfad, SIZE, FPS)


def _schritte_pruefen(schritte):
    """Nur das, was das Zeichnen braucht - und in vernuenftiger Groesse."""
    if not isinstance(schritte, list) or not 2 <= len(schritte) <= MAX_SCHRITTE:
        raise AufzeichnungFehler("Schritte fehlen")
    for s in schritte:
        if not (isinstance(s, dict) and isinstance(s.get("balls"), list) and isinstance(s.get("game"), dict)
                and len(s["balls"]) <= 40):
            raise AufzeichnungFehler("Schritt im falschen Format")
        for b in s["balls"]:
            if not (isinstance(b, dict) and isinstance(b.get("x"), (int, float)) and isinstance(b.get("y"), (int, float))):
                raise AufzeichnungFehler("Kugel im falschen Format")
        s["qr"], s["notices"], s["cue"] = None, [], None          # Live-Anteile gibt es im Video nicht


class Aufzeichnungen:
    def __init__(self, data_dir, clock=time.time, bauen=None):
        self.dir = os.path.join(data_dir, "videos")
        os.makedirs(self.dir, exist_ok=True)
        self.clock = clock
        self.bauen = bauen or _bauen_mit_replay
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.index = self._laden()
        self._worker = None
        # nach einem Neustart: was noch wartete (oder mitten im Bauen war), neu bauen
        for name, e in sorted(self.index.items(), key=lambda kv: kv[1].get("zeit") or 0):
            if e.get("status") in ("wartet", "baut"):
                e["status"] = "wartet"
                self.queue.put(name)
        if not self.queue.empty():
            self._starten()

    # ------------------------------------------------------------ Ablage
    def _laden(self):
        try:
            with open(os.path.join(self.dir, "index.json")) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _speichern(self):
        tmp = os.path.join(self.dir, "index.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.index, f)
        os.replace(tmp, os.path.join(self.dir, "index.json"))

    # ------------------------------------------------------------ Eingang vom Pi
    def eingang(self, d):
        """Ein fertiges Rack vom Pi. -> True (neu) / False (schon da)."""
        name = str(d.get("name", ""))
        if not NAME_RE.fullmatch(name):
            raise AufzeichnungFehler("Name der Aufzeichnung")
        schritte = d.get("schritte")
        _schritte_pruefen(schritte)
        teams = d.get("teams") if isinstance(d.get("teams"), list) else None
        with self.lock:
            if name in self.index:
                return False
            with open(os.path.join(self.dir, name + ".json"), "w") as f:
                json.dump({"name": name, "schritte": schritte}, f)
            try:
                zeit = float(d.get("start") or self.clock())
            except (TypeError, ValueError):
                zeit = self.clock()
            self.index[name] = {"token": secrets.token_urlsafe(18), "status": "wartet", "zeit": zeit,
                                "eingang": self.clock(), "schritte": len(schritte),
                                "teams": [str(t)[:24] for t in teams[:2]] if teams else None,
                                "ergebnis": str(d.get("ergebnis") or "")[:200]}
            self._speichern()
        self.queue.put(name)
        self._starten()
        return True

    # ------------------------------------------------------------ Videos bauen
    def _starten(self):
        with self.lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._arbeiten, name="videos", daemon=True)
                self._worker.start()

    def _arbeiten(self):
        try:
            os.setpriority(os.PRIO_PROCESS, 0, 19)       # Linux: nur dieser Thread (und ffmpeg) ganz hinten an
        except (OSError, AttributeError):
            pass
        while True:                                      # wartet still auf das naechste Rack
            self.bauen_jetzt(self.queue.get())

    def bauen_jetzt(self, name):
        with self.lock:
            e = self.index.get(name)
            if not e or e["status"] == "fertig":
                return
            e["status"] = "baut"
            self._speichern()
        pfad = os.path.join(self.dir, name + ".mp4")
        t0 = time.monotonic()
        try:
            with open(os.path.join(self.dir, name + ".json")) as f:
                schritte = json.load(f)["schritte"]
            self.bauen(schritte, pfad)
            ergebnis = {"status": "fertig", "groesse": os.path.getsize(pfad), "dauer": round(time.monotonic() - t0, 1),
                        "fehler": None}
            print(f"[videos] {name} fertig ({ergebnis['dauer']} s)", flush=True)
        except Exception as ex:                          # ein kaputtes Video darf den Server nicht stoppen
            ergebnis = {"status": "fehler", "fehler": str(ex)[:300]}
            print(f"[videos] {name} fehlgeschlagen: {ex}", flush=True)
        with self.lock:
            if name in self.index:
                self.index[name].update(ergebnis)
            self._aufraeumen()
            self._speichern()

    def _aufraeumen(self):
        """Nur die neuesten MAX_VIDEOS behalten (Daten + Video)."""
        alt = sorted(self.index, key=lambda n: self.index[n].get("zeit") or 0, reverse=True)[MAX_VIDEOS:]
        for name in alt:
            for endung in (".mp4", ".json"):
                try:
                    os.remove(os.path.join(self.dir, name + endung))
                except OSError:
                    pass
            del self.index[name]

    # ------------------------------------------------------------ Abfragen
    def liste(self, n=30):
        with self.lock:
            namen = sorted(self.index, key=lambda k: self.index[k].get("zeit") or 0, reverse=True)[:n]
            return [{"name": k, "teams": e.get("teams"), "zeit": e.get("zeit"), "status": e["status"],
                     "groesse": e.get("groesse"), "schritte": e.get("schritte"), "ergebnis": e.get("ergebnis"),
                     "fehler": e.get("fehler"),
                     "url": f"/video/{e['token']}.mp4" if e["status"] == "fertig" else None}
                    for k, e in ((k, self.index[k]) for k in namen)]

    def pfad(self, token):
        """Video zur geheimen Adresse - nur fertige."""
        if not TOKEN_RE.fullmatch(token or ""):
            return None
        with self.lock:
            for name, e in self.index.items():
                if e.get("token") == token and e["status"] == "fertig":
                    p = os.path.join(self.dir, name + ".mp4")
                    return p if os.path.isfile(p) else None
        return None

    def status(self):
        with self.lock:
            z = {}
            for e in self.index.values():
                z[e["status"]] = z.get(e["status"], 0) + 1
        return {"anzahl": z, "max": MAX_VIDEOS}
