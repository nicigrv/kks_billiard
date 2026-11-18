"""
Firmware des Queues ueber das WLAN aktualisieren - ohne USB-Kabel (der Queue ist zusammengebaut).

Ablauf im Admin-Panel (Reiter Queue, Karte "Firmware"):
  1. "Nach Update suchen": Der Pi fragt den Queue (http://<queue-ip>/api/info: Firmware, Release,
     Board) und GitHub (neuestes Release) und zeigt beides an.
  2. "Neueste Version installieren": Der Pi laedt aus dem neuesten Release die Datei
     queue-ota-<board>.bin und schickt sie an den Queue (POST /update, Kopfzeile X-Castcode).
     Oder eine eigene .bin hochladen (z. B. .pio/build/m5stack-atom/firmware.bin).
  3. Der Queue prueft die Datei, schreibt sie in die zweite App-Partition, startet neu und
     meldet sich mit der neuen Version. Geht unterwegs etwas schief, laeuft die alte Firmware weiter.

Braucht Firmware ab 3.3.0 (die hat /update). Aeltere Queues einmal per USB aktualisieren.
Nur Python-Standardbibliothek.
"""

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

from config import UserError

REPO = os.environ.get("BILLARD_REPO", "nicigrv/kks_billiard")
RELEASE_API = os.environ.get("BILLARD_RELEASE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
QUEUE_HTTP_PORT = 80
MAX_FIRMWARE = 4 * 1024 * 1024
CACHE_S = 600                       # GitHub hoechstens alle 10 min fragen (Rate-Limit)
RESTART_WAIT_S = 45                 # so lange auf den Neustart des Queues warten
RESTART_FIRST_S = 5                 # erst nach dieser Zeit nachfragen (der Queue startet gerade neu)


def check_image(data: bytes):
    """Grobe Pruefung einer ESP32-Firmware (nur App, nicht die Datei fuer Adresse 0x0)."""
    if len(data) < 100_000 or len(data) > MAX_FIRMWARE:
        raise UserError("Firmware-Datei hat eine unpassende Größe")
    if data[0] != 0xE9:
        if len(data) > 0x10000 and data[0x10000] == 0xE9:
            raise UserError("Das ist die Datei für USB (ab Adresse 0x0). Für das Update queue-ota-<board>.bin "
                            "bzw. .pio/build/<board>/firmware.bin nehmen")
        raise UserError("Das ist keine ESP32-Firmware (.bin)")


class CueUpdater:
    def __init__(self, cue, http_port=QUEUE_HTTP_PORT, release_api=RELEASE_API):
        self.cue = cue
        self.http_port = http_port
        self.release_api = release_api
        self.lock = threading.Lock()
        self.busy = False
        self.state = ""               # Text fuer das Admin-Panel
        self.ok = None                # True/False nach dem letzten Update, None = laeuft/nie
        self.progress = None          # 0..100 waehrend der Uebertragung
        self.info = None              # /api/info des Queues
        self.latest = None            # {"tag": "v0.1.8", "assets": {name: url}}
        self._latest_t = 0.0

    # ------------------------------------------------------------- Abfragen
    def _queue_host(self):
        addr = self.cue.address
        if not addr or not self.cue.connected:
            raise UserError("Queue nicht verbunden - erst koppeln und einschalten")
        return addr[0]

    def queue_info(self):
        host = self._queue_host()
        try:
            with urllib.request.urlopen(f"http://{host}:{self.http_port}/api/info", timeout=4) as r:
                info = json.loads(r.read())
        except (OSError, ValueError) as e:
            raise UserError(f"Queue antwortet nicht ({e})")
        if not info.get("ota"):
            raise UserError(f"Firmware {info.get('fw', '?')} kann noch kein Online-Update - "
                            "einmal per USB-Kabel die neue Firmware aufspielen")
        self.info = info
        return info

    def latest_release(self, force=False):
        if self.latest and not force and time.monotonic() - self._latest_t < CACHE_S:
            return self.latest
        req = urllib.request.Request(self.release_api, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "Billard-Pi"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise UserError(f"GitHub: Fehler {e.code}" + (" (noch kein Release?)" if e.code == 404 else ""))
        except (OSError, ValueError) as e:
            raise UserError(f"GitHub nicht erreichbar - hat der Pi Internet? ({e})")
        self.latest = {"tag": d.get("tag_name", "?"),
                       "assets": {a["name"]: a["browser_download_url"] for a in d.get("assets", [])}}
        self._latest_t = time.monotonic()
        return self.latest

    def check(self):
        info = self.queue_info()
        rel = self.latest_release(force=True)
        if info.get("release") == rel["tag"]:
            return f"Queue ist aktuell ({rel['tag']}, Firmware {info.get('fw')})"
        return (f"Queue: {info.get('release')} (Firmware {info.get('fw')}), neueste Version: {rel['tag']} - "
                "„Neueste Version installieren“ drücken")

    # ------------------------------------------------------------- Update
    def _begin(self, text):
        with self.lock:
            if self.busy:
                raise UserError("Es läuft schon ein Firmware-Update")
            self.busy = True
            self.ok = None
            self.progress = None
            self.state = text

    def start_github(self):
        """Neueste Firmware aus dem GitHub-Release holen und aufspielen (im Hintergrund)."""
        info = self.queue_info()
        board = info.get("board") or "m5stack-atom"
        rel = self.latest_release(force=True)
        name = f"queue-ota-{board}.bin"
        url = rel["assets"].get(name)
        if not url:
            raise UserError(f"Im Release {rel['tag']} fehlt {name}")
        self._begin(f"Lade {name} ({rel['tag']}) von GitHub ...")
        threading.Thread(target=self._run_github, args=(url, name, rel["tag"]), daemon=True).start()
        return f"Update auf {rel['tag']} gestartet - die LED am Queue blinkt blau"

    def start_file(self, data: bytes):
        """Hochgeladene .bin aufspielen (im Hintergrund)."""
        check_image(data)
        self.queue_info()
        self._begin("Übertrage Firmware an den Queue ...")
        threading.Thread(target=self._run, args=(data, None), daemon=True).start()
        return f"Firmware ({len(data) / 1e6:.2f} MB) wird an den Queue übertragen - die LED blinkt blau"

    def _run_github(self, url, name, tag):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Billard-Pi"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(MAX_FIRMWARE + 1)
            check_image(data)
        except (OSError, UserError) as e:
            return self._finish(False, f"Download von {name} fehlgeschlagen: {e}")
        self._run(data, tag)

    def _run(self, data, tag):
        try:
            self._send(data)
        except (OSError, UserError) as e:
            return self._finish(False, f"Übertragung fehlgeschlagen: {e} - die alte Firmware läuft weiter")
        self.state = "Queue startet neu ..."
        self.progress = 100
        before = (self.info or {}).get("release")
        end = time.monotonic() + RESTART_WAIT_S
        time.sleep(RESTART_FIRST_S)
        while time.monotonic() < end:
            try:
                with urllib.request.urlopen(f"http://{self._queue_host()}:{self.http_port}/api/info", timeout=3) as r:
                    info = json.loads(r.read())
                self.info = info
                if tag and info.get("release") != tag:
                    return self._finish(False, f"Queue meldet {info.get('release')} statt {tag}")
                same = not tag and info.get("release") == before
                return self._finish(True, f"Fertig: Queue läuft mit {info.get('release')} (Firmware {info.get('fw')})"
                                    + (" - Release-Version unverändert (selbst gebaut?)" if same else ""))
            except (OSError, ValueError, UserError):
                time.sleep(2)
        self._finish(False, "Firmware übertragen, aber der Queue meldet sich nicht zurück - "
                            "WLAN prüfen, im Notfall Queue aus- und einschalten")

    def _send(self, data):
        """Multipart-Upload an http://<queue>/update, mit Fortschritt."""
        host = self._queue_host()
        boundary = uuid.uuid4().hex
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"firmware\"; filename=\"firmware.bin\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        conn = http.client.HTTPConnection(host, self.http_port, timeout=60)
        try:
            conn.putrequest("POST", "/update")
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(len(head) + len(data) + len(tail)))
            conn.putheader("X-Castcode", self.cue.castcode)
            conn.endheaders()
            conn.send(head)
            self.state = "Übertrage Firmware an den Queue ..."
            step = 8192
            for i in range(0, len(data), step):
                conn.send(data[i:i + step])
                self.progress = int((i + step) * 100 / len(data)) if i + step < len(data) else 99
            conn.send(tail)
            r = conn.getresponse()
            body = r.read().decode("utf-8", "replace")
        finally:
            conn.close()
        if r.status != 200:
            try:
                msg = json.loads(body).get("error", body)
            except ValueError:
                msg = body or str(r.status)
            raise UserError(f"Queue meldet: {msg}")

    def _finish(self, ok, text):
        print(f"[queue-update] {text}", flush=True)
        with self.lock:
            self.busy = False
            self.ok = ok
            self.state = text
            if not ok:
                self.progress = None

    def status(self):
        i = self.info or {}
        return {"busy": self.busy, "ok": self.ok, "state": self.state, "progress": self.progress,
                "fw": i.get("fw"), "release": i.get("release"), "board": i.get("board"),
                "latest": (self.latest or {}).get("tag")}
