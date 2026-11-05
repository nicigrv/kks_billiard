"""
Coral-Container: fuehrt das trainierte Kugelmodell auf dem Coral USB Accelerator aus.

Laeuft dauerhaft (docker compose: Dienst "coral") und wartet auf 127.0.0.1:8765 auf Bilder
vom Hauptprogramm (app/coral_detector.py). Absichtlich minimal: Bild rein, Modellausgabe
raus. Vor- und Nachbearbeitung macht das Hauptprogramm.

Modell: /data/models/billard_edgetpu.tflite (kommt aus training/train_coral.py, Upload im
Admin-Portal). Wird die Datei ersetzt, laedt der Container das neue Modell automatisch.

Protokoll: jede Nachricht = 4 Byte Laenge (big endian) + Inhalt
    Anfrage  b"I"            -> Antwort b"\\x00" + JSON (Modell, Ein-/Ausgabeformat)
    Anfrage  b"R" + Eingabe  -> Antwort b"\\x00" + alle Ausgaben roh hintereinander
    Fehler                   -> Antwort b"\\x01" + Fehlertext
"""

import json
import os
import socketserver
import struct
import threading
import time

import numpy as np

MODEL = os.environ.get("CORAL_MODEL", "/data/models/billard_edgetpu.tflite")
HOST = os.environ.get("CORAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("CORAL_PORT", "8765"))


def _q(detail):
    scale, zp = detail.get("quantization", (0.0, 0))
    return {"shape": [int(v) for v in detail["shape"]], "dtype": np.dtype(detail["dtype"]).name,
            "scale": float(scale), "zero_point": int(zp)}


class Model:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.interp = None
        self.mtime = None
        self.info = None
        self.error = ""

    def ensure(self):
        """Modell (neu) laden, wenn noetig. Liefert None oder wirft RuntimeError."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self.interp = None
            raise RuntimeError("Noch kein Modell vorhanden (im Admin-Portal hochladen)")
        if self.interp is not None and mtime == self.mtime:
            return
        if time.time() - mtime < 2:
            raise RuntimeError("Modell wird gerade geschrieben")
        from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
        tpus = list_edge_tpus()
        if not tpus:
            self.interp = None
            raise RuntimeError("Kein Coral-Stick gefunden (blauer USB-3-Port?)")
        try:
            t0 = time.monotonic()
            interp = make_interpreter(self.path)
            interp.allocate_tensors()
        except (ValueError, RuntimeError) as e:
            self.interp = None
            self.mtime = mtime
            raise RuntimeError(f"Modell laesst sich nicht laden: {e}")
        inp = interp.get_input_details()[0]
        interp.set_tensor(inp["index"], np.zeros(inp["shape"], inp["dtype"]))
        interp.invoke()               # erster Lauf kopiert das Modell auf den Stick
        self.interp, self.mtime = interp, mtime
        self.info = {
            "model": os.path.basename(self.path),
            "model_time": time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime)),
            "tpu": str(tpus[0].get("type", "usb")) if isinstance(tpus[0], dict) else "usb",
            "input": _q(inp),
            "outputs": [_q(o) for o in interp.get_output_details()],
        }
        print(f"[coral] Modell geladen in {time.monotonic() - t0:.1f} s: {self.info['input']['shape']}",
              flush=True)

    def handle(self, body):
        with self.lock:
            self.ensure()
            if body[:1] == b"I":
                return json.dumps(self.info).encode()
            if body[:1] != b"R":
                raise RuntimeError("Unbekannte Anfrage")
            inp = self.interp.get_input_details()[0]
            data = np.frombuffer(body[1:], dtype=inp["dtype"])
            if data.size != int(np.prod(inp["shape"])):
                raise RuntimeError("Eingabe hat die falsche Groesse")
            self.interp.set_tensor(inp["index"], data.reshape(inp["shape"]))
            self.interp.invoke()
            return b"".join(self.interp.get_tensor(o["index"]).tobytes()
                            for o in self.interp.get_output_details())


MODEL_HOLDER = Model(MODEL)


class Handler(socketserver.BaseRequestHandler):
    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return bytes(buf)

    def handle(self):
        try:
            while True:
                n = struct.unpack(">I", self._recv(4))[0]
                body = self._recv(n)
                try:
                    resp = b"\x00" + MODEL_HOLDER.handle(body)
                except Exception as e:      # Fehler an das Hauptprogramm melden, weiterlaufen
                    resp = b"\x01" + str(e).encode()
                self.request.sendall(struct.pack(">I", len(resp)) + resp)
        except (ConnectionError, OSError):
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    print(f"[coral] warte auf Bilder auf {HOST}:{PORT}, Modell {MODEL}", flush=True)
    try:
        MODEL_HOLDER.ensure()
    except RuntimeError as e:
        print(f"[coral] {e}", flush=True)
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
