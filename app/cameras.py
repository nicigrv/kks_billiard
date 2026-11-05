"""
Kameraquellen. Alle liefern dieselbe Schnittstelle wie stream_reader.MjpegReader:
    start(), stop(), latest() -> (bild, bild_nr, alter_s), fps, connected, last_error

  esp32   ESP32-CAM per WLAN (MJPEG-Stream)                        -> stream_reader.MjpegReader
  usb     USB-Kamera am Pi (/dev/video0 ...)                       -> V4L2Reader
  picam   Raspberry-Pi-Kameramodul (Flachbandkabel, libcamera)     -> PiCamReader
  demo    Foto aus dem Internet als Kamerabild. Ist keine Adresse  -> DemoReader
          gesetzt oder laesst sich das Bild nicht laden, laeuft das
          eingebaute simulierte Spiel (immer verfuegbar).

Die Tisch-Ecken (Kalibrierung) werden je Quelle gespeichert (calib_key), damit der
Wechsel zwischen Demo und echter Kamera die Einrichtung nicht zerstoert.
"""

import hashlib
import os
import threading
import time
import urllib.request

import cv2
import numpy as np

from stream_reader import MjpegReader

SOURCES = ("esp32", "usb", "picam", "demo")
SOURCE_NAMES = {"esp32": "ESP32-CAM (WLAN)", "usb": "USB-Kamera am Pi",
                "picam": "Pi-Kameramodul", "demo": "Demo"}


class _Reader:
    """Gemeinsamer Teil: haelt nur das neueste Bild, zaehlt Bilder pro Sekunde."""

    calib_key = ""
    default_corners = None
    info = ""

    def __init__(self, name):
        self._lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self._frame_time = 0.0
        self._connected = False
        self._last_error = ""
        self._fps = 0.0
        self._n = 0
        self._t0 = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def latest(self):
        with self._lock:
            age = time.monotonic() - self._frame_time if self._frame is not None else float("inf")
            return self._frame, self._frame_id, age

    @property
    def connected(self):
        return self._connected

    @property
    def fps(self):
        return self._fps

    @property
    def last_error(self):
        return self._last_error

    def _push(self, frame):
        now = time.monotonic()
        with self._lock:
            self._frame = frame
            self._frame_id += 1
            self._frame_time = now
        self._connected = True
        self._last_error = ""
        self._n += 1
        if now - self._t0 >= 2.0:
            self._fps = self._n / (now - self._t0)
            self._n, self._t0 = 0, now

    def _fail(self, msg, wait=3.0):
        self._connected = False
        if msg != self._last_error:
            print(f"[kamera] {msg}", flush=True)
        self._last_error = msg
        self._stop.wait(wait)

    def _run(self):
        raise NotImplementedError


# ------------------------------------------------------------------ USB
class V4L2Reader(_Reader):
    """USB-Kamera ueber Video4Linux. MJPG-Format, damit auch 1280x720 fluessig ueber USB geht."""

    def __init__(self, device="/dev/video0", size=(1280, 720), fps=15):
        super().__init__("usb-kamera")
        self.device = device
        self.size = size
        self.want_fps = fps
        self.calib_key = f"usb:{device}"

    def _run(self):
        dev = int(self.device) if str(self.device).isdigit() else self.device
        while not self._stop.is_set():
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            if not cap.isOpened():
                self._fail(f"USB-Kamera {self.device} nicht gefunden (eingesteckt?)")
                continue
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
            cap.set(cv2.CAP_PROP_FPS, self.want_fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.info = f"{w}x{h}"
            print(f"[kamera] USB-Kamera {self.device}: {w}x{h}", flush=True)
            fails = 0
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    fails += 1
                    if fails > 20:
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                self._push(frame)
            cap.release()
            if not self._stop.is_set():
                self._fail("USB-Kamera liefert keine Bilder mehr, verbinde neu ...", 1.0)


# ------------------------------------------------------------------ Pi-Kamera
class PiCamReader(_Reader):
    """Raspberry-Pi-Kameramodul (CSI) ueber picamera2/libcamera."""

    def __init__(self, size=(1280, 720)):
        super().__init__("pi-kamera")
        self.size = size
        self.calib_key = "picam"

    def _run(self):
        try:
            from picamera2 import Picamera2
        except ImportError:
            self._fail("picamera2 ist im Container nicht installiert (siehe README: Pi-Kameramodul)", 3600)
            return
        while not self._stop.is_set():
            cam = None
            try:
                cam = Picamera2()
                # "RGB888" liefert die Bytes in der Reihenfolge B, G, R - passt direkt zu OpenCV
                cam.configure(cam.create_video_configuration(main={"size": tuple(self.size), "format": "RGB888"}))
                cam.start()
                self.info = f"{self.size[0]}x{self.size[1]}"
                print(f"[kamera] Pi-Kameramodul gestartet ({self.info})", flush=True)
                while not self._stop.is_set():
                    self._push(cam.capture_array("main").copy())
            except Exception as e:      # Kamera fehlt / belegt -> spaeter erneut
                self._fail(f"Pi-Kameramodul: {e}", 5.0)
            finally:
                if cam is not None:
                    try:
                        cam.stop()
                        cam.close()
                    except Exception:
                        pass


# ------------------------------------------------------------------ Demo
def load_image(url, cache_dir, timeout=15):
    """Bild aus dem Internet laden; die letzte erfolgreiche Kopie bleibt fuer den
    Offline-Betrieb liegen. Liefert (bild, fehlertext)."""
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, "demo-" + hashlib.sha1(url.encode()).hexdigest()[:12] + ".jpg")
    err = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Billard-Demo/1.0 (Raspberry Pi)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(25 * 1024 * 1024)
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("kein Bild (JPEG/PNG) unter dieser Adresse")
        h, w = img.shape[:2]
        if w > 1600:                       # sehr grosse Fotos verkleinern, spart Rechenzeit
            img = cv2.resize(img, (1600, int(h * 1600 / w)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(cache, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return img, ""
    except Exception as e:
        err = str(e)
    if os.path.isfile(cache):
        img = cv2.imread(cache)
        if img is not None:
            return img, f"offline, gespeicherte Kopie ({err})"
    return None, err


class DemoReader(_Reader):
    """Demo ohne Kamera: ein Foto aus dem Internet - oder das eingebaute simulierte Spiel."""

    def __init__(self, url="", cache_dir="/data", fps=5):
        super().__init__("demo")
        self.url = (url or "").strip()
        self.cache_dir = cache_dir
        self.want_fps = fps
        self.image = None
        self.scenario = None
        if self.url:
            self.image, err = load_image(self.url, cache_dir)
            if self.image is not None:
                self.info = "Bild aus dem Internet" + (f" ({err})" if err else "")
                self.calib_key = "demo:" + hashlib.sha1(self.url.encode()).hexdigest()[:12]
            else:
                self.info = f"Bild nicht ladbar ({err}) - simuliertes Spiel"
                print(f"[kamera] Demo-Bild nicht ladbar: {err}", flush=True)
        if self.image is None:
            from sim.fake_table import Scenario
            from sim.render import DEFAULT_CORNERS
            self.scenario = Scenario()
            self.scenario.start()
            if not self.info:
                self.info = "simuliertes Spiel"
            self.calib_key = "demo:sim"
            self.default_corners = [list(c) for c in DEFAULT_CORNERS]

    def stop(self):
        if self.scenario is not None:
            self.scenario.stop()
        super().stop()

    def _run(self):
        last = None
        while not self._stop.is_set():
            if self.image is not None:
                self._push(self.image)
            else:
                with self.scenario.lock:
                    jpg = self.scenario.jpeg
                if jpg is not None and jpg is not last:
                    last = jpg
                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        self._push(frame)
            self._stop.wait(1 / self.want_fps if self.image is not None else 0.05)


# ------------------------------------------------------------------ Auswahl
def make_reader(cam: dict, data_dir: str):
    src = cam.get("source", "esp32")
    if src == "usb":
        return V4L2Reader(cam.get("device") or "/dev/video0")
    if src == "picam":
        return PiCamReader()
    if src == "demo":
        return DemoReader(cam.get("demo_url", ""), data_dir)
    r = MjpegReader(cam.get("url") or "http://192.168.88.200:81/stream")
    r.calib_key = "esp32"
    r.default_corners = None
    r.info = cam.get("url", "")
    return r
