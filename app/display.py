"""
Anzeige auf dem HDMI-Ausgang (Beamer/Bildschirm): Kamerabild mit erkannten Kugeln
links, Scoreboard rechts. Laeuft im Hauptthread (pygame verlangt das).
"""

import os
import time

import cv2
import pygame

from geometry import WARP_W

KIND_COLORS = {
    "cue": (255, 255, 255),
    "eight": (150, 150, 150),
    "solid": (255, 190, 0),
    "stripe": (0, 200, 255),
}
BG = (14, 18, 24)
PANEL = (24, 30, 40)
TEXT = (235, 238, 242)
MUTED = (150, 160, 175)
ACCENT = (80, 220, 120)
WARN = (255, 170, 60)


def wrap(font, text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.size(test)[0] <= width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_rect(src_w, src_h, dst_w, dst_h):
    scale = min(dst_w / src_w, dst_h / src_h)
    w, h = int(src_w * scale), int(src_h * scale)
    return (dst_w - w) // 2, (dst_h - h) // 2, w, h, scale


class Display:
    def __init__(self, windowed=False, admin_url=""):
        if not windowed and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        self.windowed = windowed
        self.admin_url = admin_url
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
        self._fonts = {}
        self._cam_surface = None
        self._cam_id = -1

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self._fonts:
            f = pygame.font.Font(None, size)
            f.set_bold(bold)
            self._fonts[key] = f
        return self._fonts[key]

    def text(self, s, size, pos, color=TEXT, bold=False):
        surf = self.font(size, bold).render(s, True, color)
        self.screen.blit(surf, pos)
        return surf.get_size()

    # ----------------------------------------------------------------- Zeichnen
    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q):
                return False
        return True

    def draw(self, frame, frame_id, pipeline, reader, gyro_guidance=None):
        sw, sh = self.screen.get_size()
        pw = max(360, int(sw * 0.30))
        cw = sw - pw
        self.screen.fill(BG)

        # --- Kamerabild
        if frame is not None:
            fh, fw = frame.shape[:2]
            ox, oy, w, h, scale = fit_rect(fw, fh, cw, sh)
            if frame_id != self._cam_id:
                img = cv2.cvtColor(cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
                self._cam_surface = pygame.image.frombuffer(img.tobytes(), (w, h), "RGB")
                self._cam_id = frame_id
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
                self._draw_guidance(ox, oy, scale, table, gyro_guidance, rad)
        else:
            msg = "Warte auf Kamera ..."
            if reader.last_error:
                msg += f" ({reader.last_error})"
            for i, line in enumerate(wrap(self.font(42), msg, cw - 80)):
                self.text(line, 42, (40, sh // 2 - 30 + i * 44), WARN)

        # --- Scoreboard
        self._draw_panel(cw, pw, sh, pipeline, reader, gyro_guidance)
        pygame.display.flip()

    def _draw_guidance(self, ox, oy, scale, table, guidance, radius):
        if not guidance or not guidance.get("available"):
            return
        cue = guidance.get("cue")
        target = guidance.get("target")
        if not cue or not target:
            return
        cue_cam = table.to_camera([cue])[0]
        target_cam = table.to_camera([target])[0]
        c0 = (int(ox + cue_cam[0] * scale), int(oy + cue_cam[1] * scale))
        t0 = (int(ox + target_cam[0] * scale), int(oy + target_cam[1] * scale))
        pygame.draw.line(self.screen, (60, 60, 60), c0, t0, max(2, radius // 2))
        actual = guidance.get("actual_angle_deg")
        if actual is not None:
            import math
            rlen = max(40, int(radius * 8))
            ang = math.radians(actual)
            ax = int(c0[0] + math.cos(ang) * rlen)
            ay = int(c0[1] + math.sin(ang) * rlen)
            pygame.draw.line(self.screen, (230, 230, 230), c0, (ax, ay), max(2, radius // 2))
        if guidance.get("aligned"):
            self.text("✓", 180, (c0[0] - 52, c0[1] - 82), ACCENT, bold=True)

    def _draw_panel(self, x0, pw, sh, pipeline, reader, gyro_guidance=None):
        pygame.draw.rect(self.screen, PANEL, (x0, 0, pw, sh))
        game = pipeline.game
        g = game.to_dict()
        pad = 24
        y = pad
        self.text("8-BALL", 64, (x0 + pad, y), TEXT, bold=True)
        y += 70

        if g["phase"] == "idle":
            for line in wrap(self.font(40), "Kein Spiel aktiv. Starten im Admin-Portal.", pw - 2 * pad):
                self.text(line, 40, (x0 + pad, y), MUTED)
                y += 42
        else:
            box_h = int(sh * 0.22)
            for i in (0, 1):
                active = g["phase"] == "playing" and g["turn"] == i
                winner = g["phase"] == "finished" and g["winner"] == i
                rect = pygame.Rect(x0 + pad, y, pw - 2 * pad, box_h)
                pygame.draw.rect(self.screen, (34, 44, 58), rect, border_radius=14)
                if active or winner:
                    pygame.draw.rect(self.screen, ACCENT, rect, 5, border_radius=14)
                ix, iy = rect.x + 18, rect.y + 14
                self.text(g["teams"][i][:18], 52, (ix, iy), TEXT, bold=True)
                score = str(g["frames_won"][i])
                sw_, _ = self.font(80, True).size(score)
                self.text(score, 80, (rect.right - sw_ - 18, iy - 4), TEXT, bold=True)
                grp = g["groups"][i]
                label = g["group_names"][i]
                if grp:
                    label += f"  noch {g['remaining'][grp]}"
                self.text(label, 38, (ix, iy + 56), MUTED)
                if grp:
                    self._ball_icons(ix, iy + 100, grp, g["remaining"][grp])
                if active:
                    tag = "AM ZUG" + ("  -  Ball in der Hand" if g["ball_in_hand"] else "")
                    self.text(tag, 34, (ix, rect.bottom - 40), ACCENT, bold=True)
                if winner:
                    self.text("GEWINNT DAS RACK", 34, (ix, rect.bottom - 40), ACCENT, bold=True)
                y += box_h + 16
            if g["groups"] == [None, None] and g["phase"] == "playing":
                self.text(f"Tisch offen   Volle {g['remaining']['solid']}  Halbe {g['remaining']['stripe']}",
                          32, (x0 + pad, y), MUTED)
                y += 40

        # Letzte Meldung
        y += 10
        for line in wrap(self.font(34), g["message"], pw - 2 * pad)[:5]:
            self.text(line, 34, (x0 + pad, y), TEXT)
            y += 36

        # Fusszeile: Systemzustand
        status = pipeline.status_text()
        col = ACCENT if status == "Bereit" else WARN
        fy = sh - 3 * 32 - pad
        self.text(status, 32, (x0 + pad, fy), col)
        self.text(f"Kamera {reader.fps:4.1f} fps", 28, (x0 + pad, fy + 34), MUTED)
        if self.admin_url:
            self.text(f"Admin: {self.admin_url}", 28, (x0 + pad, fy + 64), MUTED)
        if gyro_guidance and gyro_guidance.get("available"):
            gy = fy - 34
            if gyro_guidance.get("actual_angle_deg") is None:
                self.text("Gyro: kein Winkel", 28, (x0 + pad, gy), WARN)
            elif gyro_guidance.get("aligned"):
                self.text("Gyro: Ausrichtung passt", 28, (x0 + pad, gy), ACCENT)
            else:
                err = abs(float(gyro_guidance.get("angle_error_deg", 0.0)))
                self.text(f"Gyro: Abweichung {err:.1f}°", 28, (x0 + pad, gy), WARN)

    def _ball_icons(self, x, y, group, n):
        r = 11
        color = KIND_COLORS[group]
        for k in range(n):
            cx = x + r + k * (2 * r + 6)
            if group == "solid":
                pygame.draw.circle(self.screen, color, (cx, y + r), r)
            else:
                pygame.draw.circle(self.screen, (240, 240, 240), (cx, y + r), r)
                pygame.draw.rect(self.screen, color, (cx - r, y + r - 5, 2 * r, 10))

    def close(self):
        pygame.quit()
