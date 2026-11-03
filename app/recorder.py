"""
Sammelt Trainingsbilder fuer ein spaeteres Coral-Modell.

Gespeichert wird die entzerrte Draufsicht in ruhigen Momenten plus eine Beschriftung
im YOLO-Format (Klasse x_mitte y_mitte breite hoehe, alles 0..1). Die Beschriftung
kommt vom OpenCV-Detektor - sie ist also NICHT perfekt und muss vor dem Training
stichprobenartig geprueft werden (z. B. mit labelImg oder CVAT).
"""

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

    def _next_name(self):
        now = time.time()
        return now, time.strftime("%Y%m%d-%H%M%S") + f"-{int(now * 1000) % 1000:03d}"

    def _write_sample(self, warped, balls, name):
        h, w = warped.shape[:2]
        cv2.imwrite(os.path.join(self.img_dir, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
        with open(os.path.join(self.lbl_dir, name + ".txt"), "w") as f:
            for b in balls:
                if b.kind in CLASSES:
                    f.write(f"{CLASSES.index(b.kind)} {b.x / w:.5f} {b.y / h:.5f} "
                            f"{2 * b.r / w:.5f} {2 * b.r / h:.5f}\n")
        self.count += 1

    def save(self, warped, balls):
        if not self.enabled or self.count >= self.max_images or not balls:
            return
        now, name = self._next_name()
        if now - self._last < self.min_interval_s:
            return
        self._last = now
        os.makedirs(self.img_dir, exist_ok=True)
        os.makedirs(self.lbl_dir, exist_ok=True)
        self._write_sample(warped, balls, name)

    def save_manual(self, warped, balls, note=""):
        if self.count >= self.max_images:
            return False
        _, name = self._next_name()
        os.makedirs(self.img_dir, exist_ok=True)
        os.makedirs(self.lbl_dir, exist_ok=True)
        self._write_sample(warped, balls, name)
        note = (note or "").strip()
        if note:
            notes_dir = os.path.join(os.path.dirname(self.img_dir), "notes")
            os.makedirs(notes_dir, exist_ok=True)
            with open(os.path.join(notes_dir, name + ".txt"), "w") as f:
                f.write(note[:4000] + "\n")
        return True
