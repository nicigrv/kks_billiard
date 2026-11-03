"""
Spielaufzeichnung: speichert bei jedem Zug die grafische Ansicht (KEINE Kamerabilder)
und macht am Ende der Runde daraus ein kurzes Video.

Ablage in /data/recordings/:
    runde-20261103-201500/schritt-001.png ...   gespeicherte Grafik je Zug
    runde-20261103-201500/schritte.json         Kugelpositionen + Spielstand je Zug
    runde-20261103-201500_Rot-Blau.mp4          fertiges Video (H.264, laeuft auf iPad/iPhone)

Im Video rollen die Kugeln von einem Zug zum naechsten an ihre neue Stelle,
versenkte Kugeln laufen in die naechste Tasche und verschwinden.
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
                stdin=subprocess.PIPE)
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
    def __init__(self, base_dir, size=(1280, 720), fps=20):
        self.base_dir = base_dir
        self.size = size
        self.fps = fps
        self.lock = threading.Lock()
        self.active = False
        self.building = False
        self.steps = []
        self.dir = None
        self.name = None
        self.last_error = ""
        self.last_video = None
        self._renderer = SceneRenderer()

    def start(self, scene):
        with self.lock:
            if self.active:
                return False
            stamp = time.strftime("%Y%m%d-%H%M%S")
            teams = scene["game"]["teams"]
            self.name = f"runde-{stamp}_{_safe(teams[0])}-{_safe(teams[1])}"
            self.dir = os.path.join(self.base_dir, f"runde-{stamp}")
            os.makedirs(self.dir, exist_ok=True)
            self.steps = []
            self.active = True
            self.last_error = ""
        self.add(scene)
        return True

    def add(self, scene):
        """Einen Zug festhalten: Grafik als PNG + Daten fuer das Video."""
        with self.lock:
            if not self.active or len(self.steps) >= MAX_STEPS:
                return
            step = static_scene(scene)
            self.steps.append(step)
            n = len(self.steps)
            folder = self.dir
        try:
            img = surface_to_bgr(self._renderer.render_view(step, self.size, show_live_cue=False))
            cv2.imwrite(os.path.join(folder, f"schritt-{n:03d}.png"), img)
            with open(os.path.join(folder, "schritte.json"), "w") as f:
                json.dump(self.steps, f)
        except (OSError, cv2.error) as e:
            self.last_error = str(e)
            print(f"[aufnahme] Fehler beim Speichern: {e}", flush=True)

    def stop(self, scene=None):
        """Aufzeichnung beenden und das Video im Hintergrund bauen."""
        if scene is not None:
            self.add(scene)
        with self.lock:
            if not self.active:
                return False
            self.active = False
            steps, name = list(self.steps), self.name
            self.building = True
        threading.Thread(target=self._build, args=(steps, name), name="video", daemon=True).start()
        return True

    def _build(self, steps, name):
        path = os.path.join(self.base_dir, name + ".mp4")
        try:
            t0 = time.monotonic()
            build_video(steps, path, self.size, self.fps, self._renderer)
            self.last_video = os.path.basename(path)
            print(f"[aufnahme] Video fertig: {path} ({time.monotonic() - t0:.0f} s)", flush=True)
        except Exception as e:     # Video kaputt darf das Spiel nicht stoppen
            self.last_error = f"Video fehlgeschlagen: {e}"
            print(f"[aufnahme] {self.last_error}", flush=True)
        finally:
            self.building = False

    def videos(self):
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
            out.append({"name": n, "size_mb": round(st.st_size / 1e6, 1),
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
                "error": self.last_error, "last_video": self.last_video}
