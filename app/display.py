"""
Anzeige auf dem HDMI-Ausgang (Beamer/Bildschirm). Laeuft im Hauptthread (pygame verlangt das).

    +---------------------------+---------------------------+
    |  Live-Kamerabild mit      |  Grafik: gezeichneter     |
    |  Kreisen um die Kugeln    |  Tisch, neu nach jedem    |
    |                           |  Stoss, Queue-Vorschlag   |
    +---------------------------+---------------------------+
    |  Game-Bar: Teams, Frames, Punkte, alle 15 Kugeln      |
    +-------------------------------------------------------+

Liegt der Queue richtig (Gyroskop), erscheint ein grosser gruener Haken.
"""

import os
import time

import cv2
import pygame

from geometry import WARP_W
from scene import ACCENT, BG, RENDER_LOCK, TEXT, WARN, SceneRenderer, wrap

KIND_COLORS = {
    "cue": (255, 255, 255),
    "eight": (150, 150, 150),
    "solid": (255, 190, 0),
    "stripe": (0, 200, 255),
}


def fit_rect(src_w, src_h, dst_w, dst_h):
    scale = min(dst_w / src_w, dst_h / src_h)
    w, h = int(src_w * scale), int(src_h * scale)
    return (dst_w - w) // 2, (dst_h - h) // 2, w, h, scale


class Display:
    def __init__(self, windowed=False):
        if not windowed and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        self.windowed = windowed
        self.screen = None

    def open(self):
        """Wartet, bis ein Bildschirm verfuegbar ist (z. B. Beamer noch aus)."""
        attempt = 0
        while True:
            try:
                pygame.display.init()
                break
            except pygame.error as e:
                if attempt % 12 == 0:
                    print(f"[display] keine HDMI-Ausgabe ({e}), versuche weiter ...", flush=True)
                attempt += 1
                pygame.display.quit()
                time.sleep(5)
        pygame.font.init()
        if self.windowed:
            self.screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
        else:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.display.set_caption("Billard")
        pygame.mouse.set_visible(False)
        self.renderer = SceneRenderer()
        self._cam_surface = None
        self._cam_key = None

    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q):
                return False
        return True

    # ----------------------------------------------------------------- Zeichnen
    def draw(self, frame, frame_id, pipeline, reader, scene):
        with RENDER_LOCK:
            self._draw(frame, frame_id, pipeline, reader, scene)
        pygame.display.flip()

    def _draw(self, frame, frame_id, pipeline, reader, scene):
        sw, sh = self.screen.get_size()
        bar_h = max(90, int(sh * 0.17))
        top_h = sh - bar_h
        half = sw // 2
        self.screen.fill(BG)

        self._draw_camera(pygame.Rect(0, 0, half, top_h), frame, frame_id, pipeline, reader)
        side = pygame.Rect(half, 0, sw - half, top_h)
        self.renderer.draw_side(self.screen, side, scene)
        self.renderer.draw_bar(self.screen, pygame.Rect(0, top_h, sw, bar_h), scene)
        pygame.draw.line(self.screen, (40, 50, 64), (half, 0), (half, top_h), 2)

        cue = scene.get("cue") or {}
        if cue.get("ok"):
            self.renderer.draw_check(self.screen, pygame.Rect(0, 0, sw, top_h))

    def _draw_camera(self, rect, frame, frame_id, pipeline, reader):
        """Linke Haelfte: Live-Kamerabild mit Kreisen um die erkannten Kugeln."""
        if frame is None:
            msg = "Warte auf Kamera ..."
            if reader.last_error:
                msg += f" ({reader.last_error})"
            fs = max(20, rect.h // 18)
            for i, line in enumerate(wrap(self.renderer.font(fs), msg, rect.w - 60)):
                self.renderer.text(self.screen, line, fs, (rect.x + 30, rect.centery - fs + i * fs), WARN)
            return
        fh, fw = frame.shape[:2]
        ox, oy, w, h, scale = fit_rect(fw, fh, rect.w, rect.h)
        ox += rect.x
        oy += rect.y
        key = (frame_id, w, h)
        if key != self._cam_key:
            img = cv2.cvtColor(cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
            self._cam_surface = pygame.image.frombuffer(img.tobytes(), (w, h), "RGB")
            self._cam_key = key
        self.screen.blit(self._cam_surface, (ox, oy))
        with pipeline.lock:
            table, balls = pipeline.table, list(pipeline.balls)
        if table.calibrated:
            corners = [(ox + x * scale, oy + y * scale) for x, y in table.corners]
            pygame.draw.lines(self.screen, ACCENT, True, corners, 2)
            pts = table.to_camera([(b.x, b.y) for b in balls])
            (x0c, y0c), (x1c, y1c) = table.corners[0], table.corners[1]
            cam_len = ((x1c - x0c) ** 2 + (y1c - y0c) ** 2) ** 0.5   # lange Tischseite im Kamerabild
            rad = max(4, int(table.ball_radius_px / WARP_W * cam_len * scale * 1.2))
            for b, (x, y) in zip(balls, pts):
                pygame.draw.circle(self.screen, KIND_COLORS.get(b.kind, TEXT),
                                   (int(ox + x * scale), int(oy + y * scale)), rad, 2)

    def close(self):
        pygame.quit()
