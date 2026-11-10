"""
Webserver (Port 8080). Nur Python-Standardbibliothek, keine Zusatzpakete.

  /          Spiel-Panel (PIN 1212): Spiel starten, Korrekturen, Schlagverbesserung, Queue an/aus
  /admin     Admin-Panel (PIN 1902): Kamera, Tisch, Queue koppeln, QR-Code, Pruefer, KI-Training
  /pruefen   Pruefer (eigener Code): Bilder der Pruefrunde festlegen
  /training  Trainingsmodus (Admin)
  /beamer    Beamerbild im Browser (ohne Anmeldung)

Schutz (auth.py): Die Seiten selbst, die Beamer-Ansicht und die Anmeldung sind offen, alles
andere braucht die passende Rolle. Der Admin darf alles, was das Spiel-Panel darf.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from auth import Auth, LoginError
from config import UserError

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_MODEL_BYTES = 64 * 1024 * 1024
BEAMER_SIZES = {"sd": (960, 540), "hd": (1280, 720), "fhd": (1920, 1080)}


PAGES = {"/": "game", "/index.html": "game", "/admin": "admin", "/beamer": "beamer", "/training": "training",
         "/pruefen": "training"}                       # Pruefer: gleiche Seite wie der Trainingsmodus, eigene Rolle
STATIC = {"/notify.js": ("notify.js", "application/javascript; charset=utf-8")}
PUBLIC_GET = set(PAGES) | set(STATIC) | {"/api/beamer.jpg", "/api/beamer.mjpg", "/api/me"}
SPIEL_GET = {"/api/game", "/api/graphic.jpg"}            # + /videos/...
PRUEFER_GET = {"/api/round", "/api/round/image"}
PUBLIC_POST = {"/api/login", "/api/logout"}
PRUEFER_POST = {"/api/round/submit"}
# Diese Befehle darf auch das Spiel-Panel (PIN 1212); alle anderen nur der Admin
GAME_COMMANDS = {"start", "new_rack", "abort", "foul", "switch_turn", "undo", "undo_shot", "set_group",
                 "adjust", "winner", "assist", "record_round", "cue_use", "cue_calibrate"}


def needed_role(path):
    """Welche Rolle braucht ein GET? None = offen."""
    if path in PUBLIC_GET:
        return None
    if path in SPIEL_GET or path.startswith("/videos/"):
        return "spiel"
    if path in PRUEFER_GET:
        return "pruefer"
    return "admin"


def make_handler(app, auth: Auth):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Billard/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path, ctype, filename=None):
            """Datei mit Range-Unterstuetzung (Safari auf iPad/iPhone spielt Videos nur so ab)."""
            size = os.path.getsize(path)
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            partial = bool(m) and (m.group(1) or m.group(2))
            if partial:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(end - start + 1))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _stream_beamer(self, size, fps):
            """Beamerbild als laufender MJPEG-Strom (Browser zeigt ihn wie ein Video)."""
            boundary = "beamerbild"
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    t0 = time.monotonic()
                    jpg = app.beamer_jpeg(size)
                    self.wfile.write(f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                                     f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
                    self.wfile.flush()
                    time.sleep(max(0.0, 1 / fps - (time.monotonic() - t0)))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return None

        def _who(self):
            return auth.who(self.headers.get("Cookie", ""), self.headers.get("X-Admin-Pin", ""))

        def _denied(self, who, need="admin"):
            if who["role"] is None:
                return self._send(401, {"error": "Bitte anmelden", "login": True})
            text = {"admin": "Nur für den Admin", "spiel": "Nur mit der Spiel-PIN"}.get(need, "Keine Berechtigung")
            return self._send(403, {"error": text, "login": True})

        def _read_json(self):
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if len(path) > 1 and path.endswith("/") and path.rstrip("/") in PAGES:
                path = path.rstrip("/")                          # /admin/ -> /admin
            who = self._who()
            need = needed_role(path)
            if need is not None and not Auth.can(who["role"], need):
                return self._denied(who, need)
            if path in PAGES:
                with open(os.path.join(HERE, PAGES[path] + ".html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path in STATIC:
                fname, ctype = STATIC[path]
                with open(os.path.join(HERE, fname), "rb") as f:
                    return self._send(200, f.read(), ctype)
            if path == "/api/me":
                return self._send(200, {"role": who["role"], "name": who["name"], "pin_required": bool(auth.pin),
                                        "game_pin_required": bool(auth.game_pin)})
            if path == "/api/state":
                return self._send(200, app.state_dict())
            if path == "/api/game":
                return self._send(200, app.game_state())
            if path == "/api/qr.png":
                png = app.qr_preview()
                if png is None:
                    return self._send(404, {"error": "Kein QR-Code"})
                return self._send(200, png, "image/png")
            if path == "/api/round":
                if who["role"] == "pruefer":
                    app.reviews.heartbeat(who["name"])
                    return self._send(200, dict(app.reviews.view(who["name"]), me=who["name"]))
                return self._send(200, {"status": app.reviews.status(), "me": who["name"]})
            if path == "/api/round/image":
                rid = query.get("id", [""])[0]
                fpath = os.path.join(app.reviews.dir, rid + ".jpg")
                if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9]{3}", rid) or not os.path.isfile(fpath):
                    return self._send(404, {"error": "Bild nicht gefunden"})
                return self._send_file(fpath, "image/jpeg")
            if path in ("/api/beamer.jpg", "/api/beamer.mjpg"):
                size = BEAMER_SIZES.get(query.get("q", ["hd"])[0], BEAMER_SIZES["hd"])
                if path.endswith(".jpg"):
                    return self._send(200, app.beamer_jpeg(size), "image/jpeg")
                try:
                    fps = min(10.0, max(1.0, float(query.get("fps", ["5"])[0])))
                except ValueError:
                    fps = 5.0
                return self._stream_beamer(size, fps)
            if path == "/api/graphic.jpg":
                jpg = app.graphic_jpeg()
                if jpg is None:
                    return self._send(503, {"error": "Grafik nicht verfügbar"})
                return self._send(200, jpg, "image/jpeg")
            if path == "/api/snapshot":
                snap = app.snapshot()
                if snap is None:
                    return self._send(503, {"error": "Noch kein Bild - ist der Tisch eingerichtet?"})
                return self._send(200, snap)
            if path == "/api/snapshot.jpg":
                try:
                    jpg = app.snapshot_jpeg(int(query.get("id", ["0"])[0]))
                except ValueError:
                    jpg = None
                if jpg is None:
                    return self._send(404, {"error": "Bild nicht mehr vorhanden"})
                return self._send(200, jpg, "image/jpeg")
            if path == "/api/review/next":
                item = app.recorder.next_pending(query.get("after", [None])[0])
                if item is None:
                    return self._send(404, {"error": "Keine Bilder zu prüfen"})
                return self._send(200, item)
            if path == "/api/review/image":
                fpath = app.recorder.pending_image(query.get("name", [""])[0])
                if fpath is None:
                    return self._send(404, {"error": "Bild nicht gefunden"})
                return self._send_file(fpath, "image/jpeg")
            if path == "/api/all-images.zip":
                # ALLE Bilder vom Pi (auch offene und verworfene) zum Aufheben
                tmp = os.path.join(os.path.dirname(app.recorder.base_dir), f"alle-bilder-{threading.get_ident()}.zip")
                try:
                    if app.recorder.export_all_zip(tmp) == 0:
                        return self._send(404, {"error": "Noch keine Bilder"})
                    return self._send_file(tmp, "application/zip",
                                           filename=time.strftime("billard-alle-bilder-%Y%m%d.zip"))
                except (BrokenPipeError, ConnectionResetError):
                    return None
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            if path == "/api/dataset.zip":
                # Trainingsdaten herunterladen (fuer training/train_coral.py)
                tmp = os.path.join(os.path.dirname(app.recorder.base_dir), f"trainingsdaten-{threading.get_ident()}.zip")
                try:
                    n = app.recorder.export_zip(tmp)
                    if n == 0:
                        return self._send(404, {"error": "Noch keine geprüften Bilder"})
                    return self._send_file(tmp, "application/zip",
                                           filename=time.strftime("billard-training-%Y%m%d.zip"))
                except (BrokenPipeError, ConnectionResetError):
                    return None
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
            if path.startswith("/videos/"):
                fpath = app.rounds.video_path(unquote(path[len("/videos/"):]))
                if fpath is None:
                    return self._send(404, {"error": "Video nicht gefunden"})
                try:
                    return self._send_file(fpath, "video/mp4")
                except (BrokenPipeError, ConnectionResetError):
                    return None
            if path in ("/api/frame.jpg", "/api/view.jpg", "/api/warped.jpg"):
                jpg = app.jpeg(path.rsplit("/", 1)[1].split(".")[0])
                if jpg is None:
                    return self._send(503, {"error": "Noch kein Kamerabild"})
                return self._send(200, jpg, "image/jpeg")
            return self._send(404, {"error": "Nicht gefunden"})

        def do_POST(self):
            path = self.path.split("?")[0]
            who = self._who()
            if path == "/api/login":
                try:
                    data = self._read_json()
                    sess, token = auth.login(data.get("secret"), self.client_address[0])
                except (ValueError, LoginError) as e:
                    return self._send(401, {"error": str(e)})
                return self._send(200, {"ok": True, "role": sess["role"], "name": sess["name"]},
                                  headers={"Set-Cookie": Auth.set_cookie(token)})
            if path == "/api/logout":
                auth.logout(Auth.token_from(self.headers.get("Cookie", "")))
                return self._send(200, {"ok": True}, headers={"Set-Cookie": Auth.clear_cookie()})
            if path in PRUEFER_POST and who["role"] == "pruefer":
                try:
                    data = self._read_json()
                    res = app.reviews.submit(who["name"], str(data["id"]), list(data.get("balls", [])))
                except (KeyError, ValueError, TypeError) as e:
                    return self._send(400, {"error": str(e)})
                msg = "Gespeichert - warte auf die anderen" if res is None else "Gespeichert - alle fertig!"
                return self._send(200, dict(app.reviews.view(who["name"]), ok=True, message=msg, me=who["name"]))
            name = path.removeprefix("/api/")
            need = "spiel" if name in GAME_COMMANDS else "admin"
            if not Auth.can(who["role"], need):
                return self._denied(who, need)
            if path == "/api/model":
                # Coral-Modell hochladen (Datei roh im Body)
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= MAX_MODEL_BYTES:
                    return self._send(400, {"error": "Datei fehlt oder ist zu groß"})
                data = self.rfile.read(n)
                try:
                    msg = app.save_model(data)
                except UserError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"ok": True, "message": msg, "state": app.state_dict()})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._send(400, {"error": "Ungültige Daten"})
            try:
                result = app.command(name, payload)
            except UserError as e:
                return self._send(400, {"error": str(e)})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": f"Ungültiger Befehl: {e}"})
            if result is None:
                return self._send(404, {"error": f"Unbekannter Befehl {name}"})
            state = app.state_dict() if who["role"] == "admin" else app.game_state()
            return self._send(200, {"ok": True, "message": result, "state": state})

    return Handler


class PortalServer(ThreadingHTTPServer):
    # Viele iPads gleichzeitig (z. B. 10 Pruefer + Admin + Beamer-Ansicht): Standard waeren nur
    # 5 wartende Verbindungen, dann werden Anfragen abgewiesen.
    request_queue_size = 128
    daemon_threads = True


def start(app, port=8080):
    pin = os.environ.get("ADMIN_PIN", "")
    game_pin = os.environ.get("GAME_PIN", "")
    auth = Auth(pin, os.environ.get("DATA_DIR", "/data"), app.reviewer_list, game_pin=game_pin)
    app.auth = auth
    server = PortalServer(("0.0.0.0", port), make_handler(app, auth))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    print(f"[web] Spiel-Panel auf Port {port} " + ("(mit PIN)" if game_pin else "(OHNE PIN)") +
          f", Admin-Panel unter /admin " + ("(mit PIN)" if pin else "(OHNE PIN)"), flush=True)
    return server
