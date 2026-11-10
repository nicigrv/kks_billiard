"""
Tests fuer KI-Chip und Trainingsbilder (ohne echten Coral-Stick):
  * Trainingsbilder: aufnehmen -> festlegen/pruefen -> nichts geht verloren -> Export
  * Trefferquote des Modells (Vorschlag gegen festgelegtes Ergebnis)
  * CoralEngine/CoralDetector mit einem Ersatz-Interpreter, der wie ein Edge-TPU-Modell
    aus dem Training antwortet (int8, 16 Kugelnummern, Kaesten 0..1)
  * Pipeline: ohne KI-Chip wird nichts gezaehlt, aber nach jedem Spielzug ein Bild aufgenommen

    python3 -m sim.test_coral
"""

import json
import os
import sys
import tempfile
import zipfile

import numpy as np

from ballnum import Ball
from coral_detector import CoralDetector, CoralEngine, CoralNotReady, letterbox
from geometry import TABLE_SIZES, Table
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
    print("Trainingsbilder aufnehmen und festlegen")
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d, min_interval_s=0)
        img = np.zeros((400, 800, 3), np.uint8)
        # 1. ohne Modell: Bild ohne Vorschlag
        n1 = rec.save_pending(img, [], 10.0)
        # 2./3. mit Modell-Vorschlag
        sugg = [Ball.from_number(100, 100, 10, 0), Ball.from_number(300, 200, 10, 3), Ball.from_number(500, 300, 10, 10)]
        n2 = rec.save_pending(img, sugg, 10.0, model="eigenes Modell vom 01.12.2026")
        n3 = rec.save_pending(img, sugg, 10.0, model="eigenes Modell vom 01.12.2026")
        check(n1 and n2 and n3 and rec.pending == 3, "Bilder auch ohne Modell (leer) aufgenommen")
        item = rec.next_pending()
        check(item["name"] == n1 and item["balls"] == [] and item["model"] is None, "aeltestes zuerst, ohne Vorschlag")

        # von Hand festlegen: Kugeln ohne Nummer werden abgelehnt
        try:
            rec.review(n1, [{"x": 50, "y": 60, "kind": "solid", "color": ""}])
            check(False, "Kugel ohne Nummer wird abgelehnt")
        except ValueError as e:
            check("ohne Nummer" in str(e), "Kugel ohne Nummer wird abgelehnt")
        rec.review(n1, [{"x": 50, "y": 60, "kind": "solid", "color": "rot", "changed": True},
                        {"x": 400, "y": 200, "kind": "cue", "color": "", "changed": True}])
        lbl = open(os.path.join(d, "geprueft", "labels", n1 + ".txt")).read().split("\n")
        check(lbl[0].startswith("3 0.06250 0.15000") and lbl[1].startswith("0 0.50000 0.50000"),
              "YOLO-Label: Klasse = Kugelnummer")

        # Vorschlag pruefen: 2 von 3 richtig (10 war eigentlich die 11)
        item2 = rec.next_pending()
        final = [dict(b) for b in item2["balls"]]
        final[2].update(kind="stripe", color="rot", number=None, changed=True)
        rec.review(n2, final)
        q = rec.model_quality()
        check(q and q["percent"] == 67 and q["images"] == 1, f"Trefferquote des Modells ({q})")
        rec.review(n3, item2["balls"])                     # alles richtig
        q = rec.model_quality()
        check(q["percent"] == 83 and q["images"] == 2, f"Trefferquote ueber mehrere Bilder ({q['percent']} %)")

        # verwerfen loescht nicht
        n4 = rec.save_pending(img, [], 10.0)
        rec.review(n4, [], discard=True)
        check(rec.discarded == 1 and os.path.isfile(os.path.join(d, "verworfen", n4 + ".jpg")),
              "verworfene Bilder werden aufgehoben, nicht geloescht")

        # Export fuer das Training: nur festgelegte, Labels aus den .json neu gebaut
        zp = os.path.join(d, "train.zip")
        n = rec.export_zip(zp)
        names = zipfile.ZipFile(zp).namelist()
        check(n == 3 and sum(x.startswith("images/") for x in names) == 3, "Trainings-Export: 3 festgelegte Bilder")
        # Alle Bilder
        n5 = rec.save_pending(img, [], 10.0)
        za = os.path.join(d, "alle.zip")
        k = rec.export_all_zip(za)
        names = zipfile.ZipFile(za).namelist()
        check(k == 5 and any(x.startswith("verworfen/") for x in names) and f"offen/{n5}.jpg" in names,
              "Alle Bilder: offene, festgelegte und verworfene")

        # Speicher voll -> nichts mehr aufnehmen
        rec.min_free_bytes = 1 << 62
        check(rec.save_pending(img, [], 10.0, force=True) is None and not rec.disk_ok(),
              "Speicher fast voll -> keine Aufnahme mehr")


# ------------------------------------------------------------------ KI-Chip
IN = 640
N = 16


class FakeInterpreter:
    """Antwortet wie ein Ultralytics-Edge-TPU-Modell: int8 rein, (1, 4+16, N) int8 raus,
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
            o[0, 4 + k, i] = sc
        return np.clip(np.round(o * 255 - 128), -128, 127).astype(np.int8)


def box(x, y, number, score=0.9):
    """Kugel bei (x, y) in der 800x400-Draufsicht -> normierter Kasten im 640er-Eingabebild."""
    return ((x * 0.8) / IN, (y * 0.8 + 160) / IN, 0.03, 0.03, number, score)


def test_engine():
    print("KI-Chip (mit Ersatz-Interpreter)")
    with tempfile.TemporaryDirectory() as d:
        model = os.path.join(d, "billard_edgetpu.tflite")
        now = [100.0]
        eng = CoralEngine(model_paths=(model,), make_interpreter=FakeInterpreter, clock=lambda: now[0])
        det = CoralDetector(10.2, eng)
        warped = np.full((400, 800, 3), 60, np.uint8)
        try:
            det.detect(warped)
            check(False, "ohne Modell: nicht bereit")
        except CoralNotReady as e:
            check("Kein Modell" in str(e), "ohne Modell: nicht bereit (und sagt warum)")

        open(model, "wb").write(b"x")
        os.utime(model, (1, 1))
        now[0] += 10
        FakeInterpreter.boxes = [box(200, 200, 0), box(480, 120, 3), box(640, 280, 11), box(400, 320, 8),
                                 box(482, 121, 3, 0.6),          # Doppeltreffer derselben Kugel
                                 box(100, 100, 11, 0.5)]         # zweite "11" -> naechstbeste Nummer
        balls = det.detect(warped)
        got = {b.number: (round(b.x), round(b.y), b.kind, b.color) for b in balls}
        check(eng.status()["ready"] and eng.status()["model"] == "eigenes Modell", "Modell geladen")
        check(len(balls) == 5, f"Doppeltreffer entfernt ({len(balls)} Kugeln)")
        ok = all(abs(got[n][0] - x) <= 2 and abs(got[n][1] - y) <= 2 for n, x, y in
                 ((0, 200, 200), (3, 480, 120), (11, 640, 280), (8, 400, 320)))
        check(ok, "Positionen korrekt auf den Tisch zurueckgerechnet")
        check(got[3][2:] == ("solid", "rot") and got[11][2:] == ("stripe", "rot") and got[0][2] == "cue"
              and got[8][2] == "eight", "Nummer -> Typ und Farbe")
        check(len({b.number for b in balls}) == 5, "jede Nummer nur einmal")
        pad = FakeInterpreter.last_input[0, 10, 10]
        check(list(pad) == [-14, -14, -14], "Eingabe int8 mit grauem Rand wie im Training")

        # Modell mit falschem Format (z. B. altes 4-Klassen-Modell)
        FakeInterpreter.out_classes = 4
        os.utime(model, (5, 5))
        now[0] += 10
        try:
            det.detect(warped)
            check(False, "altes Modell mit 4 Klassen wird abgelehnt")
        except CoralNotReady as e:
            check("16 Kugelnummern" in str(e), "altes Modell mit 4 Klassen wird abgelehnt")
        FakeInterpreter.out_classes = N

    img, s, dx, dy = letterbox(np.zeros((400, 800, 3), np.uint8), 640, 640)
    check((s, dx, dy) == (0.8, 0, 160), "Letterbox 800x400 -> 640x640")


# ----------------------------------------------------------------- Pipeline
def test_pipeline_without_chip():
    print("Pipeline ohne KI-Chip")
    L = TABLE_SIZES["8ft"]
    W = L / 2
    t = [0.0]
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d, min_interval_s=0)
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
        first = rec.pending
        show(b[:1] + a[1:], 1)            # Bewegung (Weisse rollt)
        show(b, 25)                       # wieder ruhig
        check(rec.pending == first + 1, "nach dem Spielzug ein Bild aufgenommen (ohne Vorschlag)")
        show(b, 25)
        check(rec.pending == first + 1, "ruhiger Tisch: kein weiteres Bild")
        check(pipe.capture_now() is not None and rec.pending == first + 2, "Knopf 'Jetzt aufnehmen'")


if __name__ == "__main__":
    test_training_data()
    test_engine()
    test_pipeline_without_chip()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
