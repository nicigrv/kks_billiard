"""
Sammelt Trainingsbilder fuer ein spaeteres Coral-Modell.

Gespeichert wird die entzerrte Draufsicht in ruhigen Momenten plus eine Beschriftung
im YOLO-Format (Klasse x_mitte y_mitte breite hoehe, alles 0..1). Die Beschriftung
kommt vom OpenCV-Detektor - sie ist also NICHT perfekt und muss vor dem Training
stichprobenartig geprueft werden (z. B. mit labelImg oder CVAT).
"""

import os
import time
import shutil
from datetime import datetime

import cv2
import numpy as np

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


class RoundRecorder:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.temp_dir = os.path.join(base_dir, "current")
        self.video_dir = os.path.join(base_dir, "videos")
        self.enabled = False
        self.frame_count = 0
        self.last_video = None

    def reset_round(self):
        self.frame_count = 0
        if os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def save_shot(self, state):
        if not self.enabled:
            return
        os.makedirs(self.temp_dir, exist_ok=True)
        img = self._render_state(state)
        cv2.imwrite(os.path.join(self.temp_dir, f"{self.frame_count:03d}.png"), img, [cv2.IMWRITE_PNG_COMPRESSION, 2])
        self.frame_count += 1

    def finish_round(self):
        if not self.enabled or self.frame_count <= 0:
            return None
        frames = sorted(f for f in os.listdir(self.temp_dir) if f.endswith(".png"))
        if not frames:
            return None
        os.makedirs(self.video_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.video_dir, f"runde-{stamp}.mp4")
        first = cv2.imread(os.path.join(self.temp_dir, frames[0]))
        h, w = first.shape[:2]
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 1.5, (w, h))
        for name in frames:
            frame = cv2.imread(os.path.join(self.temp_dir, name))
            if frame is not None:
                writer.write(frame)
        for _ in range(3):
            writer.write(first)
        writer.release()
        self.last_video = path
        self.reset_round()
        return path

    def latest_video(self):
        if self.last_video and os.path.isfile(self.last_video):
            return self.last_video
        return None

    @staticmethod
    def _render_state(state):
        h, w = 720, 1280
        img = np.full((h, w, 3), (17, 23, 30), dtype=np.uint8)
        panel = (35, 43, 57)
        text = (238, 238, 238)
        mute = (150, 165, 178)
        accent = (95, 225, 130)

        game = state["game"]
        cv2.putText(img, "RUNDENAUFZEICHNUNG", (40, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.1, text, 2, cv2.LINE_AA)
        cv2.putText(img, f"Status: {game['phase']}   Zug: {game['teams'][game['turn']] if game['phase'] == 'playing' else '-'}",
                    (40, 118), cv2.FONT_HERSHEY_SIMPLEX, 0.9, mute, 2, cv2.LINE_AA)
        cv2.rectangle(img, (40, 150), (1240, 680), panel, -1)

        for i in (0, 1):
            x0 = 70 + i * 590
            cv2.rectangle(img, (x0, 190), (x0 + 550, 440), (42, 53, 68), -1)
            if game["phase"] == "playing" and game["turn"] == i:
                cv2.rectangle(img, (x0, 190), (x0 + 550, 440), accent, 3)
            cv2.putText(img, game["teams"][i][:20], (x0 + 22, 248), cv2.FONT_HERSHEY_SIMPLEX, 1.0, text, 2, cv2.LINE_AA)
            cv2.putText(img, f"Frames: {game['frames_won'][i]}", (x0 + 22, 300), cv2.FONT_HERSHEY_SIMPLEX, 0.9, text, 2, cv2.LINE_AA)
            grp = game["groups"][i]
            grp_name = game["group_names"][i]
            if grp is not None:
                grp_name += f" / Rest: {game['remaining'][grp]}"
            cv2.putText(img, grp_name, (x0 + 22, 352), cv2.FONT_HERSHEY_SIMPLEX, 0.8, mute, 2, cv2.LINE_AA)
            if game["phase"] == "finished" and game["winner"] == i:
                cv2.putText(img, "GEWINNER", (x0 + 22, 405), cv2.FONT_HERSHEY_SIMPLEX, 0.95, accent, 2, cv2.LINE_AA)

        cv2.putText(img, "Meldung:", (70, 505), cv2.FONT_HERSHEY_SIMPLEX, 0.9, mute, 2, cv2.LINE_AA)
        msg = game["message"][:140]
        cv2.putText(img, msg, (70, 550), cv2.FONT_HERSHEY_SIMPLEX, 0.85, text, 2, cv2.LINE_AA)

        cv2.putText(img, "Hinweis: Es werden keine Kamerabilder gespeichert.",
                    (70, 640), cv2.FONT_HERSHEY_SIMPLEX, 0.8, accent, 2, cv2.LINE_AA)
        return img
