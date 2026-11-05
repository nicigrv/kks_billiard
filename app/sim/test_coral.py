"""
Tests fuer KI-Training und Coral-Anbindung (ohne Coral-Stick):
  * Trainingsbilder: sammeln -> pruefen (passt / korrigiert / verworfen) -> Export-ZIP
  * Coral-Container (coral/coral_server.py) mit einem Ersatz fuer Googles pycoral,
    der bekannte Erkennungen liefert: prueft Protokoll, Letterbox, Quantisierung,
    Rueckrechnung auf den Tisch und den automatischen Rueckfall auf OpenCV.

    python3 -m sim.test_coral
"""

import json
import os
import sys
import tempfile
import threading
import types
import zipfile

import numpy as np

from coral_detector import AutoDetector, CoralClient, letterbox
from geometry import TABLE_SIZES, Table
from recorder import Recorder
from sim.render import DEFAULT_CORNERS, SimBall, render_topdown, to_camera

FAILS = []
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "coral"))


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


class B:
    def __init__(self, x, y, kind, color=""):
        self.x, self.y, self.kind, self.color = x, y, kind, color


def test_training_data():
    print("Trainingsbilder sammeln und pruefen")
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(d, min_interval_s=0)
        img = np.zeros((400, 800, 3), np.uint8)
        balls = [B(100, 100, "cue"), B(300, 200, "solid", "rot"), B(500, 300, "stripe", "blau")]
        n1 = rec.save_pending(img, balls, 10.0, [0, 3, 10])
        n2 = rec.save_pending(img, balls, 10.0, [0, 3, 10])
        n3 = rec.save_pending(img, balls, 10.0, [0, 3, 10])
        check(rec.pending == 3 and rec.verified == 0, "nach drei Spielzuegen drei Bilder zu pruefen")
        item = rec.next_pending()
        check(item["name"] == n1 and len(item["balls"]) == 3 and item["pending"] == 3, "aeltestes Bild zuerst")
        check(rec.pending_image(n1) and rec.pending_image("../config") is None, "nur echte Bildnamen")

        rec.review(n1, item["balls"])                         # passt alles
        fixed = [dict(b) for b in item["balls"]]
        fixed[1].update(kind="stripe", changed=True)          # voll war falsch -> halb
        fixed.append({"x": 700, "y": 50, "kind": "eight", "color": "schwarz", "changed": True})  # fehlte
        rec.review(n2, fixed)
        rec.review(n3, item["balls"], discard=True)
        check(rec.pending == 0 and rec.verified == 2, "2 uebernommen, 1 verworfen")
        lbl = open(os.path.join(d, "geprueft", "labels", n2 + ".txt")).read().split("\n")
        check(lbl[1].startswith("3 0.37500 0.50000") and lbl[3].startswith("1 0.87500 0.12500"),
              "Korrektur steht im YOLO-Label (Klasse, Position)")
        meta = json.load(open(os.path.join(d, "geprueft", "labels", n2 + ".json")))
        check(meta["corrected"] and not json.load(open(os.path.join(d, "geprueft", "labels", n1 + ".json")))["corrected"],
              "korrigiert/unveraendert wird vermerkt")
        try:
            rec.review(n1, [])
            check(False, "doppelt pruefen wird abgelehnt")
        except FileNotFoundError:
            check(True, "doppelt pruefen wird abgelehnt")

        rec.save_verified(img, balls, 10.0, [0, 3, 10])        # Live-Korrektur
        zp = os.path.join(d, "export.zip")
        n = rec.export_zip(zp)
        names = zipfile.ZipFile(zp).namelist()
        check(n == 3 and "classes.txt" in names and sum(x.startswith("images/") for x in names) == 3,
              "Export-ZIP fuer das Training")

        rec.enabled = False
        check(rec.save_pending(img, balls, 10.0) is None, "Sammeln aus -> nichts gespeichert")


# ---------------------------------------------------------------- Coral
IN = 640
SCALE_OUT = 1 / 255.0   # wie echte Exporte: 0..1 auf 256 Stufen (2,5 px bei 640)


class FakeInterpreter:
    """Verhaelt sich wie ein Edge-TPU-Modell aus Ultralytics (int8 rein/raus, (1, 8, N) raus).
    Liefert die Kaesten aus FakeInterpreter.boxes (normiert 0..1 auf das 640er-Eingabebild)."""
    boxes = []          # (cx, cy, w, h, klasse, score)
    last_input = None

    def __init__(self, path):
        self.inp = {"index": 0, "shape": np.array([1, IN, IN, 3]), "dtype": np.int8,
                    "quantization": (1 / 255.0, -128)}
        self.out = {"index": 1, "shape": np.array([1, 8, 100]), "dtype": np.int8,
                    "quantization": (SCALE_OUT, -128)}

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
        o = np.zeros((1, 8, 100), np.float32)
        for i, (cx, cy, w, h, k, s) in enumerate(self.boxes):
            o[0, :4, i] = (cx, cy, w, h)
            o[0, 4 + k, i] = s
        return np.clip(np.round(o / SCALE_OUT - 128), -128, 127).astype(np.int8)


def install_fake_pycoral():
    edgetpu = types.ModuleType("pycoral.utils.edgetpu")
    edgetpu.list_edge_tpus = lambda: [{"type": "usb", "path": "/fake"}]
    edgetpu.make_interpreter = FakeInterpreter
    sys.modules["pycoral"] = types.ModuleType("pycoral")
    sys.modules["pycoral.utils"] = types.ModuleType("pycoral.utils")
    sys.modules["pycoral.utils.edgetpu"] = edgetpu


def test_coral():
    print("Coral-Anbindung (mit Ersatz-Coral)")
    install_fake_pycoral()
    import coral_server
    with tempfile.TemporaryDirectory() as d:
        model = os.path.join(d, "billard_edgetpu.tflite")
        open(model, "wb").write(b"xxxxTFL3" + b"\0" * 2000)
        os.utime(model, (1, 1))                       # "schon lange fertig geschrieben"
        coral_server.MODEL_HOLDER = coral_server.Model(model)
        srv = coral_server.Server(("127.0.0.1", 0), coral_server.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        table = Table(DEFAULT_CORNERS, TABLE_SIZES["8ft"])
        L, Wm = TABLE_SIZES["8ft"], TABLE_SIZES["8ft"] / 2
        sim = [SimBall(0, L * 0.25, Wm * 0.5, 1), SimBall(3, L * 0.6, Wm * 0.3, 1),
               SimBall(10, L * 0.8, Wm * 0.7, 1), SimBall(8, L * 0.5, Wm * 0.8, 1)]
        rng = np.random.default_rng(3)
        warped = table.warp(to_camera(render_topdown(sim, L, Wm, "blau", rng), DEFAULT_CORNERS))
        # Erwartete Positionen in der Draufsicht -> 640er Eingabe (Letterbox: 800x400 -> 640x320, +160 oben)
        truth = [(b.x / L * 800, b.y / Wm * 400, b.kind) for b in sim]
        k = {"cue": 0, "eight": 1, "solid": 2, "stripe": 3}
        FakeInterpreter.boxes = [((x * 0.8) / IN, (y * 0.8 + 160) / IN, 0.03, 0.03, k[kind], 0.9)
                                 for x, y, kind in truth]
        # zweite "Weisse" mit weniger Sicherheit -> darf nicht zweite Weisse werden
        FakeInterpreter.boxes.append(((400 * 0.8) / IN, (60 * 0.8 + 160) / IN, 0.03, 0.03, 0, 0.6))

        client = CoralClient(port=port)
        det = AutoDetector(table.ball_radius_px, client)
        balls, info = det.detect(warped)
        check(det.active == "coral", "Erkennung laeuft ueber den Coral")
        check(client.info["input"]["shape"] == [1, IN, IN, 3], "Modellformat vom Container gemeldet")
        got = sorted((round(b.x), round(b.y), b.kind) for b in balls)
        tol = 0.3 * table.ball_radius_px
        ok = all(any(abs(gx - x) < tol and abs(gy - y) < tol and gk == kind for gx, gy, gk in got)
                 for x, y, kind in truth)
        check(ok, "Positionen + Typ korrekt auf den Tisch zurueckgerechnet")
        check(sum(b.kind == "cue" for b in balls) == 1 and len(balls) == 5, "nur eine Weisse (zweite wird voll/halb)")
        colors = {b.kind: b.color for b in balls}
        check(colors.get("eight") == "schwarz", "8 bekommt Farbe schwarz")
        solid = next(b for b in balls if b.kind == "solid")
        check(solid.color == "rot", f"Farbe/Nummer weiter ueber Bildverarbeitung (3 = rot: {solid.color})")
        pad = FakeInterpreter.last_input[0, 10, 10]
        check(list(pad) == [-14, -14, -14], "Eingabe int8-quantisiert, grauer Rand wie im Training")
        check(info.occlusion < 0.006, "Verdeckung wird weiter berechnet")

        os.remove(model)
        client.info = None
        client._next_try = 0
        balls2, _ = det.detect(warped)
        check(det.active == "opencv" and len(balls2) >= 3, "kein Modell -> automatisch OpenCV")
        check("Noch kein Modell" in client.status()["error"], "Grund steht im Status")

        det_off = AutoDetector(table.ball_radius_px, CoralClient(port=1))
        det_off.detect(warped)
        check(det_off.active == "opencv" and "läuft nicht" in det_off.client.error, "Coral-Container nicht erreichbar -> OpenCV")
        srv.shutdown()

    img, s, dx, dy = letterbox(np.zeros((400, 800, 3), np.uint8), 640, 640)
    check((s, dx, dy) == (0.8, 0, 160), "Letterbox 800x400 -> 640x640")


if __name__ == "__main__":
    test_training_data()
    test_coral()
    print()
    if FAILS:
        print(f"{len(FAILS)} FEHLER")
        sys.exit(1)
    print("ALLES OK")
