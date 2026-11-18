"""
Stufe 2 und 3 der Kugelerkennung: Ist die Kugel gestreift? Welche Farbe hat sie?

    Stufe 1  KI-Chip (Coral):  findet die Kugeln (wo liegt eine Kugel?)        -> coral_detector.py
    Stufe 2  diese Datei:      Art: Weisse / schwarze 8 / voll / halb (gestreift)
    Stufe 3  diese Datei:      Farbe: gelb, blau, rot, violett, orange, gruen, weinrot
             -> Nummer = Farbe (1-7), bei Halben + 8; Weisse = 0, schwarze = 8

Fuer jede gefundene Kugel wird ein kleiner Ausschnitt der Draufsicht angeschaut (Farbraum Lab):
  * Art:   Anteil weisser, dunkler und farbiger Pixel (Halbe haben viel Weiss, die 8 viel Schwarz)
  * Farbe: mittlere Farbe der farbigen Pixel (a, b = Farbton, L = Helligkeit)
Entschieden wird mit je einem kleinen statistischen Modell (pro Klasse eine Normalverteilung,
"QDA"). Das Modell wird aus festgelegten Bildern gelernt - eure Lampe, eure Kamera:
  * training/train_coral.py (macht es nach dem YOLO-Training automatisch, oder nur das:
    --nur-farben, dauert Sekunden, keine Grafikkarte noetig) -> billard_merkmale.json
  * Trainingsseite im Netz (VPS) -> Admin -> "Farben lernen" aus den fertig festgelegten Bildern;
    der Pi holt die neue Datei von selbst (auf dem Pi selbst wird nicht gelernt)
Ohne gelerntes Modell gelten Startwerte aus dem Simulator (models/start_merkmale.json).

Die Datei braucht nur numpy + OpenCV und wird auch von training/train_coral.py benutzt
(in Google Colab mit hochladen). training_server/kugel_merkmale.py ist eine Kopie davon
(die Formpruefung in CI meldet, wenn beide nicht gleich sind).
"""

import json
import math
import os
import time

import cv2
import numpy as np

KINDS = ["cue", "eight", "solid", "stripe"]
KIND_NAMES = {"cue": "Weiße", "eight": "schwarze 8", "solid": "voll", "stripe": "halb (gestreift)"}
COLORS = ["gelb", "blau", "rot", "violett", "orange", "gruen", "weinrot"]   # Index + 1 = Nummer der Vollen

CROP = 32            # Ausschnitt wird auf CROP x CROP Pixel gebracht (unabhaengig von der Tischgroesse)
SPAN = 1.25          # ... und deckt 2 * SPAN * Radius ab
INNER = 0.8          # nur das Innere der Kugel auswerten (Rand: Tuch, Schatten)

KIND_FEATURES = ["weiss", "weiss_streng", "dunkel", "farbig", "hell_median", "hell_spanne"]
COLOR_FEATURES = ["a", "b", "hell"]

_yy, _xx = np.mgrid[0:CROP, 0:CROP] + 0.5 - CROP / 2
_R_CROP = CROP / 2 / SPAN
MASK = np.hypot(_xx, _yy) <= INNER * _R_CROP


def number_of(kind, color):
    if kind == "cue":
        return 0
    if kind == "eight":
        return 8
    c = COLORS.index(color) + 1
    return c if kind == "solid" else c + 8


def split_number(n):
    """Nummer -> (Art, Farbe oder None)."""
    n = int(n)
    if n == 0:
        return "cue", None
    if n == 8:
        return "eight", None
    if 1 <= n <= 7:
        return "solid", COLORS[n - 1]
    if 9 <= n <= 15:
        return "stripe", COLORS[n - 9]
    raise ValueError(f"Kugelnummer {n}")


# ------------------------------------------------------------------ Merkmale
def crop(img, x, y, r, out=CROP):
    """Ausschnitt um die Kugel (BGR, 2 * SPAN * r breit), auf out x out gebracht. Ragt er ueber den
    Rand, wird der Rand fortgesetzt (das Innere der Kugel liegt praktisch immer im Bild).
    Auch fuer das Coral-Modell von Stufe 2 + 3 (out = Eingabegroesse des Modells)."""
    size = max(4, int(math.ceil(2 * SPAN * r)))
    patch = cv2.getRectSubPix(img, (size, size), (float(x), float(y)))
    return cv2.resize(patch, (out, out), interpolation=cv2.INTER_AREA if size > out else cv2.INTER_LINEAR)


def features(patch):
    """Ausschnitt (BGR, CROP x CROP) -> (Merkmale fuer die Art, Merkmale fuer die Farbe)."""
    lab = cv2.cvtColor(patch.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)   # L 0..100, a/b ca. -128..127
    L, a, b = lab[..., 0][MASK], lab[..., 1][MASK], lab[..., 2][MASK]
    C = np.hypot(a, b)
    white = (C < 16) & (L > 58)
    white_strict = (C < 11) & (L > 72)
    dark = (L < 28) & (C < 22)
    colored = ~white & ~dark & (C >= 16)
    n = float(L.size)
    kind = [white.sum() / n, white_strict.sum() / n, dark.sum() / n, colored.sum() / n,
            float(np.median(L)), float(np.percentile(L, 90) - np.percentile(L, 10))]
    # Farbe: die farbigsten Pixel (Nummernkreis, Glanzlicht und Streifen-Weiss fallen weg)
    sel = colored if colored.sum() >= 6 else C >= np.percentile(C, 70)
    w = C[sel]
    top = w >= np.median(w)
    color = [float(np.median(a[sel][top])), float(np.median(b[sel][top])), float(np.median(L[sel][top]))]
    return np.array(kind, np.float64), np.array(color, np.float64)


def ball_features(img, x, y, r):
    return features(crop(img, x, y, r))


# ------------------------------------------------------------------ Modell (QDA)
class Gauss:
    """Pro Klasse eine Normalverteilung (Mittelwert + Kovarianz). Klein, schnell, als JSON speicherbar."""

    def __init__(self, classes, params):
        self.classes = list(classes)
        self.params = params          # {klasse: {"mean": [...], "cov": [[...]], "n": int}}
        self._prep()

    def _prep(self):
        self._m, self._inv, self._logdet = [], [], []
        for c in self.classes:
            p = self.params[c]
            cov = np.array(p["cov"], np.float64)
            sign, logdet = np.linalg.slogdet(cov)
            self._m.append(np.array(p["mean"], np.float64))
            self._inv.append(np.linalg.inv(cov))
            self._logdet.append(logdet)

    @classmethod
    def fit(cls, X, y, classes, fallback=None, shrink=0.15, min_n=5):
        """X: Merkmale (n x d), y: Klassennamen. Klassen mit zu wenig Beispielen nehmen die Werte
        aus fallback (z. B. dem Startmodell)."""
        X = np.asarray(X, np.float64)
        y = np.asarray(y)
        d = X.shape[1]
        pooled = np.cov(X.T) if len(X) > d else np.eye(d)
        floor = np.diag(np.maximum(np.diag(pooled), 1e-6)) * 0.02 + np.eye(d) * 1e-4
        params = {}
        for c in classes:
            Xc = X[y == c]
            if len(Xc) < min_n:
                if fallback is not None and c in fallback.params:
                    params[c] = dict(fallback.params[c], n=int(len(Xc)), geerbt=True)
                    continue
                raise ValueError(f"Zu wenig Beispiele fuer '{c}' ({len(Xc)})")
            cov = np.cov(Xc.T) if len(Xc) > 1 else np.zeros((d, d))
            s = shrink if len(Xc) >= 4 * d else 0.5          # wenige Beispiele: staerker glaetten
            cov = (1 - s) * cov + s * np.diag(np.diag(pooled)) + floor
            params[c] = {"mean": Xc.mean(0).tolist(), "cov": cov.tolist(), "n": int(len(Xc))}
        return cls(classes, params)

    def log_lik(self, x):
        x = np.asarray(x, np.float64)
        out = []
        for m, inv, ld in zip(self._m, self._inv, self._logdet):
            dlt = x - m
            out.append(-0.5 * (dlt @ inv @ dlt) - 0.5 * ld)
        return np.array(out)

    def proba(self, x, temp=3.0):
        """Wahrscheinlichkeiten je Klasse. temp > 1 macht das Modell weniger uebermuetig
        (wichtig, damit "jede Nummer nur einmal" noch umsortieren kann)."""
        ll = self.log_lik(x) / temp
        ll -= ll.max()
        p = np.exp(ll)
        return p / p.sum()

    def predict(self, x):
        return self.classes[int(np.argmax(self.log_lik(x)))]

    def to_dict(self):
        return {"classes": self.classes, "params": self.params}

    @classmethod
    def from_dict(cls, d):
        return cls(d["classes"], d["params"])


class BallClassifier:
    """Stufe 2 + 3 zusammen. scores16(img, x, y, r) -> Wahrscheinlichkeit je Kugelnummer 0-15."""

    def __init__(self, kind_model: Gauss, color_model: Gauss, info=None):
        self.kind = kind_model
        self.color = color_model
        self.info = info or {}

    def classify(self, img, x, y, r):
        """-> (scores16, p_art{art: p}, p_farbe{farbe: p})"""
        fk, fc = ball_features(img, x, y, r)
        pk = self.kind.proba(fk)
        pc = self.color.proba(fc)
        pk_d = dict(zip(self.kind.classes, pk))
        pc_d = dict(zip(self.color.classes, pc))
        s = np.zeros(16, np.float64)
        s[0] = pk_d["cue"]
        s[8] = pk_d["eight"]
        for i, c in enumerate(COLORS):
            s[i + 1] = pk_d["solid"] * pc_d[c]
            s[i + 9] = pk_d["stripe"] * pc_d[c]
        s /= s.sum()
        return s, pk_d, pc_d

    def to_dict(self):
        return {"format": "billard-merkmale", "version": 1, "info": self.info,
                "art": self.kind.to_dict(), "farbe": self.color.to_dict()}

    @classmethod
    def from_dict(cls, d):
        if d.get("format") != "billard-merkmale":
            raise ValueError("Keine Merkmal-Datei (billard_merkmale.json)")
        kind, color = Gauss.from_dict(d["art"]), Gauss.from_dict(d["farbe"])
        if kind.classes != KINDS or color.classes != COLORS:
            raise ValueError("Merkmal-Datei hat andere Klassen")
        for g in (kind, color):
            for c in g.classes:
                if len(g.params[c]["mean"]) != (len(KIND_FEATURES) if g is kind else len(COLOR_FEATURES)):
                    raise ValueError("Merkmal-Datei passt nicht zu dieser Programmversion")
        return cls(kind, color, d.get("info") or {})

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls.from_dict(json.load(f))

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)


# ------------------------------------------------------------------ Lernen
def collect(samples):
    """samples: iterierbar ueber (bild_bgr, [(x, y, nummer), ...], radius_px).
    -> (Art-Merkmale, Arten, Farb-Merkmale, Farben, Nummern)"""
    Xk, yk, Xc, yc, nums = [], [], [], [], []
    for img, balls, r in samples:
        for x, y, n in balls:
            try:
                kind, color = split_number(n)
            except ValueError:
                continue
            fk, fc = ball_features(img, x, y, r)
            Xk.append(fk)
            yk.append(kind)
            nums.append(int(n))
            if color is not None:
                Xc.append(fc)
                yc.append(color)
            else:
                Xc.append(None)
                yc.append(None)
    return Xk, yk, Xc, yc, nums


def fit(data, fallback: BallClassifier = None, source=""):
    Xk, yk, Xc, yc, _ = data
    if not Xk:
        raise ValueError("Keine Kugeln mit Nummer gefunden")
    xc = [x for x in Xc if x is not None]
    cc = [c for c in yc if c is not None]
    kind = Gauss.fit(Xk, yk, KINDS, fallback.kind if fallback else None)
    color = Gauss.fit(xc, cc, COLORS, fallback.color if fallback else None)
    counts = {str(n): 0 for n in range(16)}
    for k, c in zip(yk, yc):
        counts[str(number_of(k, c))] += 1
    info = {"erstellt": time.strftime("%d.%m.%Y %H:%M"), "quelle": source, "kugeln": len(Xk),
            "je_nummer": counts,
            "geerbt": sorted([c for c in KINDS if kind.params[c].get("geerbt")]
                             + [c for c in COLORS if color.params[c].get("geerbt")])}
    return BallClassifier(kind, color, info)


def evaluate(clf: BallClassifier, data):
    """Trefferquote (Art, Farbe, Nummer) und die haeufigsten Verwechslungen."""
    Xk, yk, Xc, yc, nums = data
    ok_k = ok_c = ok_n = n_c = 0
    confusions = {}
    for fk, k, fc, c, n in zip(Xk, yk, Xc, yc, nums):
        pk = clf.kind.predict(fk)
        ok_k += pk == k
        pc = None
        if c is not None:
            n_c += 1
            pc = clf.color.predict(fc)
            ok_c += pc == c
        # Nummer wie im Betrieb: Art x Farbe
        pk_v = clf.kind.proba(fk)
        pc_v = clf.color.proba(fc) if fc is not None else np.ones(len(COLORS)) / len(COLORS)
        s = {0: pk_v[0], 8: pk_v[1]}
        for i in range(7):
            s[i + 1] = pk_v[2] * pc_v[i]
            s[i + 9] = pk_v[3] * pc_v[i]
        guess = max(s, key=s.get)
        ok_n += guess == n
        if guess != n:
            confusions[(n, guess)] = confusions.get((n, guess), 0) + 1
    total = max(len(nums), 1)
    return {"kugeln": len(nums), "art": ok_k / total, "farbe": ok_c / max(n_c, 1), "nummer": ok_n / total,
            "verwechslungen": sorted(confusions.items(), key=lambda kv: -kv[1])[:8]}


def report(res, title="Prüfbilder"):
    lines = [f"{title}: {res['kugeln']} Kugeln - Art richtig {res['art'] * 100:.0f} %, "
             f"Farbe richtig {res['farbe'] * 100:.0f} %, Nummer richtig {res['nummer'] * 100:.0f} %"]
    if res["verwechslungen"]:
        lines.append("  häufigste Verwechslungen: " + ", ".join(
            f"{a}→{b} ({k}x)" for (a, b), k in res["verwechslungen"]))
    return "\n".join(lines)


# ------------------------------------------------------------------ im Betrieb
class ClassifierStore:
    """Laedt die Merkmal-Datei und laedt sie neu, sobald sie ersetzt wird (wie das Coral-Modell)."""

    def __init__(self, paths, clock=time.monotonic):
        self.paths = paths
        self.clock = clock
        self.clf = None
        self.loaded = (None, None)
        self.error = ""
        self._checked = -1e9

    def get(self):
        now = self.clock()
        if self.clf is not None and now - self._checked < 2.0:
            return self.clf
        self._checked = now
        for p in self.paths:
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if self.loaded == (p, mtime):
                return self.clf
            try:
                clf = BallClassifier.load(p)
            except (OSError, ValueError, KeyError, TypeError, np.linalg.LinAlgError) as e:
                self.error = f"{os.path.basename(p)}: {e}"
                print(f"[merkmale] {self.error}", flush=True)
                continue
            self.clf, self.loaded, self.error = clf, (p, mtime), ""
            print(f"[merkmale] geladen: {os.path.basename(p)}", flush=True)
            return clf
        return self.clf

    def status(self, start_path=None):
        self.get()                                    # (neu) laden, falls noetig
        p, mtime = self.loaded
        if p is None:
            return {"ready": False, "own": False, "model": None, "error": self.error or "keine Merkmal-Datei"}
        info = self.clf.info if self.clf else {}
        return {"ready": True, "own": p != start_path,
                "model": "Startwerte (Simulator)" if p == start_path else "eigene Farben",
                "model_time": info.get("erstellt") or time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime)),
                "balls": info.get("kugeln"), "inherited": info.get("geerbt") or [], "error": self.error}


def _subset(data, idx):
    return tuple([part[i] for i in idx] for part in data)


def learn(samples, fallback: BallClassifier = None, source="", val_every=7):
    """Lernen + ehrliche Pruefung: jede val_every-te Kugel wird zurueckgehalten und damit geprueft,
    danach wird mit allen Kugeln gelernt. -> (Modell, Pruefergebnis oder None)"""
    data = collect(samples)
    n = len(data[0])
    if n == 0:
        raise ValueError("Keine festgelegten Kugeln gefunden")
    val = [i for i in range(n) if i % val_every == val_every - 1]
    res = None
    if len(val) >= 10:
        train = [i for i in range(n) if i % val_every != val_every - 1]
        res = evaluate(fit(_subset(data, train), fallback, source), _subset(data, val))
    clf = fit(data, fallback, source)
    if res:
        clf.info["pruefung"] = {k: (round(v, 3) if isinstance(v, float) else v)
                                for k, v in res.items() if k != "verwechslungen"}
    return clf, res
