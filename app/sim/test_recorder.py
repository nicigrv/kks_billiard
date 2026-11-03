"""
Tests fuer Recorder:
    python3 -m sim.test_recorder
"""

import glob
import os
import sys
import tempfile
import types

if "cv2" not in sys.modules:
    fake_cv2 = types.SimpleNamespace(IMWRITE_JPEG_QUALITY=1, imwrite=lambda *a, **k: True)
    sys.modules["cv2"] = fake_cv2

import recorder as recorder_module
from recorder import Recorder

FAILS = []


def check(cond, text):
    print(("  OK   " if cond else "  FEHL ") + text)
    if not cond:
        FAILS.append(text)


def test_manual_recording():
    with tempfile.TemporaryDirectory(prefix="billard-rec-", dir="/tmp") as tmp:
        rec = Recorder(os.path.join(tmp, "dataset"), max_images=10, min_interval_s=0.0)
        class FakeWarped:
            shape = (120, 200, 3)

        warped = FakeWarped()
        class Ball:
            def __init__(self, x, y, r, kind):
                self.x = x
                self.y = y
                self.r = r
                self.kind = kind

        balls = [Ball(100, 60, 8, "solid"), Ball(120, 62, 8, "stripe")]
        old_imwrite = recorder_module.cv2.imwrite
        recorder_module.cv2.imwrite = lambda *args, **kwargs: True

        try:
            rec.save(warped, balls)
            check(rec.count == 0, "automatisches Speichern bleibt aus, wenn Recorder deaktiviert ist")

            ok = rec.save_manual(warped, balls, "Volle/Halbe vertauscht")
            check(ok, "manuelles Speichern liefert Erfolg")
            check(rec.count == 1, "manuelles Speichern erhoeht den Zaehler")

            imgs = glob.glob(os.path.join(tmp, "dataset", "images", "*.jpg"))
            labels = glob.glob(os.path.join(tmp, "dataset", "labels", "*.txt"))
            notes = glob.glob(os.path.join(tmp, "dataset", "notes", "*.txt"))
            check(len(imgs) == 0 and len(labels) == 1, "Labeldatei wurde geschrieben")
            check(len(notes) == 1, "Notiz zur Falscherkennung wurde geschrieben")
        finally:
            recorder_module.cv2.imwrite = old_imwrite


if __name__ == "__main__":
    test_manual_recording()
    print(f"\n{'ALLES OK' if not FAILS else f'{len(FAILS)} FEHLER'}")
    sys.exit(1 if FAILS else 0)
