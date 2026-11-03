"""
Admin-Portal (Port 8080): Spiel starten/abbrechen, Korrekturen, Kalibrierung.
Nur Python-Standardbibliothek, keine Zusatzpakete.

Schutz: Ist ADMIN_PIN gesetzt, brauchen alle aendernden Aufrufe den Header X-Admin-Pin.
Lesen (Spielstand, Bilder) ist ohne PIN erlaubt, z. B. fuer ein Handy am Tisch.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def make_handler(app, pin):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Billard/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "admin.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, app.state_dict())
            if path in ("/api/frame.jpg", "/api/view.jpg", "/api/warped.jpg"):
                jpg = app.jpeg(path.rsplit("/", 1)[1].split(".")[0])
                if jpg is None:
                    return self._send(503, {"error": "Noch kein Kamerabild"})
                return self._send(200, jpg, "image/jpeg")
            return self._send(404, {"error": "Nicht gefunden"})

        def do_POST(self):
            if pin and self.headers.get("X-Admin-Pin", "") != pin:
                return self._send(403, {"error": "Falsche PIN"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._send(400, {"error": "Ungültige Daten"})
            name = self.path.split("?")[0].removeprefix("/api/")
            try:
                result = app.command(name, payload)
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": f"Ungültiger Befehl: {e}"})
            if result is None:
                return self._send(404, {"error": f"Unbekannter Befehl {name}"})
            return self._send(200, {"ok": True, "message": result, "state": app.state_dict()})

    return Handler


def start(app, port=8080):
    pin = os.environ.get("ADMIN_PIN", "")
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app, pin))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
    print(f"[web] Admin-Portal auf Port {port}" + (" (mit PIN)" if pin else " (OHNE PIN)"), flush=True)
    return server
