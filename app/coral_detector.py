"""
Kugelerkennung in drei Stufen:

    1. Kugeln finden    KI-Chip (Coral, YOLOv8n mit 1 Klasse "Kugel")
                        Draufsicht -> verkleinern (Letterbox) -> [Coral] -> Kaesten
    2. gestreift?       Weisse / schwarze 8 / voll / halb                          } KI-Chip, wenn ein Klassen-
    3. Farbe            gelb, blau, rot, violett, orange, gruen, weinrot           } Modell da ist, sonst Statistik
    -> Nummer (0 = Weisse, 1-7 Volle, 8, 9-15 Halbe); jede Nummer gibt es nur einmal.

Stufe 2 + 3 auf dem KI-Chip: ein kleines Netz (training/train_klassen.py) bekommt einen Ausschnitt
um jede gefundene Kugel und liefert direkt die Wahrscheinlichkeit je Nummer 0-15. Ohne dieses Modell
(oder wenn es nicht laedt) rechnet der Pi wie bisher mit Statistik (kugel_merkmale.py).

Stufe 2 + 3 schauen sich jede gefundene Kugel in der vollen Draufsicht an (nicht im
verkleinerten Coral-Bild) und lernen aus festgelegten Bildern (billard_merkmale.json).
Aeltere Modelle mit 16 Klassen (Kugelnummern) gehen weiter: dann zaehlt nur, DASS dort eine
Kugel ist - die Nummer bestimmen Stufe 2 + 3.

Coral-Modell:
    /data/models/billard_edgetpu.tflite      eigenes Modell (Upload im Admin-Panel)
    /app/models/start_edgetpu.tflite         Startmodell (mit Simulator-Bildern trainiert)
Stufe 2 + 3 auf dem KI-Chip (optional):
    /data/models/billard_klassen_edgetpu.tflite   aus training/train_klassen.py (Upload im Admin-Panel);
                                                  ohne Edge-TPU-Teil laeuft es auf dem Prozessor
Merkmale (Stufe 2 + 3, Statistik - Rueckfall):
    /data/models/billard_merkmale.json       gelernt (Admin-Panel oder train_coral.py)
    /app/models/start_merkmale.json          Startwerte aus dem Simulator
Wird eine Datei ersetzt, laedt der Detektor sie von selbst.

Laeuft im Hauptprogramm (kein eigener Container): tflite_runtime + libedgetpu (Coral-Treiber),
beide fuer Python 3.11 / Debian 12 gebaut gegen TensorFlow 2.17.1 (siehe Dockerfile).
"""

import os
import time

import cv2
import numpy as np

from ballnum import Ball
from kugel_merkmale import COLORS, ClassifierStore, crop

MODEL_CLASSES = (1, 16)             # 1 = "Kugel" (neu), 16 = Kugelnummern (aeltere Modelle)
MODELS_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "models")
DATA_MODEL = os.path.join(os.environ.get("DATA_DIR", "/data"), "models", "billard_edgetpu.tflite")
START_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "start_edgetpu.tflite")
DATA_FEATURES = os.path.join(MODELS_DIR, "billard_merkmale.json")
START_FEATURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "start_merkmale.json")
DATA_KLASSEN = os.path.join(MODELS_DIR, "billard_klassen_edgetpu.tflite")
EDGETPU_LIB = "libedgetpu.so.1"


class CoralNotReady(Exception):
    """Kein Stick, kein Modell oder Modell kaputt - es wird nichts ausgewertet."""


def _default_interpreter(path):
    """Interpreter mit Edge-TPU-Delegat. Ohne Stick schlaegt das fehl (gewollt: nur Coral)."""
    try:
        from tflite_runtime.interpreter import Interpreter, load_delegate
    except ImportError as e:
        raise CoralNotReady("tflite_runtime fehlt (Programm mit dem Dockerfile bauen)") from e
    try:
        delegate = load_delegate(EDGETPU_LIB)
    except (ValueError, OSError) as e:
        raise CoralNotReady("Kein Coral-Stick gefunden (blauer USB-3-Port?)") from e
    return Interpreter(model_path=path, experimental_delegates=[delegate])


def is_edgetpu_model(path):
    """Fuer den Coral umgewandelt (edgetpu_compiler)? Sonst laeuft es nur auf dem Prozessor."""
    try:
        with open(path, "rb") as f:
            return b"edgetpu-custom-op" in f.read()
    except OSError:
        return False


def _auto_interpreter(path):
    """Klassen-Modell: auf dem Coral, wenn umgewandelt - sonst auf dem Prozessor (1 Kern)."""
    if is_edgetpu_model(path):
        return _default_interpreter(path)
    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError as e:
        raise CoralNotReady("tflite_runtime fehlt (Programm mit dem Dockerfile bauen)") from e
    return Interpreter(model_path=path, num_threads=1)


class CoralEngine:
    """Haelt das Modell auf dem Coral. Wird nur aus dem Erkennungs-Thread benutzt;
    status() darf von ueberall gelesen werden."""

    def __init__(self, model_paths=(DATA_MODEL, START_MODEL), make_interpreter=_default_interpreter,
                 retry_s=5.0, clock=time.monotonic, check=None, name="coral", missing=None):
        self.model_paths = model_paths
        self.check = check or _check_yolo           # Ausgabeform pruefen -> Klassenzahl
        self.name = name
        self.missing = missing or "Kein Modell vorhanden (im Admin-Panel hochladen)"
        self.make_interpreter = make_interpreter
        self.retry_s = retry_s
        self.clock = clock
        self.interp = None
        self.model = None           # (Pfad, Aenderungszeit)
        self.error = "noch nicht gestartet"
        self.ms = None
        self.nc = None              # Klassen des Modells (1 oder 16)
        self._next_try = 0.0
        self._checked = 0.0

    def _pick(self):
        for p in self.model_paths:
            try:
                return p, os.path.getmtime(p)
            except OSError:
                continue
        return None, None

    def ready(self):
        """Modell geladen? Laedt bei Bedarf (neu) - hoechstens alle paar Sekunden ein Versuch."""
        now = self.clock()
        if self.interp is not None and now - self._checked < 2.0:
            return True
        self._checked = now
        path, mtime = self._pick()
        if self.interp is not None and self.model == (path, mtime):
            return True
        if now < self._next_try:
            return False
        if path is None:
            return self._fail(self.missing)
        if time.time() - mtime < 2:
            return self._fail("Modell wird gerade geschrieben", 1.0)
        try:
            t0 = time.monotonic()
            interp = self.make_interpreter(path)
            interp.allocate_tensors()
            inp = interp.get_input_details()[0]
            interp.set_tensor(inp["index"], np.zeros(inp["shape"], inp["dtype"]))
            interp.invoke()                     # erster Lauf kopiert das Modell auf den Stick
            out = interp.get_output_details()[0]
            nc = self.check([int(v) for v in inp["shape"]], [int(v) for v in out["shape"]])
        except CoralNotReady as e:
            return self._fail(str(e))
        except (ValueError, RuntimeError) as e:
            return self._fail(f"Modell laesst sich nicht laden: {e}")
        self.interp, self.model, self.error, self.nc = interp, (path, mtime), "", nc
        print(f"[{self.name}] Modell geladen in {time.monotonic() - t0:.1f} s: {os.path.basename(path)} "
              f"{[int(v) for v in inp['shape']]}", flush=True)
        return True

    def _fail(self, msg, retry=None):
        if msg != self.error:
            print(f"[{self.name}] {msg}", flush=True)
        self.interp, self.model, self.error = None, None, msg
        self._next_try = self.clock() + (self.retry_s if retry is None else retry)
        return False

    def input_info(self):
        d = self.interp.get_input_details()[0]
        scale, zp = d.get("quantization", (0.0, 0))
        return [int(v) for v in d["shape"]], np.dtype(d["dtype"]), float(scale), int(zp)

    def run(self, data):
        t0 = time.monotonic()
        try:
            inp = self.interp.get_input_details()[0]
            self.interp.set_tensor(inp["index"], data)
            self.interp.invoke()
            o = self.interp.get_output_details()[0]
            out = self.interp.get_tensor(o["index"])
        except (ValueError, RuntimeError) as e:     # z. B. Stick abgezogen
            self._fail(f"Coral-Fehler: {e}")
            raise CoralNotReady(self.error) from e
        scale, zp = o.get("quantization", (0.0, 0))
        out = out.astype(np.float32)
        if scale:
            out = (out - zp) * scale
        ms = (time.monotonic() - t0) * 1000
        self.ms = ms if self.ms is None else 0.9 * self.ms + 0.1 * ms
        return out

    def status(self):
        path, mtime = self.model or (None, None)
        own = path is not None and path != START_MODEL
        return {
            "ready": self.interp is not None,
            "model": None if path is None else ("eigenes Modell" if own else "Startmodell (Simulator)"),
            "model_time": None if mtime is None else time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime)),
            "error": self.error,
            "ms": None if self.ms is None else round(self.ms, 1),
            "classes": self.nc,
            "on_tpu": None if path is None else is_edgetpu_model(path),
        }


def _check_yolo(inp_shape, out_shape):
    nc = next((k for k in MODEL_CLASSES if 4 + k in out_shape), None)
    if nc is None:
        raise CoralNotReady(f"Modell hat das falsche Format {out_shape} - gebraucht wird ein "
                            f"Kugel-Modell aus training/train_coral.py (1 Klasse)")
    return nc


def _check_klassen(inp_shape, out_shape):
    if len(inp_shape) != 4 or inp_shape[0] != 1 or inp_shape[1] != inp_shape[2] or inp_shape[3] != 3 \
            or out_shape[-1] != 16:
        raise CoralNotReady(f"Klassen-Modell hat das falsche Format {inp_shape} -> {out_shape} - "
                            "gebraucht wird billard_klassen_edgetpu.tflite aus training/train_klassen.py")
    return 16


def klassen_engine(path=DATA_KLASSEN, make_interpreter=_auto_interpreter, **kw):
    """Stufe 2 + 3 auf dem KI-Chip (optional; ohne Datei rechnet der Pi mit Statistik)."""
    return CoralEngine((path,), make_interpreter, check=_check_klassen, name="klassen",
                       missing="kein Klassen-Modell (optional, training/train_klassen.py)", **kw)


def scores_to_parts(s):
    """16 Wahrscheinlichkeiten (Nummern) -> (Art {art: p}, Farbe {farbe: p}) wie bei der Statistik."""
    pk = {"cue": float(s[0]), "eight": float(s[8]), "solid": float(s[1:8].sum()), "stripe": float(s[9:16].sum())}
    pc = s[1:8] + s[9:16]
    pc = pc / max(float(pc.sum()), 1e-9)
    return pk, {c: float(p) for c, p in zip(COLORS, pc)}


# -------------------------------------------------------------- YOLO-Auswertung
_INPUT_LUTS = {}


def input_lut(dt, scale_q, zp):
    """Tabelle Pixelwert 0..255 -> Modelleingabe (gleiche Rechnung wie vorher pro Pixel,
    aber nur 256-mal statt fuer jedes der 640 x 640 x 3 Pixel)."""
    key = (np.dtype(dt).str, float(scale_q), int(zp))
    lut = _INPUT_LUTS.get(key)
    if lut is None:
        v = np.arange(256, dtype=np.float32)
        q = v / 255.0 / (scale_q or 1 / 255.0) + zp
        info = np.iinfo(dt)
        lut = np.clip(np.round(q), info.min, info.max).astype(dt)
        _INPUT_LUTS[key] = lut
    return lut


def quantize_input(img, dt, scale_q, zp):
    """uint8-Bild -> Modelleingabe. Uebliche Coral-Modelle (Skala 1/255) brauchen gar keine
    Rechnung: uint8 bleibt wie es ist, int8 ist nur das oberste Bit gekippt."""
    if np.dtype(dt) == np.float32:
        return img.astype(np.float32) / 255.0
    lut = input_lut(dt, scale_q, zp)
    if lut.dtype == np.uint8 and np.array_equal(lut, np.arange(256)):
        return img
    if lut.dtype == np.int8 and np.array_equal(lut, np.arange(-128, 128)):
        return (img ^ np.uint8(128)).view(np.int8)
    if lut.dtype in (np.uint8, np.int8):
        return cv2.LUT(img, lut)
    return lut[img]


def letterbox(img, out_h, out_w, fill=114):
    """Bild ohne Verzerrung auf die Modellgroesse bringen (Rand grau) - genau wie im Training."""
    ih, iw = img.shape[:2]
    scale = min(out_w / iw, out_h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    dx, dy = (out_w - nw) // 2, (out_h - nh) // 2
    canvas = np.full((out_h, out_w, 3), fill, np.uint8)
    canvas[dy:dy + nh, dx:dx + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return canvas, scale, dx, dy


def decode_yolo(out, in_w, in_h, conf=0.35, iou=0.45, nc=1):
    """YOLOv8-Ausgabe (1, 4+nc, N) -> [(cx, cy, w, h, score)] in Eingabe-Pixeln.
    Bei mehreren Klassen (aeltere Modelle) zaehlt nur die beste: "hier ist eine Kugel"."""
    a = np.squeeze(out, 0) if out.ndim == 3 else out
    if a.shape[0] == 4 + nc:
        a = a.T
    if a.shape[1] != 4 + nc:
        raise CoralNotReady(f"Unerwartete Modellausgabe {out.shape} ({nc} Klassen erwartet)")
    boxes = a[:, :4].copy()
    best = a[:, 4:].max(1)
    if boxes.size and float(np.abs(boxes).max()) <= 2.0:   # Edge-TPU-Export liefert 0..1
        boxes *= np.array([in_w, in_h, in_w, in_h], np.float32)
    keep = np.nonzero(best >= conf)[0]
    if not len(keep):
        return []
    tl = [[float(b[0] - b[2] / 2), float(b[1] - b[3] / 2), float(b[2]), float(b[3])] for b in boxes[keep]]
    idx = np.array(cv2.dnn.NMSBoxes(tl, [float(s) for s in best[keep]], conf, iou)).reshape(-1)
    return [(*boxes[keep[i]], float(best[keep[i]])) for i in idx]


def assign_unique(cands):
    """Jede Nummer gibt es nur einmal. Haben zwei Treffer dieselbe Nummer, behaelt der
    sicherere sie; der andere bekommt seine naechstbeste noch freie Nummer."""
    taken = set()
    for c in sorted(cands, key=lambda c: -c["score"]):
        order = np.argsort(-c["scores"])
        n = next((int(k) for k in order if int(k) not in taken), int(order[0]))
        if n != c["number"]:
            c["score"] = float(c["scores"][n])
        c["number"] = n
        taken.add(n)
    return cands


class CoralDetector:
    """detect(draufsicht) -> Liste von Ball (mit Nummer). Wirft CoralNotReady ohne Coral/Modell."""

    name = "coral"

    def __init__(self, radius_px, engine: CoralEngine, conf=0.35, features: ClassifierStore = None,
                 klassen: CoralEngine = None):
        self.r = float(radius_px)
        self.engine = engine
        self.conf = conf
        self.features = features or default_features()
        self.klassen = klassen              # Stufe 2 + 3 auf dem KI-Chip (None = nur Statistik)

    def find(self, warped):
        """Stufe 1 (KI-Chip): wo liegen Kugeln? -> [{"x", "y", "det"}]"""
        if not self.engine.ready():
            raise CoralNotReady(self.engine.error)
        shape, dt, scale_q, zp = self.engine.input_info()
        _, h, w, _ = shape
        img, scale, dx, dy = letterbox(warped, h, w)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)          # erst nach dem Verkleinern: weniger Pixel
        data = quantize_input(img, dt, scale_q, zp)[None]
        out = self.engine.run(data)
        dets = decode_yolo(out, w, h, self.conf, nc=self.engine.nc or 1)

        H, W = warped.shape[:2]
        cands = []
        for cx, cy, bw, bh, score in dets:
            x, y = (cx - dx) / scale, (cy - dy) / scale
            if -self.r <= x <= W + self.r and -self.r <= y <= H + self.r:
                cands.append({"x": float(np.clip(x, 0, W - 1)), "y": float(np.clip(y, 0, H - 1)), "det": score})
        # Doppelte Treffer derselben Kugel weg: zwei echte Kugeln liegen nie naeher als 1 Durchmesser
        kept, min_d2 = [], (1.45 * self.r) ** 2
        for c in sorted(cands, key=lambda c: -c["det"]):
            if all((c["x"] - o["x"]) ** 2 + (c["y"] - o["y"]) ** 2 >= min_d2 for o in kept):
                kept.append(c)
        return kept

    def classify_tpu(self, warped, cands):
        """Stufe 2 + 3 auf dem KI-Chip: Ausschnitt je Kugel -> 16 Wahrscheinlichkeiten. False = nicht bereit."""
        k = self.klassen
        if k is None or not k.ready():
            return False
        shape, dt, scale_q, zp = k.input_info()
        size = shape[1]
        try:
            for c in cands:
                patch = cv2.cvtColor(crop(warped, c["x"], c["y"], self.r, out=size), cv2.COLOR_BGR2RGB)
                s = k.run(quantize_input(patch, dt, scale_q, zp)[None]).reshape(-1)[:16]
                s = np.clip(s, 1e-6, None)
                s = s / s.sum()
                c["scores"] = s
                c["p_kind"], c["p_color"] = scores_to_parts(s)
        except CoralNotReady:
            return False
        return True

    def detect(self, warped):
        kept = self.find(warped)
        if not self.classify_tpu(warped, kept):          # Stufe 2 + 3 auf dem Prozessor (Statistik)
            clf = self.features.get()
            if clf is None:
                raise CoralNotReady("Keine Farb-/Streifen-Werte (models/start_merkmale.json fehlt)")
            for c in kept:
                c["scores"], c["p_kind"], c["p_color"] = clf.classify(warped, c["x"], c["y"], self.r)
        for c in kept:
            c["number"] = int(np.argmax(c["scores"]))
            c["score"] = float(c["scores"][c["number"]])
        return [Ball.from_number(c["x"], c["y"], self.r, c["number"], c["score"]) for c in assign_unique(kept)]


_FEATURES = None


def default_features():
    """Eine gemeinsame Merkmal-Datei fuer alle Detektoren (laedt neu, wenn sie ersetzt wird)."""
    global _FEATURES
    if _FEATURES is None:
        _FEATURES = ClassifierStore((DATA_FEATURES, START_FEATURES))
    return _FEATURES
