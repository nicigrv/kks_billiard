"""
Tests fuer KI-Chip und Trainingsbilder (ohne echten Coral-Stick):
  * Trainingsbilder: aufnehmen (mit Vorschlag des KI-Chips), Einzelkugel, nach dem Upload loeschen
    (festgelegt und gelernt wird nur auf der Trainingsseite im Netz, nicht auf dem Pi)
  * CoralEngine/CoralDetector mit einem Ersatz-Interpreter, der wie ein Edge-TPU-Modell
    aus dem Training antwortet (int8, 1 Klasse "Kugel" bzw. aeltere 16 Kugelnummern, Kaesten 0..1);
    die Nummer kommt aus Stufe 2 + 3 (gestreift? Farbe?) am Simulator-Bild
  * Pipeline: ohne KI-Chip wird nichts gezaehlt, Bilder entstehen nur per Knopf

    python3 -m sim.test_coral
"""

import json
import os
import sys
import tempfile

import numpy as np

from ballnum import Ball
import cv2

import kugel_merkmale as km
from coral_detector import CoralDetector, CoralEngine, CoralNotReady, default_features, klassen_engine, letterbox
from geometry import TABLE_SIZES, WARP_H, WARP_W, Table
from pipeline import Pipeline
from recorder import Recorder
from sim.render import DEFAULT_CORNERS, SimBall, render_topdown, to_camera

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


# ---------------------------------------------------------------- Trainingsbilder
def test_training_data():
    print("Trainingsbilder aufnehmen (für die Trainingsseite)")
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d)
        img = np.zeros((400, 800, 3), np.uint8)
        n1 = rec.save_pending(img, [], 10.0)
        sugg = [Ball.from_number(100, 100, 10, 0), Ball.from_number(300, 200, 10, 3), Ball.from_number(500, 300, 10, 10)]
        n2 = rec.save_pending(img, sugg, 10.0, model="eigenes Modell vom 01.12.2026")
        check(n1 and n2 and n1 != n2 and rec.pending == 2, "Bilder auch ohne Modell (leer) aufgenommen")
        with open(os.path.join(rec.pending_dir, n2 + ".json")) as f:
            meta = json.load(f)
        check([b["number"] for b in meta["balls"]] == [0, 3, 10] and meta["model"].startswith("eigenes"),
              "Vorschlag des KI-Chips (Nummern) liegt dabei")
        e1 = rec.save_single(img, 5, 10.0, b"\xff\xd8leer")
        e2 = rec.save_single(img, 5, 10.0, b"\xff\xd8leer")
        leer = [n for n in os.listdir(rec.single_dir) if n.startswith("leer-")]
        check(e1 and e2 and rec.single == 2 and len(leer) == 1, "Einzelkugel: 2 Bilder, leerer Tisch nur einmal")
        check(rec.waiting == 4, f"wartet auf den Upload: {rec.waiting}")
        rec.remove(os.path.join(rec.pending_dir, n1 + ".jpg"), os.path.join(rec.pending_dir, n1 + ".json"))
        check(rec.pending == 1 and not os.path.exists(os.path.join(rec.pending_dir, n1 + ".jpg")),
              "nach dem Upload gelöscht")

        rec.min_free_bytes = 1 << 62
        check(rec.save_pending(img, [], 10.0) is None and rec.save_single(img, 1, 10.0) is None and not rec.disk_ok(),
              "Speicher fast voll -> keine Aufnahme mehr")
        rec.min_free_bytes = 0

        # Ablage aus aelteren Versionen (am Pi festgelegt / verworfen)
        os.makedirs(rec.ok_img)
        os.makedirs(rec.ok_lbl)
        os.makedirs(rec.discard_dir)
        old = "20261101-120000-000"
        for p in (os.path.join(rec.ok_img, old + ".jpg"), os.path.join(rec.ok_lbl, old + ".json"),
                  os.path.join(rec.ok_lbl, old + ".txt"), os.path.join(rec.discard_dir, old + ".jpg")):
            with open(p, "w") as f:
                f.write("{}")
        check(rec.waiting == 4, "alte festgelegte Bilder warten auch auf den Upload")
        rec.remove(os.path.join(rec.ok_img, old + ".jpg"), os.path.join(rec.ok_lbl, old + ".json"))
        check(rec.verified == 0 and not os.listdir(rec.ok_lbl), "alte Bilder samt YOLO-Datei nach dem Upload gelöscht")
        k = rec.delete_all()
        check(k == 4 and rec.waiting == 0 and not os.path.exists(rec.discard_dir), f"Alle Bilder löschen ({k} Bilder)")
        check(rec.save_pending(img, [], 10.0) is not None and rec.pending == 1, "danach geht Aufnehmen wieder")


# ------------------------------------------------------------------ KI-Chip
IN = 640
N = 1


class FakeInterpreter:
    """Antwortet wie ein Ultralytics-Edge-TPU-Modell: int8 rein, (1, 4+klassen, N) int8 raus,
    Kaesten normiert 0..1. Liefert die Kaesten aus FakeInterpreter.boxes."""
    boxes = []            # (cx, cy, w, h, nummer, score) normiert auf das 640er-Eingabebild
    last_input = None
    out_classes = N

    def __init__(self, path):
        self.inp = {"index": 0, "shape": np.array([1, IN, IN, 3]), "dtype": np.int8, "quantization": (1 / 255, -128)}
        self.out = {"index": 1, "shape": np.array([1, 4 + self.out_classes, 300]), "dtype": np.int8,
                    "quantization": (1 / 255, -128)}

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [self.inp]

    def get_output_details(self):
        return [self.out]

    def set_tensor(self, idx, data):
        FakeInterpreter.last_input = np.array(data)

    def invoke(self):
        pass

    def get_tensor(self, idx):
        o = np.zeros((1, 4 + self.out_classes, 300), np.float32)
        for i, (cx, cy, w, h, k, sc) in enumerate(self.boxes):
            o[0, :4, i] = (cx, cy, w, h)
            o[0, 4 + min(k, self.out_classes - 1), i] = sc
        return np.clip(np.round(o * 255 - 128), -128, 127).astype(np.int8)


def box(x, y, cls=0, score=0.9, err=0.0):
    """Kugel bei (x, y) in der 800x400-Draufsicht -> normierter Kasten im 640er-Eingabebild.
    err: so viele Pixel liegt der Kasten daneben (wie bei einem echten Modell)."""
    return (((x + err) * 0.8) / IN, ((y - err) * 0.8 + 160) / IN, 0.03, 0.03, cls, score)


def sim_table(balls_spec, seed=3):
    """Simulator-Draufsicht mit Kugeln [(nummer, x_px, y_px)] -> (bild, radius)."""
    L = TABLE_SIZES["8ft"]
    W = L / 2
    t = Table(DEFAULT_CORNERS, L)
    balls = [SimBall(n, x / WARP_W * L, y / WARP_H * W, look=1000 + n) for n, x, y in balls_spec]
    rng = np.random.default_rng(seed)
    return t.warp(to_camera(render_topdown(balls, L, W, "blau", rng), DEFAULT_CORNERS)), t.ball_radius_px


def test_engine():
    print("KI-Chip (mit Ersatz-Interpreter) + Stufe 2/3")
    spec = [(0, 200, 200), (3, 480, 120), (11, 640, 280), (8, 400, 320), (5, 120, 330), (13, 300, 90), (2, 700, 80)]
    with tempfile.TemporaryDirectory() as d:
        model = os.path.join(d, "billard_edgetpu.tflite")
        now = [100.0]
        eng = CoralEngine(model_paths=(model,), make_interpreter=FakeInterpreter, clock=lambda: now[0])
        warped, r = sim_table(spec)
        det = CoralDetector(r, eng)
        try:
            det.detect(warped)
            check(False, "ohne Modell: nicht bereit")
        except CoralNotReady as e:
            check("Kein Modell" in str(e), "ohne Modell: nicht bereit (und sagt warum)")

        open(model, "wb").write(b"x")
        os.utime(model, (1, 1))
        now[0] += 10
        FakeInterpreter.boxes = [box(x, y, err=1.5 * (-1) ** i) for i, (n, x, y) in enumerate(spec)]
        FakeInterpreter.boxes.append(box(482, 121, score=0.6))            # Doppeltreffer derselben Kugel
        balls = det.detect(warped)
        got = {b.number: (round(b.x), round(b.y), b.kind, b.color) for b in balls}
        check(eng.status()["ready"] and eng.status()["model"] == "eigenes Modell" and eng.status()["classes"] == 1,
              "Modell mit 1 Klasse (Kugel) geladen")
        check(len(balls) == len(spec), f"Doppeltreffer entfernt ({len(balls)} Kugeln)")
        check(all(n in got and abs(got[n][0] - x) <= 3 and abs(got[n][1] - y) <= 3 for n, x, y in spec),
              f"Stufe 1 + 2 + 3: jede Kugel mit richtiger Nummer an richtiger Stelle ({sorted(got)})")
        check(got.get(3, ())[2:] == ("solid", "rot") and got.get(11, ())[2:] == ("stripe", "rot")
              and got.get(0, ())[2:3] == ("cue",) and got.get(8, ())[2:3] == ("eight",), "Typ und Farbe passen")
        check(len({b.number for b in balls}) == len(balls), "jede Nummer nur einmal")
        pad = FakeInterpreter.last_input[0, 10, 10]
        check(list(pad) == [-14, -14, -14], "Eingabe int8 mit grauem Rand wie im Training")

        # Aelteres Modell mit 16 Kugelnummern: Nummer des Modells wird ignoriert, Stufe 2 + 3 entscheiden
        FakeInterpreter.out_classes = 16
        FakeInterpreter.boxes = [box(x, y, cls=(n + 5) % 16) for n, x, y in spec]   # absichtlich falsche Nummern
        os.utime(model, (3, 3))
        now[0] += 10
        balls = det.detect(warped)
        check(eng.status()["classes"] == 16 and sorted(b.number for b in balls) == sorted(n for n, _, _ in spec),
              "altes 16-Nummern-Modell: nur 'Kugel ja/nein' zaehlt, Nummer aus Stufe 2 + 3")

        # Modell mit falschem Format (z. B. altes 4-Klassen-Modell)
        FakeInterpreter.out_classes = 4
        os.utime(model, (5, 5))
        now[0] += 10
        try:
            det.detect(warped)
            check(False, "altes Modell mit 4 Klassen wird abgelehnt")
        except CoralNotReady as e:
            check("1 Klasse" in str(e), "altes Modell mit 4 Klassen wird abgelehnt")
        FakeInterpreter.out_classes = N

    img, s, dx, dy = letterbox(np.zeros((400, 800, 3), np.uint8), 640, 640)
    check((s, dx, dy) == (0.8, 0, 160), "Letterbox 800x400 -> 640x640")


class FakeKlassen:
    """Wie ein Klassen-Modell aus train_klassen.py (uint8 rein, Skala 1/255; 16 Wahrscheinlichkeiten
    uint8 raus). "Erkennt" mit der Statistik am uebergebenen Ausschnitt - so faellt jeder Fehler beim
    Ausschneiden (Mitte, Groesse, Farbreihenfolge RGB/BGR, Quantisierung) auf."""
    calls = 0
    size = 40
    out_n = 16

    def __init__(self, path):
        self.inp = {"index": 0, "shape": np.array([1, self.size, self.size, 3]), "dtype": np.uint8,
                    "quantization": (1 / 255, 0)}
        self.out = {"index": 1, "shape": np.array([1, self.out_n]), "dtype": np.uint8, "quantization": (1 / 256, 0)}
        self.x = None

    def allocate_tensors(self):
        pass

    def get_input_details(self):
        return [self.inp]

    def get_output_details(self):
        return [self.out]

    def set_tensor(self, idx, data):
        self.x = np.array(data)

    def invoke(self):
        FakeKlassen.calls += 1

    def get_tensor(self, idx):
        rgb = self.x[0]
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        patch = cv2.resize(bgr, (km.CROP, km.CROP), interpolation=cv2.INTER_AREA)
        clf = default_features().get()
        fk, fc = km.features(patch)
        pk, pc = clf.kind.proba(fk), clf.color.proba(fc)
        s = np.zeros(16)
        s[0], s[8] = pk[0], pk[1]
        s[1:8], s[9:16] = pk[2] * pc, pk[3] * pc
        n = int(np.argmax(s))
        o = np.zeros((1, self.out_n), np.uint8)
        o[0, n % self.out_n] = 250
        return o


def test_klassen():
    print("Stufe 2 + 3 auf dem KI-Chip (Klassen-Modell, Ersatz-Interpreter)")
    spec = [(0, 200, 200), (3, 480, 120), (11, 640, 280), (8, 400, 320), (5, 120, 330), (13, 300, 90), (2, 700, 80)]
    with tempfile.TemporaryDirectory() as d:
        model = os.path.join(d, "billard_edgetpu.tflite")
        kpath = os.path.join(d, "billard_klassen_edgetpu.tflite")
        open(model, "wb").write(b"x")
        os.utime(model, (1, 1))
        now = [100.0]
        eng = CoralEngine(model_paths=(model,), make_interpreter=FakeInterpreter, clock=lambda: now[0])
        kl = klassen_engine(kpath, make_interpreter=FakeKlassen, clock=lambda: now[0])
        warped, r = sim_table(spec)
        det = CoralDetector(r, eng, klassen=kl)
        FakeInterpreter.boxes = [box(x, y) for n, x, y in spec]

        balls = det.detect(warped)
        check(sorted(b.number for b in balls) == sorted(n for n, _, _ in spec) and FakeKlassen.calls == 0,
              "ohne Klassen-Modell: Statistik auf dem Prozessor (wie bisher)")
        check(not kl.status()["ready"] and "optional" in kl.status()["error"], "Status: kein Klassen-Modell (optional)")

        open(kpath, "wb").write(b"TFL3 ... edgetpu-custom-op ...")
        os.utime(kpath, (2, 2))
        now[0] += 10
        balls = det.detect(warped)
        got = {b.number: (round(b.x), round(b.y)) for b in balls}
        check(kl.status()["ready"] and kl.status()["on_tpu"], "Klassen-Modell geladen, laeuft auf dem KI-Chip")
        check(FakeKlassen.calls == len(spec) + 1, f"ein Durchlauf je Kugel (+1 Probelauf beim Laden: {FakeKlassen.calls})")
        check(all(n in got and abs(got[n][0] - x) <= 3 and abs(got[n][1] - y) <= 3 for n, x, y in spec),
              f"Nummern vom KI-Chip richtig (Ausschnitt, RGB, Quantisierung) ({sorted(got)})")

        FakeKlassen.out_n = 4                           # falsches Format -> Rueckfall auf die Statistik
        os.utime(kpath, (3, 3))
        now[0] += 10
        FakeKlassen.calls = 0
        balls = det.detect(warped)
        check(sorted(b.number for b in balls) == sorted(n for n, _, _ in spec) and not kl.status()["ready"]
              and "Format" in kl.status()["error"], "falsches Klassen-Modell: Hinweis + Statistik als Rueckfall")
        FakeKlassen.out_n = 16


# ----------------------------------------------------------------- Pipeline
def test_pipeline_without_chip():
    print("Pipeline ohne KI-Chip")
    L = TABLE_SIZES["8ft"]
    W = L / 2
    t = [0.0]
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d)
        eng = CoralEngine(model_paths=(os.path.join(d, "fehlt.tflite"),), clock=lambda: t[0])
        pipe = Pipeline(Table(DEFAULT_CORNERS, L), lambda r: CoralDetector(r, eng), clock=lambda: t[0], recorder=rec)
        rng = np.random.default_rng(1)
        a = [SimBall(0, L * 0.3, W * 0.5, 1), SimBall(5, L * 0.6, W * 0.4, 1)]
        b = [SimBall(0, L * 0.5, W * 0.6, 1), SimBall(5, L * 0.6, W * 0.4, 1)]

        def show(balls, n):
            for _ in range(n):
                pipe.process(to_camera(render_topdown(balls, L, W, "blau", rng), DEFAULT_CORNERS))
                t[0] += 0.1

        show(a, 25)
        check("KI-Chip nicht bereit" in pipe.status_text(), f"Status sagt es ({pipe.status_text()[:50]}...)")
        check(pipe.game.shots == 0, "ohne KI-Chip wird nichts gezaehlt")
        show(b[:1] + a[1:], 1)            # Bewegung (Weisse rollt)
        show(b, 25)                       # wieder ruhig
        check(rec.pending == 0, "ohne Schalter wird nichts automatisch aufgenommen")
        got = []
        pipe.on_capture = lambda name, src: got.append(src)
        check(pipe.capture_now() is not None and rec.pending == 1 and got == ["knopf"], "Bild per Knopf")

        print("Automatisch nach jedem Stoss (auch ohne KI-Chip)")
        pipe.auto_capture = True

        def roll(start, end, frames=8):
            for k in range(1, frames + 1):
                f = k / frames
                moved = [SimBall(e.number, s.x + (e.x - s.x) * f, s.y + (e.y - s.y) * f, 1) for s, e in zip(start, end)]
                show(moved, 1)

        c = [SimBall(0, L * 0.7, W * 0.3, 1), SimBall(5, L * 0.6, W * 0.4, 1)]
        roll(b, c)
        show(c, 10)
        check(rec.pending == 1, "solange die Kugeln noch nicht ruhig sind: kein Bild")
        show(c, 15)
        check(rec.pending == 2 and got[-1] == "auto", "Tisch wieder ruhig -> ein Bild")
        show(c, 30)
        check(rec.pending == 2, "ruhiger Tisch: nicht noch eins")
        roll(c, b)
        show(b, 25)
        check(rec.pending == 2, "zweiter Stoss zu schnell danach (< 8 s): kein Bild")
        show(b, 60)
        show(b[:1] + c[1:], 1)            # nur ein Bild Unruhe (Rauschen, Schatten)
        show(b, 30)
        check(rec.pending == 2, "kurzes Flackern ist kein Stoss")
        roll(b, c)
        show(c, 25)
        check(rec.pending == 3, "naechster Stoss -> naechstes Bild")
        pipe.auto_capture = False
        roll(c, b)
        show(b, 100)
        check(rec.pending == 3, "Schalter aus -> wieder nur per Knopf")


if __name__ == "__main__":
    test_training_data()
    test_engine()
    test_klassen()
    test_pipeline_without_chip()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
