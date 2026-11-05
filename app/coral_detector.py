"""
Kugelerkennung mit dem Coral-Stick (trainiertes Modell, siehe training/).

Googles Coral-Software laeuft nur mit Python 3.9, dieses Programm mit 3.11. Deshalb
laeuft das Modell in einem eigenen kleinen Container ("coral", coral/coral_server.py),
und dieses Programm schickt ihm die Bilder ueber eine lokale Verbindung (Port 8765):

    Draufsicht -> verkleinern (Letterbox) -> [Coral: Modell] -> Kaesten -> Kugeln

Vom Modell kommen Position und Typ (Weisse / 8 / Volle / Halbe). Die Farbe (fuer die
Kugelnummer), die Verdeckung (Arm ueber dem Tisch) und die genaue Kugelmitte liefert
weiterhin die klassische Bildverarbeitung - das ist robust und braucht kein Training.

AutoDetector nimmt den Coral, wenn ein Modell geladen ist, und faellt sonst (oder bei
jedem Fehler) unbemerkt auf die klassische Erkennung zurueck. Das Spiel laeuft also immer.
"""

import json
import socket
import struct
import time

import cv2
import numpy as np

from detector import Ball, ClassicDetector, DetectInfo

CLASSES = ["cue", "eight", "solid", "stripe"]
DTYPES = {"uint8": np.uint8, "int8": np.int8, "float32": np.float32, "int32": np.int32, "int16": np.int16}


class CoralError(Exception):
    pass


# ------------------------------------------------------------------ Verbindung
class CoralClient:
    """Verbindung zum Coral-Container. Nachricht: 4 Byte Laenge + Inhalt.
    Anfrage: b'I' (Info) oder b'R' + Eingabedaten. Antwort: 1 Byte Status (0 = ok) + Daten."""

    def __init__(self, host="127.0.0.1", port=8765, timeout=2.0, retry_s=5.0):
        self.host, self.port, self.timeout, self.retry_s = host, port, timeout, retry_s
        self.sock = None
        self.info = None
        self.error = "noch nicht verbunden"
        self.ms = None
        self._next_try = 0.0
        self._info_at = 0.0

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise CoralError("Verbindung zum Coral-Container getrennt")
            buf += chunk
        return bytes(buf)

    def _call(self, body):
        try:
            if self.sock is None:
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self.sock.settimeout(self.timeout)
            self.sock.sendall(struct.pack(">I", len(body)) + body)
            n = struct.unpack(">I", self._recv(4))[0]
            resp = self._recv(n)
        except ConnectionRefusedError as e:
            self.close()
            raise CoralError("Coral-Container läuft nicht (docker compose up -d)") from e
        except (OSError, struct.error) as e:
            self.close()
            raise CoralError(f"Coral-Container antwortet nicht ({e})") from e
        if resp[:1] != b"\x00":
            raise CoralError(resp[1:].decode("utf-8", "replace"))
        return resp[1:]

    def fail(self, err):
        self.error = str(err)
        self.info = None
        self._next_try = time.monotonic() + self.retry_s

    def ready(self):
        """Ist ein Modell auf dem Coral bereit? Prueft hoechstens alle paar Sekunden neu."""
        now = time.monotonic()
        if self.info is not None and now - self._info_at < 10:
            return True
        if now < self._next_try:
            return False
        try:
            info = json.loads(self._call(b"I"))
        except (CoralError, ValueError) as e:
            self.fail(e)
            return False
        self.info, self._info_at, self.error = info, now, ""
        return True

    def run(self, data: bytes):
        t0 = time.monotonic()
        raw = self._call(b"R" + data)
        outs, pos = [], 0
        for o in self.info["outputs"]:
            dt = np.dtype(DTYPES[o["dtype"]])
            n = int(np.prod(o["shape"])) * dt.itemsize
            arr = np.frombuffer(raw[pos:pos + n], dtype=dt).reshape(o["shape"])
            pos += n
            if o.get("scale"):
                arr = (arr.astype(np.float32) - o.get("zero_point", 0)) * o["scale"]
            outs.append(arr.astype(np.float32))
        ms = (time.monotonic() - t0) * 1000
        self.ms = ms if self.ms is None else 0.9 * self.ms + 0.1 * ms
        return outs

    def status(self):
        i = self.info or {}
        return {"ready": self.info is not None, "model": i.get("model"), "model_time": i.get("model_time"),
                "tpu": i.get("tpu"), "error": self.error, "ms": None if self.ms is None else round(self.ms, 1)}


# -------------------------------------------------------------- YOLO-Auswertung
def letterbox(img, out_h, out_w, fill=114):
    """Bild ohne Verzerrung auf die Modellgroesse bringen (Rand wird grau aufgefuellt).
    Genau so macht es auch das Training (Ultralytics)."""
    ih, iw = img.shape[:2]
    scale = min(out_w / iw, out_h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    dx, dy = (out_w - nw) // 2, (out_h - nh) // 2
    canvas = np.full((out_h, out_w, 3), fill, np.uint8)
    canvas[dy:dy + nh, dx:dx + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return canvas, scale, dx, dy


def decode_yolo(out, in_w, in_h, conf=0.35, iou=0.45, nc=len(CLASSES)):
    """YOLOv8-Ausgabe (1, 4+nc, N) -> [(cx, cy, w, h, score, klasse, alle_scores)] in Eingabe-Pixeln."""
    a = np.squeeze(out, 0) if out.ndim == 3 else out
    if a.shape[0] == 4 + nc:
        a = a.T
    if a.shape[1] != 4 + nc:
        raise CoralError(f"Unerwartete Modellausgabe {out.shape} (Modell mit {nc} Klassen erwartet)")
    boxes, scores = a[:, :4].copy(), a[:, 4:]
    if boxes.size and float(np.abs(boxes).max()) <= 2.0:   # TFLite-Export liefert 0..1
        boxes *= np.array([in_w, in_h, in_w, in_h], np.float32)
    cls = scores.argmax(1)
    best = scores[np.arange(len(scores)), cls]
    keep = np.nonzero(best >= conf)[0]
    if not len(keep):
        return []
    tl = [[float(b[0] - b[2] / 2), float(b[1] - b[3] / 2), float(b[2]), float(b[3])] for b in boxes[keep]]
    idx = cv2.dnn.NMSBoxes(tl, [float(s) for s in best[keep]], conf, iou)
    idx = np.array(idx).reshape(-1)
    return [(*boxes[keep[i]], float(best[keep[i]]), int(cls[keep[i]]), scores[keep[i]]) for i in idx]


class CoralDetector:
    name = "coral"

    def __init__(self, radius_px, client: CoralClient, conf=0.35):
        self.r = float(radius_px)
        self.client = client
        self.conf = conf
        self.cv = ClassicDetector(radius_px)    # fuer Farbe und Verdeckung

    def _prepare(self, warped):
        inp = self.client.info["input"]
        _, h, w, _ = inp["shape"]
        img, scale, dx, dy = letterbox(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB), h, w)
        dt = np.dtype(DTYPES[inp["dtype"]])
        if dt == np.float32:
            data = img.astype(np.float32) / 255.0
        elif inp.get("scale"):
            q = img.astype(np.float32) / 255.0 / inp["scale"] + inp.get("zero_point", 0)
            info = np.iinfo(dt)
            data = np.clip(np.round(q), info.min, info.max).astype(dt)
        else:
            data = img.astype(dt)
        return data.tobytes(), (w, h, scale, dx, dy)

    def detect(self, warped):
        data, (w, h, scale, dx, dy) = self._prepare(warped)
        outs = self.client.run(data)
        out = next((o for o in outs if len(CLASSES) + 4 in o.shape), outs[0])
        dets = decode_yolo(out, w, h, self.conf)

        H, W = warped.shape[:2]
        blur = cv2.GaussianBlur(warped, (5, 5), 0)
        mask, _, cloth = self.cv._noncloth_mask(blur)
        hsv = cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)
        cands = []
        for cx, cy, bw, bh, score, k, all_scores in dets:
            x, y = (cx - dx) / scale, (cy - dy) / scale
            if not (-self.r <= x <= W + self.r and -self.r <= y <= H + self.r):
                continue
            # Das Modell rechnet in groben Stufen (int8, ~3 px). Mitte auf die Kugelflaeche
            # nachziehen - wie bei der klassischen Erkennung.
            x, y = self.cv._refine(mask, float(np.clip(x, 0, W - 1)), float(np.clip(y, 0, H - 1)))
            cands.append([score, x, y, k, all_scores])

        # Doppelte Treffer derselben Kugel entfernen: zwei echte Kugeln liegen nie naeher
        # als einen Durchmesser beieinander (gleiche Grenze wie die klassische Erkennung).
        kept, min_d2 = [], (1.45 * self.r) ** 2
        for c in sorted(cands, key=lambda c: -c[0]):
            if all((c[1] - o[1]) ** 2 + (c[2] - o[2]) ** 2 >= min_d2 for o in kept):
                kept.append(c)
        cands = kept

        # Es gibt nur eine Weisse und eine 8: die sicherste behaelt den Typ,
        # die anderen bekommen ihre zweitbeste Klasse (voll/halb).
        for special in (0, 1):
            holders = sorted((c for c in cands if c[3] == special), key=lambda c: -c[0])
            for c in holders[1:]:
                c[3] = 2 + int(c[4][3] > c[4][2])

        balls = []
        for score, x, y, k, _ in cands:
            fw, fb, fc, color = self.cv._color_stats(hsv, x, y)
            kind = CLASSES[k]
            if kind == "eight":
                color = "schwarz"
            elif kind == "cue":
                color = ""
            balls.append(Ball(x, y, self.r, kind, color, fw, fb))

        covered = np.zeros_like(mask)
        for b in balls:
            cv2.circle(covered, (int(b.x), int(b.y)), int(self.r * 1.35), 1, -1)
        unexplained = int((mask & (1 - covered)).sum())
        info = DetectInfo(occlusion=unexplained / mask.size, cloth_lab=cloth, candidates=len(dets),
                          extra={"detector": "coral"})
        return balls, info


class AutoDetector:
    """Coral, wenn ein Modell bereit ist - sonst (und bei jedem Fehler) die klassische Erkennung."""

    def __init__(self, radius_px, client: CoralClient, mode=lambda: "auto"):
        self.r = float(radius_px)
        self.client = client
        self.mode = mode
        self.classic = ClassicDetector(radius_px)
        self.coral = CoralDetector(radius_px, client)
        self.active = "opencv"

    @property
    def name(self):
        return self.active

    def detect(self, warped):
        if self.mode() != "opencv" and self.client.ready():
            try:
                res = self.coral.detect(warped)
                self.active = "coral"
                return res
            except CoralError as e:
                print(f"[coral] {e} - weiter mit OpenCV", flush=True)
                self.client.fail(e)
        self.active = "opencv"
        return self.classic.detect(warped)
