"""
Sammelt Trainingsbilder fuer ein spaeteres Coral-Modell.

Gespeichert wird die entzerrte Draufsicht in ruhigen Momenten plus eine Beschriftung
im YOLO-Format (Klasse x_mitte y_mitte breite hoehe, alles 0..1). Die Beschriftung
kommt vom OpenCV-Detektor - sie ist also NICHT perfekt und muss vor dem Training
stichprobenartig geprueft werden (z. B. mit labelImg oder CVAT).
"""

import json
import os
import time

import cv2

CLASSES = ["cue", "eight", "solid", "stripe"]


class Recorder:
    def __init__(self, base_dir, max_images=5000, min_interval_s=4.0):
        self.img_dir = os.path.join(base_dir, "images")
        self.lbl_dir = os.path.join(base_dir, "labels")
        self.max_images = max_images
        self.min_interval_s = min_interval_s
        self.enabled = False
        self._last = 0.0
        self.count = 0
        if os.path.isdir(self.img_dir):
            self.count = len(os.listdir(self.img_dir))
        self.corrected = self.count_corrected(os.path.join(base_dir, "korrigiert"))

    def save_labeled(self, warped, balls, numbers=None, subdir="korrigiert"):
        """Von Hand gepruefte Beschriftung speichern (Admin-Portal). Diese Bilder sind
        fuer das Training wertvoller als die automatischen und landen getrennt."""
        base = os.path.join(os.path.dirname(self.img_dir), subdir)
        img_dir, lbl_dir = os.path.join(base, "images"), os.path.join(base, "labels")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(lbl_dir, exist_ok=True)
        now = time.time()
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(now * 1000) % 1000:03d}"
        h, w = warped.shape[:2]
        cv2.imwrite(os.path.join(img_dir, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
        with open(os.path.join(lbl_dir, name + ".txt"), "w") as f:
            for b in balls:
                if b.kind in CLASSES:
                    f.write(f"{CLASSES.index(b.kind)} {b.x / w:.5f} {b.y / h:.5f} "
                            f"{2 * b.r / w:.5f} {2 * b.r / h:.5f}\n")
        if numbers is not None:
            # Kugelnummern zusaetzlich (fuer ein spaeteres Modell mit 16 Klassen)
            with open(os.path.join(lbl_dir, name + ".json"), "w") as f:
                json.dump([{"x": b.x, "y": b.y, "kind": b.kind, "number": n} for b, n in zip(balls, numbers)], f)
        self.corrected = self.count_corrected(base)
        return name

    @staticmethod
    def count_corrected(base):
        try:
            return len(os.listdir(os.path.join(base, "images")))
        except OSError:
            return 0

    def save(self, warped, balls):
        if not self.enabled or self.count >= self.max_images or not balls:
            return
        now = time.time()
        if now - self._last < self.min_interval_s:
            return
        self._last = now
        os.makedirs(self.img_dir, exist_ok=True)
        os.makedirs(self.lbl_dir, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(now * 1000) % 1000:03d}"
        h, w = warped.shape[:2]
        cv2.imwrite(os.path.join(self.img_dir, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
        with open(os.path.join(self.lbl_dir, name + ".txt"), "w") as f:
            for b in balls:
                if b.kind in CLASSES:
                    f.write(f"{CLASSES.index(b.kind)} {b.x / w:.5f} {b.y / h:.5f} "
                            f"{2 * b.r / w:.5f} {2 * b.r / h:.5f}\n")
        self.count += 1
