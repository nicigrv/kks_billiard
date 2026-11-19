"""
Spielaufzeichnung: JEDES Rack wird automatisch aufgezeichnet - als Daten (Kugelpositionen und
Spielstand je Zug, KEINE Kamerabilder). Das Video daraus baut die Trainingsseite im Netz (VPS),
damit der Pi waehrend des Spiels nicht rechnen muss.

Ablauf:
    Spiel starten / Neues Rack  -> Aufzeichnung beginnt (RoundRecorder.start)
    jeder Stoss, jede Korrektur -> ein Schritt (add), laufend in laufend.json gesichert
    Rack vorbei / Abbruch       -> fertig/<name>.json (stop); uploader.py schickt sie zum VPS
                                   und loescht sie danach. Der VPS baut daraus das Video.
    Ohne Trainingsseite baut der Pi das Video selbst (mit niedriger Prioritaet).

Ablage in /data/recordings/:
    laufend.json                         die gerade laufende Aufzeichnung
    fertig/runde-20261103-201500_Rot-Blau.json   fertige Runde, wartet auf den Upload
    runde-20261103-201500_Rot-Blau.mp4   Video (nur ohne Trainingsseite, H.264 fuer iPad/iPhone)

Im Video rollen die Kugeln von einem Zug zum naechsten an ihre neue Stelle,
versenkte Kugeln laufen in die naechste Tasche und verschwinden. build_video() wird auch von der
Trainingsseite benutzt (training_server/replay.py ist eine Kopie dieser Datei).
"""

import copy
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time

import cv2

from planner import POCKETS
from scene import SceneRenderer, surface_to_bgr

MAX_STEPS = 400
NAME_RE = re.compile(r"runde-[0-9]{8}-[0-9]{6}(-[0-9]{1,3})?(_[A-Za-z0-9_-]{1,24}-[A-Za-z0-9_-]{1,24})?")


def _nice():
    """Video bauen stoert nichts anderes: niedrigste Prioritaet. Unter Linux gilt das nur fuer den
    aufrufenden Thread (die Erkennung laeuft normal weiter); ffmpeg erbt sie."""
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    except (OSError, AttributeError):
        pass


def _safe(text):
    text = (text or "").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = text.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    return re.sub(r"[^A-Za-z0-9_-]+", "", text.replace(" ", "_"))[:24] or "Team"


def static_scene(scene):
    """Kopie ohne Live-Anteile (Gyroskop), ohne Bildschirm-Texte."""
    s = copy.deepcopy({k: v for k, v in scene.items() if k not in ("cue", "info")})
    s["cue"] = None
    s["info"] = ""
    return s


# ------------------------------------------------------------- Animation
def match_balls(prev, nxt, radius):
    """Welche Kugel ist nachher welche? Erst ueber die Nummer, dann gleicher Typ + naechste."""
    pairs, used = [], set()
    rest_prev = []
    for i, b in enumerate(prev):
        n = b.get("number")
        j = None
        if n not in (None, 0, 8):
            j = next((k for k, a in enumerate(nxt) if k not in used and a.get("number") == n), None)
        if j is None:
            rest_prev.append(i)
        else:
            used.add(j)
            pairs.append((i, j))
    # Rest (Weisse, 8, Kugeln ohne lesbare Farbe): gleicher Typ, kuerzester Weg
    cands = sorted(((math.hypot(prev[i]["x"] - a["x"], prev[i]["y"] - a["y"]), i, k)
                    for i in rest_prev for k, a in enumerate(nxt)
                    if k not in used and a["kind"] == prev[i]["kind"]
                    and (prev[i].get("number") in (None, 0, 8) or a.get("number") is None)))
    done = set()
    for d, i, k in cands:
        if i in done or k in used:
            continue
        done.add(i)
        used.add(k)
        pairs.append((i, k))
    gone = [i for i in range(len(prev)) if i not in {p for p, _ in pairs}]
    new = [k for k in range(len(nxt)) if k not in used]
    return pairs, gone, new


def _ease(t):
    return 1 - (1 - t) ** 3


def tween(a, b, t, radius):
    """Zwischenbild zwischen zwei Zuegen (t = 0..1)."""
    pairs, gone, new = match_balls(a["balls"], b["balls"], radius)
    e = _ease(t)
    balls = []
    for i, k in pairs:
        p, q = a["balls"][i], b["balls"][k]
        balls.append(dict(q, x=p["x"] + (q["x"] - p["x"]) * e, y=p["y"] + (q["y"] - p["y"]) * e))
    for i in gone:
        p = a["balls"][i]
        px, py = min(POCKETS, key=lambda c: math.hypot(c[0] - p["x"], c[1] - p["y"]))
        balls.append(dict(p, x=p["x"] + (px - p["x"]) * e, y=p["y"] + (py - p["y"]) * e,
                          scale=max(0.05, 1 - 0.8 * e), alpha=max(0.0, 1 - e)))
    for k in new:
        balls.append(dict(b["balls"][k], alpha=max(0.0, min(1.0, 2 * t - 1))))
    s = dict(a if t < 1 else b)
    s["balls"] = balls
    s["suggestion"] = None          # waehrend die Kugeln rollen keinen Vorschlag zeigen
    return s


def frames_for(steps, fps, hold_s=1.6, anim_s=1.1, last_hold_s=3.5):
    """Liefert die Szenen aller Videobilder nacheinander."""
    if not steps:
        return
    R = steps[0].get("radius") or 10.0
    for k, cur in enumerate(steps):
        hold = last_hold_s if k == len(steps) - 1 else hold_s
        for _ in range(int(hold * fps)):
            yield cur
        if k + 1 < len(steps):
            n = max(2, int(anim_s * fps))
            for f in range(1, n + 1):
                yield tween(cur, steps[k + 1], f / n, R)


# -------------------------------------------------------------- Video
class _Encoder:
    """H.264 ueber ffmpeg (laeuft auf iPad/iPhone). Rueckfall: OpenCV mit MPEG-4."""

    def __init__(self, path, size, fps):
        self.path = path
        self.proc = self.writer = None
        w, h = size
        if shutil.which("ffmpeg"):
            self.proc = subprocess.Popen(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", path],
                stdin=subprocess.PIPE)          # erbt die niedrige Prioritaet des Video-Threads
        else:
            self.writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            if not self.writer.isOpened():
                raise RuntimeError("Kein Video-Encoder verfuegbar (ffmpeg installieren)")

    def write(self, bgr):
        if self.proc:
            self.proc.stdin.write(bgr.tobytes())
        else:
            self.writer.write(bgr)

    def close(self):
        if self.proc:
            self.proc.stdin.close()
            if self.proc.wait() != 0:
                raise RuntimeError("ffmpeg ist fehlgeschlagen")
        else:
            self.writer.release()


def build_video(steps, path, size=(1280, 720), fps=20, renderer=None):
    renderer = renderer or SceneRenderer()
    tmp = path + ".part.mp4"
    enc = _Encoder(tmp, size, fps)
    last_key, last_bgr = None, None
    try:
        for sc in frames_for(steps, fps):
            key = id(sc)
            if key != last_key:           # Standbilder nur einmal zeichnen
                last_bgr = surface_to_bgr(renderer.render_view(sc, size, show_live_cue=False))
                last_key = key
            enc.write(last_bgr)
    finally:
        enc.close()
    os.replace(tmp, path)
    return path


# ----------------------------------------------------------- Aufzeichnung
class RoundRecorder:
    """Zeichnet jedes Rack automatisch auf (nur Daten). local_build: Funktion -> True, wenn der Pi das
    Video selbst bauen soll (keine Trainingsseite eingerichtet)."""

    def __init__(self, base_dir, size=(1280, 720), fps=20, local_build=lambda: True):
        self.base_dir = base_dir
        self.done_dir = os.path.join(base_dir, "fertig")
        self.size = size
        self.fps = fps
        self.local_build = local_build
        self.lock = threading.Lock()
        self.active = False
        self.building = False
        self.steps = []
        self.name = None
        self.started = None
        self.last_error = ""
        self.last_video = None
        self.last_name = None
        self._renderer = None
        self._resume()

    # ------------------------------------------------------------ Ablage
    def _write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def _data(self, steps, end=None):
        game = steps[-1]["game"] if steps else {}
        return {"name": self.name, "teams": game.get("teams"), "start": self.started, "ende": end,
                "ergebnis": game.get("message", ""), "frames_won": game.get("frames_won"),
                "size": list(self.size), "fps": self.fps, "schritte": steps}

    def _resume(self):
        """Nach einem Neustart: angefangene Aufzeichnung abschliessen (nichts geht verloren)."""
        path = os.path.join(self.base_dir, "laufend.json")
        try:
            with open(path) as f:
                d = json.load(f)
            os.remove(path)
        except (OSError, ValueError):
            return
        if isinstance(d, dict) and NAME_RE.fullmatch(str(d.get("name", ""))) and len(d.get("schritte") or []) >= 2:
            self.name, self.started = d["name"], d.get("start")
            self._finish(d["schritte"])

    def _unique(self, base, tail):
        """Gleicher Zeitstempel (zwei Racks in derselben Sekunde, Neustart): Zaehler anhaengen."""
        for i in range(1000):
            name = base + (f"-{i}" if i else "") + tail
            if name != self.last_name and not any(os.path.exists(p) for p in (
                    os.path.join(self.done_dir, name + ".json"), os.path.join(self.base_dir, name + ".mp4"))):
                return name
        return base + tail

    # ------------------------------------------------------------ Ablauf
    def start(self, scene):
        """Spiel gestartet / neues Rack: eine laufende Aufzeichnung wird vorher abgeschlossen."""
        if self.active:
            self.stop()
        with self.lock:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            teams = scene["game"]["teams"]
            self.name = self._unique(f"runde-{stamp}", f"_{_safe(teams[0])}-{_safe(teams[1])}")
            self.started = time.time()
            self.steps = []
            self.active = True
            self.last_error = ""
        self.add(scene)
        return True

    def add(self, scene):
        """Einen Zug festhalten (nur Daten, keine Grafik - die zeichnet der VPS beim Video)."""
        with self.lock:
            if not self.active or len(self.steps) >= MAX_STEPS:
                return
            self.steps.append(static_scene(scene))
            data = self._data(self.steps)
        try:
            self._write(os.path.join(self.base_dir, "laufend.json"), data)
        except OSError as e:
            self.last_error = str(e)
            print(f"[aufnahme] Fehler beim Speichern: {e}", flush=True)

    def stop(self, scene=None):
        """Rack vorbei / abgebrochen: Aufzeichnung abschliessen."""
        if scene is not None:
            self.add(scene)
        with self.lock:
            if not self.active:
                return False
            self.active = False
            steps = list(self.steps)
            self.steps = []
        try:
            os.remove(os.path.join(self.base_dir, "laufend.json"))
        except OSError:
            pass
        if len(steps) < 2:                           # nur gestartet, gleich wieder abgebrochen
            return True
        self._finish(steps)
        return True

    def _finish(self, steps):
        data = self._data(steps, end=time.time())
        try:
            self._write(os.path.join(self.done_dir, self.name + ".json"), data)
        except OSError as e:
            self.last_error = str(e)
            print(f"[aufnahme] Fehler beim Speichern: {e}", flush=True)
            return
        self.last_name = self.name
        if self.local_build():                       # ohne Trainingsseite: Video selbst bauen
            self.building = True
            threading.Thread(target=self._build, args=(steps, self.name), name="video", daemon=True).start()

    def _build(self, steps, name):
        _nice()                                      # nur dieser Thread: Erkennung geht vor
        path = os.path.join(self.base_dir, name + ".mp4")
        try:
            t0 = time.monotonic()
            if self._renderer is None:
                self._renderer = SceneRenderer()
            build_video(steps, path, self.size, self.fps, self._renderer)
            self.last_video = os.path.basename(path)
            try:
                os.remove(os.path.join(self.done_dir, name + ".json"))
            except OSError:
                pass
            print(f"[aufnahme] Video fertig: {path} ({time.monotonic() - t0:.0f} s)", flush=True)
        except Exception as e:     # Video kaputt darf das Spiel nicht stoppen
            self.last_error = f"Video fehlgeschlagen: {e}"
            print(f"[aufnahme] {self.last_error}", flush=True)
        finally:
            self.building = False

    # ------------------------------------------------------------ fuer den Upload
    def finished(self):
        """Fertige Runden, die noch zum VPS muessen: [(name, pfad)], aelteste zuerst."""
        try:
            names = sorted(n[:-5] for n in os.listdir(self.done_dir) if n.endswith(".json"))
        except OSError:
            return []
        return [(n, os.path.join(self.done_dir, n + ".json")) for n in names if NAME_RE.fullmatch(n)]

    # ------------------------------------------------------------ Anzeige
    def videos(self):
        """Videos, die der Pi selbst gebaut hat (nur ohne Trainingsseite)."""
        try:
            names = [n for n in os.listdir(self.base_dir) if n.endswith(".mp4") and not n.endswith(".part.mp4")]
        except OSError:
            return []
        out = []
        for n in sorted(names, reverse=True)[:30]:
            try:
                st = os.stat(os.path.join(self.base_dir, n))
            except OSError:
                continue
            out.append({"name": n, "size_mb": round(st.st_size / 1e6, 1), "url": "/videos/" + n, "status": "fertig",
                        "time": time.strftime("%d.%m.%Y %H:%M", time.localtime(st.st_mtime))})
        return out

    def video_path(self, name):
        """Pfad zu einem Video - nur Dateinamen aus dem Aufnahme-Ordner, keine Pfade."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.mp4", name or "") or name.endswith(".part.mp4"):
            return None
        p = os.path.join(self.base_dir, name)
        return p if os.path.isfile(p) else None

    def status(self):
        return {"active": self.active, "steps": len(self.steps), "building": self.building,
                "error": self.last_error, "last_video": self.last_video, "waiting": len(self.finished())}
