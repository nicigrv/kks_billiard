"""
Zeigt den Stream der ESP32-CAM im Vollbild auf dem HDMI-Ausgang des Raspberry Pi.

Funktioniert mit Desktop (X11/Wayland) UND ohne Desktop (Raspberry Pi OS Lite):
ohne Desktop nutzt pygame/SDL2 direkt KMS/DRM, also den HDMI-Ausgang.

Start:
    python3 viewer.py --url http://192.168.178.50:81/stream
    python3 viewer.py --url ... --windowed     # zum Testen im Fenster

Tasten:  ESC / Q = beenden,  F = FPS-Anzeige an/aus
"""

import argparse
import os
import sys
import time

import cv2
import pygame

from stream_reader import MjpegReader

STALE_AFTER_S = 2.0   # so lange ohne neues Bild -> Warnung anzeigen


def parse_args():
    p = argparse.ArgumentParser(description="ESP32-CAM -> HDMI Viewer")
    p.add_argument("--url", default=os.environ.get("CAM_URL", "http://192.168.178.50:81/stream"))
    p.add_argument("--windowed", action="store_true", help="Fenster statt Vollbild")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                   help="Bild drehen, falls die Kamera anders herum haengt")
    p.add_argument("--no-fps", action="store_true", help="FPS-Anzeige beim Start aus")
    return p.parse_args()


def drm_diagnostics() -> str:
    """Sammelt, was fuer die HDMI-Ausgabe wichtig ist, damit das Log die Ursache zeigt."""
    lines = []
    try:
        devs = sorted(os.listdir("/dev/dri"))
        lines.append(f"/dev/dri: {', '.join(devs) or '(leer)'}")
        for d in devs:
            p = os.path.join("/dev/dri", d)
            if not os.access(p, os.R_OK | os.W_OK):
                lines.append(f"  keine Schreib-/Leserechte auf {p}")
    except FileNotFoundError:
        lines.append("/dev/dri fehlt -> im Compose 'devices: - /dev/dri:/dev/dri' pruefen")
    connected = []
    try:
        for name in sorted(os.listdir("/sys/class/drm")):
            status = os.path.join("/sys/class/drm", name, "status")
            if os.path.isfile(status):
                with open(status) as f:
                    st = f.read().strip()
                lines.append(f"  Anschluss {name}: {st}")
                if st == "connected":
                    connected.append(name)
    except OSError:
        lines.append("  /sys/class/drm nicht lesbar")
    if not connected:
        lines.append("=> KEIN Bildschirm erkannt: HDMI-Kabel pruefen, Bildschirm einschalten "
                     "(Pi 4: den HDMI-Port neben dem USB-C-Strom nehmen).")
    else:
        lines.append("=> Bildschirm erkannt. Wahrscheinlich belegt ein Desktop die Anzeige: "
                     "sudo raspi-config -> System -> Boot -> Console, dann neu starten.")
    return "\n".join(lines)


def setup_display(windowed: bool):
    # Ohne Desktop: SDL direkt auf KMS/DRM (HDMI) rendern lassen.
    if not windowed and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")

    # Bei Fehler nicht abstuerzen (Docker wuerde im Sekundentakt neu starten),
    # sondern Ursache melden und weiter probieren - z. B. bis der Bildschirm an ist.
    attempt = 0
    while True:
        try:
            pygame.display.init()
            break
        except pygame.error as e:
            if attempt == 0:
                print(f"[display] HDMI-Ausgabe nicht moeglich: {e}\n{drm_diagnostics()}", flush=True)
            elif attempt % 12 == 0:
                print(f"[display] immer noch keine Ausgabe ({e}), versuche weiter ...", flush=True)
            attempt += 1
            pygame.display.quit()
            time.sleep(5)
    if attempt:
        print("[display] HDMI-Ausgabe jetzt verfuegbar.", flush=True)
    pygame.font.init()

    if windowed:
        screen = pygame.display.set_mode((1280, 720), pygame.RESIZABLE)
    else:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Billard Cam")
    pygame.mouse.set_visible(False)
    return screen


def fit_rect(src_w, src_h, dst_w, dst_h):
    """Bild seitenrichtig einpassen (Letterbox), nicht verzerren."""
    scale = min(dst_w / src_w, dst_h / src_h)
    w, h = int(src_w * scale), int(src_h * scale)
    return (dst_w - w) // 2, (dst_h - h) // 2, w, h


ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def frame_to_surface(frame, rotate: int, size):
    if rotate:
        frame = cv2.rotate(frame, ROTATIONS[rotate])
    # Skalieren mit OpenCV (schneller als pygame.transform auf dem Pi)
    frame = cv2.resize(frame, size, interpolation=cv2.INTER_LINEAR)
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    # pygame erwartet (breite, hoehe, 3); OpenCV liefert (hoehe, breite, 3)
    return pygame.image.frombuffer(frame.tobytes(), size, "RGB")


def draw_text(screen, font, text, pos, color=(255, 255, 255)):
    shadow = font.render(text, True, (0, 0, 0))
    label = font.render(text, True, color)
    screen.blit(shadow, (pos[0] + 2, pos[1] + 2))
    screen.blit(label, pos)


def main():
    args = parse_args()
    reader = MjpegReader(args.url).start()
    screen = setup_display(args.windowed)
    font = pygame.font.Font(None, 36)
    big_font = pygame.font.Font(None, 64)
    clock = pygame.time.Clock()

    show_fps = not args.no_fps
    last_id = -1
    surface = None
    target = None

    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN:
                    if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                        return
                    if ev.key == pygame.K_f:
                        show_fps = not show_fps
                if ev.type == pygame.VIDEORESIZE:
                    last_id = -1  # neu skalieren

            frame, frame_id, age = reader.latest()
            sw, sh = screen.get_size()

            # Nur neu umrechnen, wenn wirklich ein neues Bild da ist -> spart CPU
            if frame is not None and frame_id != last_id:
                fh, fw = frame.shape[:2]
                if args.rotate in (90, 270):
                    fw, fh = fh, fw
                target = fit_rect(fw, fh, sw, sh)
                surface = frame_to_surface(frame, args.rotate, (target[2], target[3]))
                last_id = frame_id

            screen.fill((0, 0, 0))
            if surface is not None:
                screen.blit(surface, (target[0], target[1]))

            if frame is None:
                msg = "Warte auf Kamera ..."
                if reader.last_error:
                    msg += f"  ({reader.last_error})"
                draw_text(screen, big_font, msg, (40, sh // 2 - 32), (255, 200, 0))
            elif age > STALE_AFTER_S:
                draw_text(screen, big_font, f"Kamera-Bild veraltet ({age:.0f} s)",
                          (40, 40), (255, 80, 80))

            if show_fps:
                status = "verbunden" if reader.connected else "getrennt"
                draw_text(screen, font,
                          f"Cam {reader.fps:4.1f} fps | Anzeige {clock.get_fps():4.1f} fps | {status}",
                          (20, sh - 44))

            pygame.display.flip()
            clock.tick(60)  # Anzeige max. 60 Hz, blockiert nicht den Reader-Thread
    finally:
        reader.stop()
        pygame.quit()


if __name__ == "__main__":
    sys.exit(main())
