"""
Grafische Ansicht: gezeichneter Tisch mit den aktuellen Kugeln (rechte Haelfte auf dem
Beamer), Game-Bar (unterer Rand) und das grosse gruene Haken-Symbol.

Gezeichnet wird mit pygame auf beliebige Surfaces, auch ganz ohne Bildschirm. Dieselbe
Darstellung nutzen:
  * der Beamer (display.py),
  * das Admin-Portal auf dem iPad (/api/graphic.jpg),
  * die Spielaufzeichnung (replay.py).

Eingabe ist ein "Szene"-dict (App.scene() in main.py):
    balls       [{x, y, kind, color, number}]  Draufsicht-Koordinaten (geometry.WARP_W x WARP_H)
    radius      Kugelradius in Draufsicht-Pixeln
    game        Game.to_dict()
    bar         {Nummer: True wenn noch auf dem Tisch} fuer 1..15
    suggestion  Stossvorschlag (planner.py) oder None
    cue         {"angle": Grad oder None, "ok": bool, "err": Grad} oder None   (Gyroskop)
    status, info  kurze Texte fuer die Fusszeile
"""

import math
import threading

import numpy as np
import pygame

from ballnum import SOLIDS, STRIPES
from geometry import WARP_H, WARP_W

# pygame/SDL_ttf ist nicht fuer gleichzeitiges Zeichnen aus mehreren Threads gedacht
RENDER_LOCK = threading.RLock()

BG = (14, 18, 24)
PANEL = (24, 30, 40)
PANEL_LIGHT = (34, 44, 58)
TEXT = (235, 238, 242)
MUTED = (150, 160, 175)
ACCENT = (80, 220, 120)
WARN = (255, 170, 60)
FOUL = (220, 70, 60)
FELT = (18, 98, 72)
FELT_LINE = (40, 125, 96)
RAIL = (92, 54, 32)
RAIL_EDGE = (128, 80, 48)
POCKET = (6, 8, 8)
DIAMOND = (235, 225, 200)

NUMBER_COLORS = {1: (245, 195, 20), 2: (35, 85, 205), 3: (215, 45, 40), 4: (120, 55, 160),
                 5: (245, 125, 25), 6: (25, 140, 65), 7: (135, 30, 45), 8: (18, 18, 18)}
UNKNOWN_COLORS = {"solid": (215, 170, 50), "stripe": (70, 150, 225)}
CUE_WHITE = (246, 244, 236)

GROUP_NAMES = {"solid": "Volle", "stripe": "Halbe"}


# --------------------------------------------------------------------- Logik
def bar_balls(game, visible, seen):
    """Welche Kugeln zeigt die Game-Bar noch als 'im Spiel'?

    Massgeblich ist der Spielstand (game['remaining']), denn der ist korrigierbar.
    Welche NUMMERN durchgestrichen werden, kommt aus der Kamera: zuerst Kugeln, die
    nicht mehr zu sehen sind, aber in diesem Rack schon gesehen wurden."""
    out = {}
    playing = game.get("phase") in ("playing", "finished")
    for group, nums in (("solid", SOLIDS), ("stripe", STRIPES)):
        n_off = 7 - game["remaining"][group] if playing else 0
        order = sorted(nums, key=lambda n: (n in visible, n not in seen, n))
        off = set(order[:max(0, n_off)])
        for n in nums:
            out[n] = n not in off
    out[8] = bool(game.get("eight_on_table", True)) or not playing
    return out


def team_points(game):
    """Punkte je Mannschaft = versenkte Kugeln der eigenen Gruppe."""
    return [7 - game["remaining"][g] if g else 0 for g in game["groups"]]


def ball_color(number, kind):
    if kind == "cue":
        return CUE_WHITE
    if kind == "eight":
        return NUMBER_COLORS[8]
    if number:
        return NUMBER_COLORS[number if number < 8 else number - 8]
    return UNKNOWN_COLORS.get(kind, (200, 200, 200))


def wrap(font, text, width):
    words, lines, cur = (text or "").split(), [], ""
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


def surface_to_bgr(surf):
    """pygame-Surface -> OpenCV-Bild (BGR, uint8)."""
    arr = pygame.surfarray.array3d(surf)          # (w, h, 3) RGB
    return np.ascontiguousarray(arr.transpose(1, 0, 2)[:, :, ::-1])


# ------------------------------------------------------------------ Zeichnen
class SceneRenderer:
    """Ein Renderer pro Thread (eigene Schrift-Objekte); Zeichnen laeuft unter RENDER_LOCK."""

    def __init__(self):
        with RENDER_LOCK:
            pygame.font.init()
        self._fonts = {}
        self._sprites = {}

    def font(self, size, bold=False):
        size = max(8, int(size))
        key = (size, bold)
        if key not in self._fonts:
            f = pygame.font.Font(None, size)
            f.set_bold(bold)
            self._fonts[key] = f
        return self._fonts[key]

    def text(self, surf, s, size, pos, color=TEXT, bold=False, anchor="topleft"):
        img = self.font(size, bold).render(str(s), True, color)
        r = img.get_rect(**{anchor: (int(pos[0]), int(pos[1]))})
        surf.blit(img, r)
        return r

    # ------------------------------------------------------------- Kugeln
    def ball_sprite(self, number, kind, r):
        r = max(3, int(round(r)))
        key = (number, kind, r)
        if key in self._sprites:
            return self._sprites[key]
        size = 2 * r + 2
        c = (r + 1, r + 1)
        s = pygame.Surface((size, size), pygame.SRCALPHA)
        col = ball_color(number, kind)
        pygame.draw.circle(s, col, c, r)
        if kind == "stripe":
            caps = pygame.Surface((size, size), pygame.SRCALPHA)
            pygame.draw.circle(caps, CUE_WHITE, c, r)
            band = int(r * 0.55)
            caps.fill((0, 0, 0, 0), pygame.Rect(0, c[1] - band, size, 2 * band + 1))
            s.blit(caps, (0, 0))
        if kind != "cue" and r >= 7:
            pygame.draw.circle(s, CUE_WHITE, c, max(3, int(r * 0.52)))
            if number:
                img = self.font(r * 1.05, True).render(str(number), True, (20, 20, 20))
                s.blit(img, img.get_rect(center=c))
        # Glanzlicht + Rand fuer Kontrast auf dem Tuch
        pygame.draw.circle(s, (255, 255, 255, 90), (c[0] - r // 3, c[1] - r // 3), max(1, r // 4))
        pygame.draw.circle(s, (0, 0, 0, 170), c, r, 1)
        if len(self._sprites) > 400:
            self._sprites.clear()
        self._sprites[key] = s
        return s

    def draw_ball(self, surf, center, r, number, kind, alpha=255):
        spr = self.ball_sprite(number, kind, r)
        if alpha < 255:
            spr = spr.copy()
            spr.set_alpha(alpha)
        surf.blit(spr, spr.get_rect(center=(int(center[0]), int(center[1]))))

    # ------------------------------------------------------------- Tisch
    @staticmethod
    def table_layout(rect):
        """Spielflaeche (2:1) mit Bande, mittig in rect. Liefert (Bande, Tuch, Massstab)."""
        rail = 0.065
        felt_w = min(rect.w / (1 + 2 * rail), rect.h / (0.5 + 2 * rail))
        felt_h = felt_w / 2
        rw = felt_w * rail
        outer = pygame.Rect(0, 0, int(felt_w + 2 * rw), int(felt_h + 2 * rw))
        outer.center = rect.center
        felt = pygame.Rect(0, 0, int(felt_w), int(felt_h))
        felt.center = outer.center
        return outer, felt, felt_w / WARP_W

    def draw_table(self, surf, rect, scene, show_live_cue=True):
        outer, felt, s = self.table_layout(rect)
        rw = (outer.w - felt.w) // 2

        def P(x, y):
            return (felt.x + x * s, felt.y + y * s)

        pygame.draw.rect(surf, RAIL, outer, border_radius=max(4, rw // 2))
        pygame.draw.rect(surf, RAIL_EDGE, outer, 2, border_radius=max(4, rw // 2))
        pygame.draw.rect(surf, FELT, felt)
        # Kopflinie + Fusspunkt
        pygame.draw.line(surf, FELT_LINE, P(WARP_W * 0.25, 0), P(WARP_W * 0.25, WARP_H), 1)
        pygame.draw.circle(surf, FELT_LINE, P(WARP_W * 0.75, WARP_H / 2), 3)
        # Diamanten
        for k in range(1, 8):
            if k == 4:
                continue
            x = WARP_W * k / 8
            for yy in (felt.top - rw / 2, felt.bottom + rw / 2):
                pygame.draw.circle(surf, DIAMOND, (int(felt.x + x * s), int(yy)), max(2, rw // 7))
        for k in (1, 2, 3):
            y = WARP_H * k / 4
            for xx in (felt.left - rw / 2, felt.right + rw / 2):
                pygame.draw.circle(surf, DIAMOND, (int(xx), int(felt.y + y * s)), max(2, rw // 7))

        R = float(scene.get("radius") or 10.0)
        pr = int(R * s * 2.0)
        sug = scene.get("suggestion") or {}
        for i, (x, y) in enumerate(((0, 0), (WARP_W / 2, 0), (WARP_W, 0),
                                    (0, WARP_H), (WARP_W / 2, WARP_H), (WARP_W, WARP_H))):
            pygame.draw.circle(surf, POCKET, P(x, y), pr)
            if sug.get("pocket_index") == i:
                pygame.draw.circle(surf, ACCENT, P(x, y), pr + 3, 3)

        balls = scene.get("balls") or []
        cue_ball = next((b for b in balls if b["kind"] == "cue"), None)

        # Vorschlag: Laufwege + Geisterkugel
        if sug.get("type") == "pot":
            self._dashed(surf, P(*sug["cue"]), P(*sug["ghost"]), (230, 230, 230), 2, 10)
            self._dashed(surf, P(*sug["target"]), P(*sug["pocket"]), ACCENT, 3, 12)
            pygame.draw.circle(surf, (230, 230, 230), P(*sug["ghost"]), max(3, int(R * s)), 2)
        elif sug.get("type") == "break":
            self._dashed(surf, P(*sug["cue"]), P(*sug["target"]), (230, 230, 230), 2, 10)

        for b in sorted(balls, key=lambda b: b["kind"] == "cue"):
            self.draw_ball(surf, P(b["x"], b["y"]), R * s * b.get("scale", 1.0),
                           b.get("number"), b["kind"], int(255 * b.get("alpha", 1.0)))

        # Queues: dunkel = optimal, hell = echte Lage (Gyroskop)
        if cue_ball is not None:
            cb = P(cue_ball["x"], cue_ball["y"])
            length = felt.w * 0.5
            if sug.get("angle") is not None:
                self.draw_cue(surf, cb, sug["angle"], R * s, length, dark=True)
            cue = scene.get("cue") or {}
            if show_live_cue and cue.get("angle") is not None and sug.get("angle") is not None:
                self.draw_cue(surf, cb, cue["angle"], R * s, length, dark=False, ok=cue.get("ok"))
        return felt

    @staticmethod
    def _dashed(surf, a, b, color, width, dash):
        ax, ay = a
        bx, by = b
        d = math.hypot(bx - ax, by - ay)
        if d < 1:
            return
        n = int(d // dash)
        for i in range(0, n, 2):
            t0, t1 = i / n, min(1.0, (i + 1) / n)
            pygame.draw.line(surf, color, (ax + (bx - ax) * t0, ay + (by - ay) * t0),
                             (ax + (bx - ax) * t1, ay + (by - ay) * t1), width)

    @staticmethod
    def draw_cue(surf, ball_center, angle_deg, ball_r, length, dark, ok=False):
        """Queue hinter der Weissen, Spitze zeigt in Stossrichtung angle_deg."""
        a = math.radians(angle_deg)
        dx, dy = math.cos(a), math.sin(a)
        nx, ny = -dy, dx
        gap = ball_r * 1.6
        tip = (ball_center[0] - dx * gap, ball_center[1] - dy * gap)
        butt = (tip[0] - dx * length, tip[1] - dy * length)
        w_tip, w_butt = max(3.0, ball_r * 0.45), max(6.0, ball_r * 1.05)
        if dark:
            w_tip, w_butt = w_tip + 4, w_butt + 4

        def quad(p0, p1, w0, w1):
            return [(p0[0] + nx * w0 / 2, p0[1] + ny * w0 / 2), (p0[0] - nx * w0 / 2, p0[1] - ny * w0 / 2),
                    (p1[0] - nx * w1 / 2, p1[1] - ny * w1 / 2), (p1[0] + nx * w1 / 2, p1[1] + ny * w1 / 2)]

        def lerp(t):
            return (tip[0] - dx * length * t, tip[1] - dy * length * t), w_tip + (w_butt - w_tip) * t

        if dark:
            shaft, butt_col, edge = (58, 38, 24), (28, 18, 12), (210, 210, 200)
        else:
            shaft, butt_col, edge = (238, 214, 165), (175, 110, 60), (60, 40, 20)
            if ok:
                edge = ACCENT
        (pm, wm) = lerp(0.62)
        pygame.draw.polygon(surf, shaft, quad(tip, pm, w_tip, wm))
        pygame.draw.polygon(surf, butt_col, quad(pm, butt, wm, w_butt))
        (pf, wf) = lerp(0.025)
        pygame.draw.polygon(surf, (250, 250, 250) if not dark else (150, 150, 150), quad(tip, pf, w_tip, wf))
        pygame.draw.polygon(surf, edge, quad(tip, butt, w_tip, w_butt), 2)

    @staticmethod
    def draw_check(surf, rect):
        """Grosser gruener Haken: Queue liegt richtig."""
        size = int(min(rect.w, rect.h) * 0.42)
        c = rect.center
        disc = pygame.Surface((2 * size + 4, 2 * size + 4), pygame.SRCALPHA)
        pygame.draw.circle(disc, (10, 20, 14, 190), (size + 2, size + 2), size)
        pygame.draw.circle(disc, ACCENT, (size + 2, size + 2), size, max(4, size // 14))
        surf.blit(disc, disc.get_rect(center=c))
        k = size / 100.0
        pts = [(c[0] - 48 * k, c[1] + 2 * k), (c[0] - 14 * k, c[1] + 36 * k), (c[0] + 52 * k, c[1] - 34 * k)]
        w = max(6, int(18 * k))
        pygame.draw.lines(surf, ACCENT, False, pts, w)
        for p in pts:
            pygame.draw.circle(surf, ACCENT, (int(p[0]), int(p[1])), w // 2)

    # ------------------------------------------------- rechte Bildschirmhaelfte
    def headline(self, game):
        if game["phase"] == "idle":
            return "Kein Spiel aktiv", MUTED
        if game["phase"] == "finished":
            w = game.get("winner")
            return (f"{game['teams'][w]} gewinnt das Rack" if w is not None else "Rack beendet"), ACCENT
        t = game["turn"]
        grp = game["groups"][t]
        s = f"Am Zug: {game['teams'][t]}"
        if grp:
            s += f" ({GROUP_NAMES[grp]})"
        if game.get("breaking"):
            s += " - Anstoß"
        elif game.get("ball_in_hand"):
            s += " - Ball in der Hand"
        return s, TEXT

    def draw_side(self, surf, rect, scene, show_live_cue=True):
        """Ueberschrift, Tischgrafik, Meldung/Vorschlag, Systemzeile."""
        game = scene["game"]
        h = rect.h
        pad = max(8, int(h * 0.025))
        head = pygame.Rect(rect.x + pad, rect.y + pad, rect.w - 2 * pad, int(h * 0.09))
        info_h = int(h * 0.24)
        table = pygame.Rect(rect.x + pad, head.bottom + pad // 2, rect.w - 2 * pad,
                            rect.h - head.h - info_h - 2 * pad)
        title, col = self.headline(game)
        old_clip = surf.get_clip()
        fs = int(head.h * 0.85)
        while fs > 14 and self.font(fs, True).size(title)[0] > head.w:
            fs -= 2
        self.text(surf, title, fs, head.midleft, col, bold=True, anchor="midleft")

        surf.set_clip(table.inflate(pad, pad // 2).clip(rect))
        self.draw_table(surf, table, scene, show_live_cue)
        surf.set_clip(old_clip)

        y = table.bottom + pad // 2
        fs = max(14, int(h * 0.045))
        sug = scene.get("suggestion")
        lines = []
        if game.get("message"):
            lines += [(ln, TEXT) for ln in wrap(self.font(fs), game["message"], rect.w - 2 * pad)[:2]]
        if sug and sug.get("text"):
            lines += [(ln, ACCENT) for ln in wrap(self.font(fs), sug["text"], rect.w - 2 * pad)[:2]]
        cue = scene.get("cue")
        if cue and cue.get("angle") is not None and sug and sug.get("angle") is not None and cue.get("err") is not None:
            e = cue["err"]
            if cue.get("ok"):
                lines.append(("Queue passt!", ACCENT))
            else:
                lines.append((f"Queue {abs(e):.1f}° " + ("im Uhrzeigersinn drehen" if e < 0 else
                                                          "gegen den Uhrzeigersinn drehen"), WARN))
        for ln, c in lines[:4]:
            self.text(surf, ln, fs, (rect.x + pad, y), c)
            y += int(fs * 0.95)

        small = max(12, int(h * 0.036))
        fy = rect.bottom - pad - small
        status = scene.get("status") or ""
        r = self.text(surf, status, small, (rect.x + pad, fy), ACCENT if status == "Bereit" else WARN)
        info = scene.get("info") or ""
        if info:
            self.text(surf, info, small, (rect.right - pad, fy), MUTED, anchor="topright")
        return r

    # --------------------------------------------------------------- Game-Bar
    def draw_bar(self, surf, rect, scene):
        """Unterer Rand: Mannschaften, Frames, Punkte und alle 15 Kugeln
        (versenkte durchgestrichen)."""
        game = scene["game"]
        pygame.draw.rect(surf, PANEL, rect)
        pygame.draw.line(surf, PANEL_LIGHT, rect.topleft, rect.topright, 2)
        pad = max(6, int(rect.h * 0.08))
        team_w = int(rect.w * 0.25)
        idle = game["phase"] == "idle"
        pts = team_points(game)

        for i in (0, 1):
            box = pygame.Rect(0, rect.y + pad, team_w - pad, rect.h - 2 * pad)
            if i == 0:
                box.x = rect.x + pad
            else:
                box.right = rect.right - pad
            active = game["phase"] == "playing" and game["turn"] == i
            winner = game["phase"] == "finished" and game.get("winner") == i
            pygame.draw.rect(surf, PANEL_LIGHT, box, border_radius=10)
            if active or winner:
                pygame.draw.rect(surf, ACCENT, box, max(3, rect.h // 40), border_radius=10)
            ip = max(6, box.h // 10)
            name_fs = int(box.h * 0.36)
            name = game["teams"][i]
            while name_fs > 12 and self.font(name_fs, True).size(name)[0] > box.w * 0.62:
                name_fs -= 2
            self.text(surf, name, name_fs, (box.x + ip, box.y + ip), TEXT, bold=True)
            self.text(surf, game["frames_won"][i], int(box.h * 0.62),
                      (box.right - ip, box.centery), TEXT, bold=True, anchor="midright")
            grp = game["groups"][i]
            if idle:
                sub = ""
            elif grp:
                sub = f"{GROUP_NAMES[grp]} · {pts[i]} " + ("Punkt" if pts[i] == 1 else "Punkte")
            else:
                sub = "Tisch offen"
            if active:
                sub = ("AM ZUG  " + sub).strip()
            self.text(surf, sub, int(box.h * 0.25), (box.x + ip, box.bottom - ip), ACCENT if active else MUTED,
                      anchor="bottomleft")

        # Kugelreihe: Gruppe von Mannschaft 1 links, 8 in der Mitte, Gruppe von Mannschaft 2 rechts
        mid = pygame.Rect(rect.x + team_w + pad, rect.y, rect.w - 2 * team_w - 2 * pad, rect.h)
        left, right = SOLIDS, STRIPES
        if game["groups"][0] == "stripe":
            left, right = STRIPES, SOLIDS
        order = list(left) + [8] + list(right)
        gaps = 2
        slot = mid.w / (len(order) + gaps * 0.6)
        r = max(5, int(min(slot * 0.42, rect.h * 0.22)))
        bar = scene.get("bar") or {}
        x = mid.x + slot / 2
        cy = mid.centery - int(rect.h * 0.07)
        for k, n in enumerate(order):
            if k in (7, 8):
                x += slot * 0.3
            on = bar.get(n, True)
            kind = "eight" if n == 8 else ("solid" if n < 8 else "stripe")
            self.draw_ball(surf, (x, cy), r, n, kind, 255 if on else 70)
            if not on:
                d = r * 0.95
                pygame.draw.line(surf, FOUL, (x - d, cy - d), (x + d, cy + d), max(3, r // 4))
                pygame.draw.line(surf, FOUL, (x - d, cy + d), (x + d, cy - d), max(3, r // 4))
            x += slot
        cap_fs = max(12, int(rect.h * 0.17))
        on_l = sum(1 for n in left if bar.get(n, True))
        on_r = sum(1 for n in right if bar.get(n, True))
        lname = GROUP_NAMES["solid" if left == SOLIDS else "stripe"]
        rname = GROUP_NAMES["solid" if right == SOLIDS else "stripe"]
        self.text(surf, f"{lname}: noch {on_l}", cap_fs, (mid.x + slot * 0.1, rect.bottom - pad), MUTED,
                  anchor="bottomleft")
        self.text(surf, f"{rname}: noch {on_r}", cap_fs, (mid.right - slot * 0.1, rect.bottom - pad), MUTED,
                  anchor="bottomright")

    # ---------------------------------------------------- Komplettansicht (iPad/Video)
    def render_view(self, scene, size=(1280, 720), show_live_cue=True):
        """Tischgrafik + Game-Bar ohne Kamerabild (fuer iPad und Aufzeichnung)."""
        with RENDER_LOCK:
            w, h = size
            surf = pygame.Surface(size)
            surf.fill(BG)
            bar_h = int(h * 0.2)
            self.draw_side(surf, pygame.Rect(0, 0, w, h - bar_h), scene, show_live_cue)
            self.draw_bar(surf, pygame.Rect(0, h - bar_h, w, bar_h), scene)
            cue = scene.get("cue") or {}
            if show_live_cue and cue.get("ok"):
                self.draw_check(surf, pygame.Rect(0, 0, w, h - bar_h))
            return surf
