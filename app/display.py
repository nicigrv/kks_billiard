"""
Anzeige auf dem HDMI-Ausgang (Beamer/Bildschirm): Kamerabild mit erkannten Kugeln
links, Scoreboard rechts. Laeuft im Hauptthread (pygame verlangt das).
"""

import os
import time

import cv2
import pygame

from geometry import WARP_H, WARP_W
from ui_state import game_bar_state

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

    def draw(self, frame, frame_id, pipeline, reader):
        sw, sh = self.screen.get_size()
        bar_h = max(110, int(sh * 0.16))
        top_h = max(240, sh - bar_h)
        pw = max(360, int(sw * 0.30))
        cw = sw - pw
        self.screen.fill(BG)

        # --- Kamerabild
        if frame is not None:
            fh, fw = frame.shape[:2]
            ox, oy, w, h, scale = fit_rect(fw, fh, cw, top_h)
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
        else:
            msg = "Warte auf Kamera ..."
            if reader.last_error:
                msg += f" ({reader.last_error})"
            for i, line in enumerate(wrap(self.font(42), msg, cw - 80)):
                self.text(line, 42, (40, top_h // 2 - 30 + i * 44), WARN)

        # --- Scoreboard
        self._draw_panel(cw, pw, top_h, pipeline, reader)
        self._draw_game_bar(sw, top_h, bar_h, pipeline)
        pygame.display.flip()

    def _draw_panel(self, x0, pw, sh, pipeline, reader):
        pygame.draw.rect(self.screen, PANEL, (x0, 0, pw, sh))
        game = pipeline.game
        g = game.to_dict()
        pad = 24
        y = pad
        self.text("8-BALL", 56, (x0 + pad, y), TEXT, bold=True)
        y += 60

        with pipeline.lock:
            table, balls = pipeline.table, list(pipeline.balls)
        anim_h = int(sh * 0.28)
        self._draw_animation(x0 + pad, y, pw - 2 * pad, anim_h, table, balls, pipeline.status_text())
        y += anim_h + 14

        if g["phase"] == "idle":
            for line in wrap(self.font(34), "Kein Spiel aktiv. Starten im Admin-Portal.", pw - 2 * pad):
                self.text(line, 34, (x0 + pad, y), MUTED)
                y += 36
        else:
            box_h = max(88, int(sh * 0.18))
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
                    self.text(tag, 28, (ix, rect.bottom - 34), ACCENT, bold=True)
                if winner:
                    self.text("GEWINNT DAS RACK", 28, (ix, rect.bottom - 34), ACCENT, bold=True)
                y += box_h + 16
            if g["groups"] == [None, None] and g["phase"] == "playing":
                self.text(f"Tisch offen   Volle {g['remaining']['solid']}  Halbe {g['remaining']['stripe']}", 28,
                          (x0 + pad, y), MUTED)
                y += 32

        # Letzte Meldung
        y += 10
        for line in wrap(self.font(30), g["message"], pw - 2 * pad)[:4]:
            self.text(line, 30, (x0 + pad, y), TEXT)
            y += 32

        # Fusszeile: Systemzustand
        status = pipeline.status_text()
        col = ACCENT if status == "Bereit" else WARN
        fy = sh - 3 * 28 - pad
        self.text(status, 28, (x0 + pad, fy), col)
        self.text(f"Kamera {reader.fps:4.1f} fps", 24, (x0 + pad, fy + 30), MUTED)
        if self.admin_url:
            self.text(f"Admin: {self.admin_url}", 24, (x0 + pad, fy + 58), MUTED)

    def _draw_animation(self, x, y, w, h, table, balls, status):
        rect = pygame.Rect(x, y, w, h)
        pygame.draw.rect(self.screen, (18, 60, 34), rect, border_radius=14)
        pygame.draw.rect(self.screen, (95, 140, 100), rect, 2, border_radius=14)
        self.text("Tisch-Animation", 28, (x + 14, y + 8), TEXT, bold=True)
        self.text("Koe-Position folgt", 22, (x + 14, y + h - 30), MUTED)
        if not table.calibrated:
            self.text("Nicht kalibriert", 28, (x + 14, y + h // 2 - 10), WARN, bold=True)
            return
        if not balls:
            self.text(status, 24, (x + 14, y + h // 2 - 10), MUTED)
            return
        pad = 16
        tx0, ty0 = x + pad, y + 38
        tw, th = w - 2 * pad, h - 52
        pygame.draw.rect(self.screen, (34, 110, 48), (tx0, ty0, tw, th), border_radius=12)
        pygame.draw.rect(self.screen, (160, 130, 70), (tx0 - 3, ty0 - 3, tw + 6, th + 6), 3, border_radius=12)
        rad = max(7, int(table.ball_radius_px / WARP_W * tw * 1.4))
        for b in balls:
            cx = int(tx0 + (b.x / WARP_W) * tw)
            cy = int(ty0 + (b.y / WARP_H) * th)
            if b.kind == "stripe":
                pygame.draw.circle(self.screen, (240, 240, 240), (cx, cy), rad)
                pygame.draw.rect(self.screen, KIND_COLORS[b.kind], (cx - rad, cy - 4, 2 * rad, 8))
            else:
                pygame.draw.circle(self.screen, KIND_COLORS.get(b.kind, TEXT), (cx, cy), rad)

    def _draw_ball_token(self, x, y, r, number, color, active=True, stripe=False):
        if stripe:
            pygame.draw.circle(self.screen, (245, 245, 245), (x, y), r)
            pygame.draw.rect(self.screen, color, (x - r, y - max(3, r // 3), 2 * r, 2 * max(3, r // 3)))
        else:
            pygame.draw.circle(self.screen, color, (x, y), r)
        text_col = (10, 10, 10) if active else MUTED
        n = str(number)
        tw, th = self.font(18, True).size(n)
        self.text(n, 18, (x - tw // 2, y - th // 2), text_col, bold=True)
        if not active:
            pygame.draw.line(self.screen, MUTED, (x - r - 2, y), (x + r + 2, y), 3)

    def _draw_game_bar(self, sw, y0, h, pipeline):
        pygame.draw.rect(self.screen, (18, 22, 30), (0, y0, sw, h))
        with pipeline.lock:
            state = game_bar_state(pipeline.game.to_dict())
        pad = 18
        self.text("Game Bar", 34, (pad, y0 + 8), TEXT, bold=True)
        self.text(f"Volle Punkte: {state['solid_points']}   Halbe Punkte: {state['stripe_points']}", 24,
                  (pad + 160, y0 + 14), MUTED)

        cx = pad
        y = y0 + h - 34
        r = 13
        self.text("Volle", 24, (cx, y - 10), KIND_COLORS["solid"], bold=True)
        cx += 70
        for b in state["solids"]:
            self._draw_ball_token(cx, y, r, b["number"], KIND_COLORS["solid"], active=b["active"])
            cx += 2 * r + 10

        self.text("8", 24, (cx + 8, y - 10), KIND_COLORS["eight"], bold=True)
        cx += 30
        self._draw_ball_token(cx, y, r, 8, KIND_COLORS["eight"], active=state["eight_on_table"])
        cx += 40

        self.text("Halbe", 24, (cx, y - 10), KIND_COLORS["stripe"], bold=True)
        cx += 90
        for b in state["stripes"]:
            self._draw_ball_token(cx, y, r, b["number"], KIND_COLORS["stripe"], active=b["active"], stripe=True)
            cx += 2 * r + 10

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
