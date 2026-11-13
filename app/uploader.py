"""
Gespeicherte Trainingsbilder zum Server schicken (Trainingsseite im Netz, training_server/).

Laeuft im Hintergrund, nur wenn TRAINING_UPLOAD_URL und TRAINING_UPLOAD_KEY gesetzt sind.
Schaut regelmaessig in dataset/pruefen/ (offen) und dataset/geprueft/ (am Tisch festgelegt) und
schickt jedes Bild genau einmal hoch. Was schon oben ist, steht in dataset/hochgeladen.txt.
Ohne Internet wird es spaeter nochmal versucht; am Tisch merkt man davon nichts.
Die Bilder bleiben auf dem Pi.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

from recorder import NAME_RE


class Uploader:
    def __init__(self, recorder, url, key, interval=15):
        self.rec = recorder
        self.url = url.rstrip("/") + "/api/upload"
        self.key = key
        self.interval = interval
        self.list_path = os.path.join(recorder.base_dir, "hochgeladen.txt")
        self.lock = threading.Lock()
        self.sent = self._load()
        self.last_ok = None
        self.error = None
        self.waiting = 0

    def _load(self):
        try:
            with open(self.list_path) as f:
                return {line.strip() for line in f if line.strip()}
        except OSError:
            return set()

    def _mark(self, name):
        self.sent.add(name)
        os.makedirs(os.path.dirname(self.list_path), exist_ok=True)
        with open(self.list_path, "a") as f:
            f.write(name + "\n")

    def _todo(self):
        """(name, jpg, json, festgelegt) fuer alles, was noch nicht oben ist - aelteste zuerst."""
        out = []
        for folder, lbl, done in ((self.rec.pending_dir, self.rec.pending_dir, False),
                                  (self.rec.ok_img, self.rec.ok_lbl, True)):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for fn in names:
                stem = fn[:-4]
                if fn.endswith(".jpg") and NAME_RE.fullmatch(stem) and stem not in self.sent:
                    js = os.path.join(lbl, stem + ".json")
                    if os.path.isfile(js):
                        out.append((stem, os.path.join(folder, fn), js, done))
        return sorted(out)

    def send(self, name, jpg, js, done):
        with open(jpg, "rb") as f:
            image = base64.b64encode(f.read()).decode()
        with open(js) as f:
            meta = json.load(f)
        body = json.dumps({"name": name, "image": image, "meta": meta, "festgelegt": done,
                           "von": meta.get("labeled_by") if done else None}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Upload-Key": self.key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200

    def run_once(self):
        """Alles Offene hochladen. Liefert die Anzahl hochgeladener Bilder."""
        with self.lock:
            todo = self._todo()
            self.waiting = len(todo)
            n = 0
            for name, jpg, js, done in todo:
                try:
                    if self.send(name, jpg, js, done):
                        self._mark(name)
                        n += 1
                        self.waiting -= 1
                        self.last_ok = time.time()
                        self.error = None
                except FileNotFoundError:
                    continue                     # gerade am Tisch festgelegt/verschoben - naechstes Mal
                except urllib.error.HTTPError as e:
                    self.error = f"Server antwortet {e.code}"
                    if e.code in (400, 413):
                        self._mark(name)         # kaputtes Bild nicht ewig wiederholen
                        self.waiting -= 1
                        continue
                    break
                except (OSError, ValueError) as e:
                    self.error = "Server nicht erreichbar"
                    print(f"[Upload] {name}: {e}", flush=True)
                    break
            return n

    def status(self):
        return {"waiting": self.waiting, "last_ok": self.last_ok, "error": self.error, "sent": len(self.sent)}

    def start(self):
        def loop():
            while True:
                try:
                    n = self.run_once()
                    if n:
                        print(f"[Upload] {n} Bild(er) zum Trainingsserver geschickt", flush=True)
                except Exception as e:           # nie den Pi stoeren
                    print(f"[Upload] Fehler: {e}", flush=True)
                time.sleep(self.interval)
        threading.Thread(target=loop, name="upload", daemon=True).start()
        return self
