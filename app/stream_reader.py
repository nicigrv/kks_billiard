"""
MJPEG-Reader fuer die ESP32-CAM.

Laeuft in einem eigenen Thread, parst den multipart-Stream selbst (kein
OpenCV-VideoCapture, das puffert intern und erzeugt Latenz) und haelt immer
nur das NEUESTE Bild vor. Wer langsamer liest als die Kamera liefert,
ueberspringt alte Bilder statt hinterherzuhinken.

Bei Verbindungsverlust wird automatisch neu verbunden.
"""

import threading
import time
import urllib.request

import cv2
import numpy as np


class MjpegReader:
    def __init__(self, url: str, timeout: float = 5.0, reconnect_delay: float = 1.0):
        self.url = url
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay

        self._lock = threading.Lock()
        self._frame = None          # numpy BGR-Bild
        self._frame_id = 0          # zaehlt hoch bei jedem neuen Bild
        self._frame_time = 0.0      # time.monotonic() des letzten Bildes
        self._connected = False
        self._last_error = ""

        self._fps = 0.0
        self._fps_count = 0
        self._fps_t0 = time.monotonic()

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mjpeg-reader", daemon=True)

    # ---------------- oeffentliche API ----------------
    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def latest(self):
        """Liefert (frame, frame_id, alter_in_sekunden). frame kann None sein."""
        with self._lock:
            age = time.monotonic() - self._frame_time if self._frame is not None else float("inf")
            return self._frame, self._frame_id, age

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def last_error(self) -> str:
        return self._last_error

    # ---------------- intern ----------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._read_stream()
            except Exception as e:  # Netzwerkfehler, Timeout, kaputter Stream ...
                self._last_error = f"{type(e).__name__}: {e}"
            self._connected = False
            self._stop.wait(self.reconnect_delay)

    def _read_stream(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": "billard-pi"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            self._connected = True
            self._last_error = ""
            buf = b""
            while not self._stop.is_set():
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    raise ConnectionError("Stream beendet")
                buf += chunk

                # JPEG-Grenzen ueber SOI (FFD8) / EOI (FFD9) finden.
                # Robuster als sich auf Content-Length zu verlassen.
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf = buf[-1:]  # evtl. halber Marker am Ende behalten
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        buf = buf[start:]
                        break
                    jpg = buf[start:end + 2]
                    buf = buf[end + 2:]
                    self._handle_jpeg(jpg)

                # Schutz gegen unendlich wachsenden Puffer bei Muell im Stream
                if len(buf) > 2_000_000:
                    buf = b""

    def _handle_jpeg(self, jpg: bytes):
        frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return  # kaputtes Bild (WLAN-Stoerung) -> verwerfen
        now = time.monotonic()
        with self._lock:
            self._frame = frame
            self._frame_id += 1
            self._frame_time = now

        self._fps_count += 1
        dt = now - self._fps_t0
        if dt >= 1.0:
            self._fps = self._fps_count / dt
            self._fps_count = 0
            self._fps_t0 = now
