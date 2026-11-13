"""
Billard-Training im Netz (VPS): Bilder vom Tisch zuhause festlegen.

  Pi am Tisch ──(gespeicherte Bilder, Schluessel)──▶ POST /api/upload
  Jeder Trainer meldet sich mit seinem Code an (/), legt Kugeln fest:
    Runde 1: jedes Bild wird einmal festgelegt (wer ein Bild offen hat, haelt es 10 Minuten)
    Runde 2: eine ANDERE Person kontrolliert das Ergebnis - "Passt alles" oder korrigieren
    -> fertig, kommt in die Trainings-ZIP
  Erst wenn kein Bild mehr in Runde 1 frei ist, gibt es Bilder aus Runde 2.
  Admin (/admin, hinter Authelia): Trainer und Codes, Stand, ZIP fuer training/train_coral.py.

Nur Standardbibliothek. Daten in DATA_DIR: billard.sqlite, bilder/<name>.jpg, geheim.key.
"""

import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import zipfile

HIER = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_KEY = os.environ.get("UPLOAD_KEY", "")
ADMIN_GRUPPEN = {g.strip() for g in os.environ.get("ADMIN_GRUPPEN", "admins").split(",") if g.strip()}

NAME_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{3}")
CLASSES = ["weiss"] + [str(n) for n in range(1, 16)]
COLOR_NUMBER = {"gelb": 1, "blau": 2, "rot": 3, "violett": 4, "orange": 5, "gruen": 6, "weinrot": 7}
CLAIM_S = 600                     # so lange gehoert ein geoeffnetes Bild dem, der es festlegt
COOKIE = "billard_training"
MAX_AGE = 120 * 24 * 3600
MAX_UPLOAD = 8_000_000


class Fehler(ValueError):
    """Meldung fuer die Seite (HTTP 400, ausser code ist gesetzt)."""

    def __init__(self, text, code=400):
        super().__init__(text)
        self.code = code


# ------------------------------------------------------------------ Kugeln
def ball_num(b):
    n = b.get("number")
    if n is not None:
        return int(n)
    kind = b.get("kind")
    if kind == "cue":
        return 0
    if kind == "eight":
        return 8
    c = COLOR_NUMBER.get(b.get("color") or "")
    if c is None or kind not in ("solid", "stripe"):
        return None
    return c if kind == "solid" else c + 8


def saubere_kugeln(balls):
    """Kugeln aus dem Browser pruefen und auf das gespeicherte Format bringen."""
    if not isinstance(balls, list) or len(balls) > 40:
        raise Fehler("Kugeln fehlen")
    out = []
    for b in balls:
        if not isinstance(b, dict) or b.get("kind") == "none":
            continue
        try:
            x, y = float(b["x"]), float(b["y"])
        except (KeyError, TypeError, ValueError):
            raise Fehler("Kugel ohne Position")
        d = {"x": round(x, 1), "y": round(y, 1), "kind": str(b.get("kind", ""))[:10],
             "color": str(b.get("color", "") or "")[:10]}
        d["number"] = ball_num({"kind": d["kind"], "color": d["color"]})
        if d["number"] is None:
            raise Fehler("Eine Kugel hat noch keine Nummer – Kugel antippen und Nummer wählen")
        if b.get("changed"):
            d["changed"] = True
        out.append(d)
    nummern = [b["number"] for b in out]
    doppelt = sorted({n for n in nummern if nummern.count(n) > 1})
    if doppelt:
        raise Fehler("Nummer " + ", ".join("W" if n == 0 else str(n) for n in doppelt) + " ist doppelt vergeben – bitte prüfen")
    return out


def yolo_lines(balls, radius, w, h):
    out = []
    for b in balls:
        n = ball_num(b)
        if n is not None and 0 <= n < len(CLASSES):
            out.append(f"{n} {b['x'] / w:.5f} {b['y'] / h:.5f} {2 * radius / w:.5f} {2 * radius / h:.5f}")
    return "\n".join(out) + ("\n" if out else "")


def gleich(a, b, radius):
    """Gleiche Kugeln an (fast) gleicher Stelle? Dann hat Runde 2 nichts korrigiert."""
    if len(a) != len(b):
        return False
    frei = list(b)
    for k in a:
        treffer = next((j for j in frei if ball_num(j) == ball_num(k)
                        and (j["x"] - k["x"]) ** 2 + (j["y"] - k["y"]) ** 2 <= (0.5 * radius) ** 2), None)
        if treffer is None:
            return False
        frei.remove(treffer)
    return True


# ------------------------------------------------------------------ Daten
class Daten:
    def __init__(self, data_dir, clock=time.time):
        self.dir = data_dir
        self.bilder = os.path.join(data_dir, "bilder")
        os.makedirs(self.bilder, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.claims = {}                       # Bildname -> (person_id, seit)
        self.fehlversuche = {}                 # IP -> [Zeitpunkte]
        self.db = sqlite3.connect(os.path.join(data_dir, "billard.sqlite"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS personen (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
                aktiv INTEGER NOT NULL DEFAULT 1, erstellt REAL NOT NULL, zuletzt REAL);
            CREATE TABLE IF NOT EXISTS bilder (
                name TEXT PRIMARY KEY, eingang REAL NOT NULL, breite INTEGER NOT NULL, hoehe INTEGER NOT NULL,
                radius REAL NOT NULL, modell TEXT, vorschlag TEXT NOT NULL,
                status TEXT NOT NULL,                          -- runde1 | runde2 | fertig | verworfen
                kugeln1 TEXT, von1 INTEGER, von1_name TEXT, zeit1 REAL,
                kugeln2 TEXT, von2 INTEGER, zeit2 REAL, korrigiert INTEGER,
                verworfen_von INTEGER, verworfen_runde INTEGER);
            CREATE INDEX IF NOT EXISTS bilder_status ON bilder(status, name);
        """)
        self.db.commit()
        key_path = os.path.join(data_dir, "geheim.key")
        if not os.path.exists(key_path):
            with open(key_path, "wb") as f:
                f.write(secrets.token_bytes(32))
            os.chmod(key_path, 0o600)
        with open(key_path, "rb") as f:
            self.geheim = f.read()

    # --------------------------------------------------------------- Personen
    def _neuer_code(self):
        while True:
            code = f"{secrets.randbelow(1_000_000):06d}"
            if not self.db.execute("SELECT 1 FROM personen WHERE code = ?", (code,)).fetchone():
                return code

    def person_anlegen(self, name):
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
        if not name:
            raise Fehler("Bitte einen Namen eingeben")
        with self.lock:
            if self.db.execute("SELECT 1 FROM personen WHERE lower(name) = lower(?)", (name,)).fetchone():
                raise Fehler(f"„{name}“ gibt es schon")
            code = self._neuer_code()
            self.db.execute("INSERT INTO personen (name, code, erstellt) VALUES (?, ?, ?)", (name, code, self.clock()))
            self.db.commit()
        return code

    def person_code_neu(self, pid):
        with self.lock:
            code = self._neuer_code()
            if not self.db.execute("UPDATE personen SET code = ? WHERE id = ?", (code, int(pid))).rowcount:
                raise Fehler("Person nicht gefunden", 404)
            self.db.commit()
        return code

    def person_aktiv(self, pid, aktiv):
        with self.lock:
            if not self.db.execute("UPDATE personen SET aktiv = ? WHERE id = ?", (1 if aktiv else 0, int(pid))).rowcount:
                raise Fehler("Person nicht gefunden", 404)
            self.db.commit()
            if not aktiv:
                self.freigeben(int(pid))

    def person_loeschen(self, pid):
        """Entfernt die Anmeldung. Festgelegte Bilder bleiben, der Name bleibt daran stehen."""
        with self.lock:
            p = self.person(int(pid))
            if not p:
                raise Fehler("Person nicht gefunden", 404)
            self.db.execute("UPDATE bilder SET von1_name = COALESCE(von1_name, ?) WHERE von1 = ?", (p["name"], p["id"]))
            self.db.execute("DELETE FROM personen WHERE id = ?", (p["id"],))
            self.db.commit()
            self.freigeben(p["id"])

    def person(self, pid):
        with self.lock:
                r = self.db.execute("SELECT * FROM personen WHERE id = ?", (pid,)).fetchone()
        return dict(r) if r else None

    def personen(self):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM personen ORDER BY lower(name)")]
            for p in rows:
                p["runde1"] = self.db.execute("SELECT COUNT(*) FROM bilder WHERE von1 = ?", (p["id"],)).fetchone()[0]
                p["runde2"] = self.db.execute("SELECT COUNT(*) FROM bilder WHERE von2 = ?", (p["id"],)).fetchone()[0]
                p["verworfen"] = self.db.execute("SELECT COUNT(*) FROM bilder WHERE verworfen_von = ?", (p["id"],)).fetchone()[0]
        return rows

    def letzter_eingang(self):
        with self.lock:
            return self.db.execute("SELECT MAX(eingang) FROM bilder").fetchone()[0]

    # --------------------------------------------------------------- Anmeldung
    def _token(self, p):
        return hmac.new(self.geheim, f"{p['id']}:{p['code']}".encode(), hashlib.sha256).hexdigest()[:32]

    def anmelden(self, code, ip):
        now = self.clock()
        with self.lock:
            versuche = [t for t in self.fehlversuche.get(ip, []) if now - t < 600]
            self.fehlversuche[ip] = versuche
            if len(versuche) >= 8:
                raise Fehler("Zu viele Fehlversuche – bitte in 10 Minuten nochmal", 429)
            code = re.sub(r"\D", "", str(code or ""))
            r = self.db.execute("SELECT * FROM personen WHERE code = ?", (code,)).fetchone() if len(code) == 6 else None
            if not r:
                versuche.append(now)
                raise Fehler("Code stimmt nicht", 401)
            if not r["aktiv"]:
                raise Fehler("Dieser Code ist gerade gesperrt – frag beim Admin nach", 403)
            self.fehlversuche.pop(ip, None)
            p = dict(r)
        return p, f"{p['id']}.{self._token(p)}"

    def person_aus_cookie(self, wert):
        try:
            pid, tok = (wert or "").split(".", 1)
            p = self.person(int(pid))
        except (ValueError, TypeError):
            return None
        if not p or not p["aktiv"] or not hmac.compare_digest(tok, self._token(p)):
            return None
        now = self.clock()
        if not p["zuletzt"] or now - p["zuletzt"] > 60:
            with self.lock:
                self.db.execute("UPDATE personen SET zuletzt = ? WHERE id = ?", (now, p["id"]))
                self.db.commit()
        return p

    # --------------------------------------------------------------- Upload vom Pi
    def upload(self, d):
        name = str(d.get("name", ""))
        if not NAME_RE.fullmatch(name):
            raise Fehler("Bildname")
        try:
            jpg = base64.b64decode(d.get("image", ""), validate=True)
        except (ValueError, TypeError):
            raise Fehler("Bild")
        if not jpg.startswith(b"\xff\xd8") or len(jpg) > MAX_UPLOAD:
            raise Fehler("Bild ist kein JPEG")
        meta = d.get("meta") or {}
        try:
            w, h, radius = int(meta["width"]), int(meta["height"]), float(meta["radius"])
        except (KeyError, TypeError, ValueError):
            raise Fehler("Bildangaben fehlen")
        balls = [b for b in meta.get("balls", []) if isinstance(b, dict)]
        with self.lock:
            if self.db.execute("SELECT 1 FROM bilder WHERE name = ?", (name,)).fetchone():
                return False
            tmp = os.path.join(self.bilder, name + ".jpg.tmp")
            with open(tmp, "wb") as f:
                f.write(jpg)
            os.replace(tmp, os.path.join(self.bilder, name + ".jpg"))
            festgelegt = bool(d.get("festgelegt")) and all(ball_num(b) is not None for b in balls)
            if festgelegt:
                # am Tisch schon festgelegt -> zaehlt als Runde 1, jetzt kontrolliert jemand anderes
                kugeln = [{"x": float(b["x"]), "y": float(b["y"]), "kind": b.get("kind", ""),
                           "color": b.get("color", "") or "", "number": ball_num(b)} for b in balls]
                von = "am Tisch" + (f" ({str(d['von'])[:30]})" if d.get("von") else "")
                self.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status,
                                   kugeln1, von1_name, zeit1) VALUES (?,?,?,?,?,?,?, 'runde2', ?, ?, ?)""",
                                (name, self.clock(), w, h, radius, meta.get("model"), json.dumps(balls),
                                 json.dumps(kugeln), von, self.clock()))
            else:
                self.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status)
                                   VALUES (?,?,?,?,?,?,?, 'runde1')""",
                                (name, self.clock(), w, h, radius, meta.get("model"), json.dumps(balls)))
            self.db.commit()
        return True

    # --------------------------------------------------------------- Festlegen
    def _belegt_von_anderem(self, name, pid, now):
        c = self.claims.get(name)
        return c and c[0] != pid and now - c[1] < CLAIM_S

    def freigeben(self, pid):
        with self.lock:
            for n in [n for n, c in self.claims.items() if c[0] == pid]:
                del self.claims[n]

    def zaehler(self, pid=None):
        with self.lock:
            return self._zaehler(pid)

    def _zaehler(self, pid):
        z = {s: 0 for s in ("runde1", "runde2", "fertig", "verworfen")}
        for r in self.db.execute("SELECT status, COUNT(*) n FROM bilder GROUP BY status"):
            z[r["status"]] = r["n"]
        if pid is not None:
            z["fuer_mich2"] = self.db.execute(
                "SELECT COUNT(*) FROM bilder WHERE status = 'runde2' AND (von1 IS NULL OR von1 != ?)", (pid,)).fetchone()[0]
            z["ich"] = self.db.execute(
                "SELECT (SELECT COUNT(*) FROM bilder WHERE von1 = ?) + (SELECT COUNT(*) FROM bilder WHERE von2 = ?)"
                " + (SELECT COUNT(*) FROM bilder WHERE verworfen_von = ?)", (pid, pid, pid)).fetchone()[0]
        return z

    def naechstes(self, pid, nach=None):
        """Runde 1 zuerst (aeltestes freies Bild), dann Runde 2 - nie das eigene Bild aus Runde 1.
        'nach' = Bild, das die Person gerade mit "Spaeter" uebersprungen hat."""
        now = self.clock()
        with self.lock:
            kandidaten = []
            for runde, sql, args in (
                    (1, "SELECT * FROM bilder WHERE status = 'runde1' ORDER BY name", ()),
                    (2, "SELECT * FROM bilder WHERE status = 'runde2' AND (von1 IS NULL OR von1 != ?) ORDER BY name", (pid,))):
                rows = [r for r in self.db.execute(sql, args) if not self._belegt_von_anderem(r["name"], pid, now)]
                if nach:          # erst die danach, dann die davor - das uebersprungene ganz zum Schluss
                    rows = ([r for r in rows if r["name"] > nach] + [r for r in rows if r["name"] < nach],
                            [r for r in rows if r["name"] == nach])
                else:
                    rows = (rows, [])
                kandidaten.append((runde, rows))
            reihenfolge = [(rd, r) for rd, (vorne, _) in kandidaten for r in vorne] + \
                          [(rd, r) for rd, (_, hinten) in kandidaten for r in hinten]
            if not reihenfolge:
                return None
            runde, r = reihenfolge[0]
            self.freigeben(pid)
            self.claims[r["name"]] = (pid, now)
            vorschlag = json.loads(r["vorschlag"]) if runde == 1 else json.loads(r["kugeln1"])
            von = None
            if runde == 2:
                von = r["von1_name"]
                if r["von1"] is not None:
                    p = self.person(r["von1"])
                    von = p["name"] if p else von
            return {"name": r["name"], "width": r["breite"], "height": r["hoehe"], "radius": r["radius"],
                    "runde": runde, "von": von, "model": r["modell"] if runde == 1 else None,
                    "balls": vorschlag, "stand": self.zaehler(pid)}

    def speichern(self, pid, name, balls, verwerfen=False):
        if not NAME_RE.fullmatch(name or ""):
            raise Fehler("Bildname")
        now = self.clock()
        with self.lock:
            r = self.db.execute("SELECT * FROM bilder WHERE name = ?", (name,)).fetchone()
            if not r:
                raise Fehler("Bild nicht gefunden", 404)
            if r["status"] not in ("runde1", "runde2"):
                self.claims.pop(name, None)
                raise Fehler("Dieses Bild ist schon fertig – weiter mit dem nächsten", 409)
            if self._belegt_von_anderem(name, pid, now):
                raise Fehler("Jemand anderes legt dieses Bild gerade fest", 409)
            runde = 1 if r["status"] == "runde1" else 2
            if runde == 2 and r["von1"] == pid:
                raise Fehler("Dein eigenes Bild aus Runde 1 kontrolliert jemand anderes", 409)
            if verwerfen:
                self.db.execute("UPDATE bilder SET status = 'verworfen', verworfen_von = ?, verworfen_runde = ? WHERE name = ?",
                                (pid, runde, name))
                text = "Bild als unbrauchbar aussortiert"
            else:
                kugeln = saubere_kugeln(balls)
                if runde == 1:
                    self.db.execute("UPDATE bilder SET status = 'runde2', kugeln1 = ?, von1 = ?, zeit1 = ? WHERE name = ?",
                                    (json.dumps(kugeln), pid, now, name))
                    text = "Gespeichert – jemand anderes kontrolliert es in Runde 2"
                else:
                    korrigiert = not gleich(json.loads(r["kugeln1"]), kugeln, r["radius"])
                    self.db.execute("UPDATE bilder SET status = 'fertig', kugeln2 = ?, von2 = ?, zeit2 = ?, korrigiert = ? WHERE name = ?",
                                    (json.dumps(kugeln), pid, now, int(korrigiert), name))
                    text = "Korrigiert – fertig fürs Training" if korrigiert else "Bestätigt – fertig fürs Training"
            self.db.commit()
            self.claims.pop(name, None)
        return text

    def bild_pfad(self, name):
        if not NAME_RE.fullmatch(name or ""):
            return None
        p = os.path.join(self.bilder, name + ".jpg")
        return p if os.path.isfile(p) else None

    def zurueck_in_runde1(self, name):
        """Admin: verworfenes Bild doch wieder festlegen lassen."""
        with self.lock:
            if not self.db.execute("""UPDATE bilder SET status = 'runde1', kugeln1 = NULL, von1 = NULL, von1_name = NULL,
                                      zeit1 = NULL, verworfen_von = NULL, verworfen_runde = NULL
                                      WHERE name = ? AND status = 'verworfen'""", (name,)).rowcount:
                raise Fehler("Bild nicht gefunden", 404)
            self.db.commit()

    def letzte(self, n=40):
        out = []
        namen = {p["id"]: p["name"] for p in self.personen()}
        with self.lock:
            rows = list(self.db.execute("SELECT * FROM bilder ORDER BY name DESC LIMIT ?", (n,)))
        for r in rows:
            out.append({"name": r["name"], "status": r["status"], "korrigiert": r["korrigiert"],
                        "von1": namen.get(r["von1"], r["von1_name"]), "von2": namen.get(r["von2"]),
                        "verworfen_von": namen.get(r["verworfen_von"]),
                        "kugeln": len(json.loads(r["kugeln2"] or r["kugeln1"] or r["vorschlag"]))})
        return out

    # --------------------------------------------------------------- Export
    def zip_bauen(self, ziel, mit_runde1=False):
        """Trainings-ZIP wie am Pi ("Trainingsdaten laden"): classes.txt, images/, labels/ (YOLO + json).
        Fertig = Runde 2 kontrolliert. mit_runde1: auch Bilder, die erst einmal festgelegt sind."""
        stati = ("fertig", "runde2") if mit_runde1 else ("fertig",)
        with self.lock:
            rows = list(self.db.execute(f"SELECT * FROM bilder WHERE status IN ({','.join('?' * len(stati))}) ORDER BY name", stati))
        n = 0
        with zipfile.ZipFile(ziel, "w", zipfile.ZIP_STORED) as z:
            z.writestr("classes.txt", "\n".join(CLASSES) + "\n")
            for r in rows:
                kugeln = json.loads(r["kugeln2"] or r["kugeln1"] or "null")
                jpg = self.bild_pfad(r["name"])
                if kugeln is None or not jpg:
                    continue
                z.write(jpg, f"images/{r['name']}.jpg")
                z.writestr(f"labels/{r['name']}.txt", yolo_lines(kugeln, r["radius"], r["breite"], r["hoehe"]))
                z.writestr(f"labels/{r['name']}.json", json.dumps({
                    "radius": r["radius"], "width": r["breite"], "height": r["hoehe"], "balls": kugeln,
                    "runde": 2 if r["status"] == "fertig" else 1, "korrigiert": bool(r["korrigiert"])}))
                n += 1
        return n


# ------------------------------------------------------------------ Web
SEITEN = {"/": "trainieren.html", "/index.html": "trainieren.html", "/admin": "admin.html", "/admin/": "admin.html"}
STATISCH = {"/notify.js": ("notify.js", "application/javascript; charset=utf-8")}


def ist_admin_pfad(pfad):
    return pfad in ("/admin", "/admin/") or pfad.startswith("/api/admin/")


def make_handler(daten: Daten):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "BillardTraining"
        sys_version = ""

        def log_message(self, fmt, *args):
            if not (args and str(args[0]).startswith("GET /api/stand")):
                super().log_message(fmt, *args)

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
                                 "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _ip(self):
            fwd = self.headers.get("X-Forwarded-For", "")
            return fwd.split(",")[0].strip() if fwd else self.client_address[0]

        def _person(self):
            for teil in self.headers.get("Cookie", "").split(";"):
                k, _, v = teil.strip().partition("=")
                if k == COOKIE:
                    return daten.person_aus_cookie(urllib.parse.unquote(v))
            return None

        def _admin(self):
            """Admin = von Authelia angemeldet (Traefik setzt Remote-User nur auf /admin und /api/admin)."""
            user = self.headers.get("Remote-User", "").strip()
            gruppen = {g.strip() for g in self.headers.get("Remote-Groups", "").split(",")}
            return user if user and (gruppen & ADMIN_GRUPPEN) else None

        def _json(self):
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise Fehler("Falsches Format", 415)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD * 1.4:
                raise Fehler("Zu groß", 413)
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise Fehler("Ungültige Anfrage")
            if not isinstance(d, dict):
                raise Fehler("Ungültige Anfrage")
            return d

        def _datei(self, pfad, ctype, name=None):
            with open(pfad, "rb") as f:
                body = f.read()
            h = {"Content-Disposition": f'attachment; filename="{name}"'} if name else None
            self._send(200, body, ctype, h)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(url.query)
            pfad = url.path
            try:
                if pfad == "/gesund":
                    return self._send(200, {"ok": True})
                if pfad in STATISCH:
                    datei, ctype = STATISCH[pfad]
                    return self._datei(os.path.join(HIER, datei), ctype)
                if pfad in SEITEN:
                    if ist_admin_pfad(pfad) and not self._admin():
                        return self._send(403, "Nur für Admins (greylo-Login)", "text/plain; charset=utf-8")
                    return self._datei(os.path.join(HIER, SEITEN[pfad]), "text/html; charset=utf-8")
                if pfad.startswith("/api/admin/"):
                    return self._admin_get(pfad, q)
                p = self._person()
                if pfad == "/api/me":
                    return self._send(200, {"name": p["name"]} if p else {"name": None})
                if not p:
                    return self._send(401, {"error": "Bitte mit deinem Code anmelden"})
                if pfad == "/api/next":
                    nach = (q.get("after") or [None])[0]
                    item = daten.naechstes(p["id"], nach)
                    if not item:
                        return self._send(404, {"stand": daten.zaehler(p["id"])})
                    return self._send(200, item)
                if pfad == "/api/bild":
                    jpg = daten.bild_pfad((q.get("name") or [""])[0])
                    if not jpg:
                        return self._send(404, {"error": "Bild nicht gefunden"})
                    return self._datei(jpg, "image/jpeg")
                if pfad == "/api/stand":
                    return self._send(200, daten.zaehler(p["id"]))
                return self._send(404, {"error": "Nicht gefunden"})
            except Fehler as e:
                self._send(e.code, {"error": str(e)})

        def _admin_get(self, pfad, q):
            if not self._admin():
                return self._send(403, {"error": "Nur für Admins"})
            if pfad == "/api/admin/stand":
                return self._send(200, {"zaehler": daten.zaehler(), "personen": daten.personen(),
                                        "letzte": daten.letzte(), "upload": bool(UPLOAD_KEY),
                                        "letzter_eingang": daten.letzter_eingang()})
            if pfad == "/api/admin/bild":
                jpg = daten.bild_pfad((q.get("name") or [""])[0])
                return self._datei(jpg, "image/jpeg") if jpg else self._send(404, {"error": "Bild nicht gefunden"})
            if pfad == "/api/admin/zip":
                mit1 = (q.get("runde1") or ["0"])[0] == "1"
                fd, tmp = tempfile.mkstemp(suffix=".zip", dir=daten.dir)
                os.close(fd)
                try:
                    n = daten.zip_bauen(tmp, mit1)
                    if not n:
                        return self._send(404, {"error": "Noch keine fertigen Bilder"})
                    name = time.strftime("billard-training-%Y%m%d-%H%M") + ("-mit-runde1" if mit1 else "") + ".zip"
                    size = os.path.getsize(tmp)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with open(tmp, "rb") as f:
                        while chunk := f.read(1 << 20):
                            self.wfile.write(chunk)
                finally:
                    os.remove(tmp)
                return None
            return self._send(404, {"error": "Nicht gefunden"})

        def do_POST(self):
            pfad = urllib.parse.urlsplit(self.path).path
            try:
                if pfad == "/api/upload":
                    key = self.headers.get("X-Upload-Key", "")
                    if not UPLOAD_KEY or not hmac.compare_digest(key, UPLOAD_KEY):
                        return self._send(403, {"error": "Kein Zugriff"})
                    neu = daten.upload(self._json())
                    return self._send(200, {"ok": True, "neu": neu})
                if pfad.startswith("/api/admin/"):
                    return self._admin_post(pfad)
                if pfad == "/api/login":
                    p, wert = daten.anmelden(self._json().get("code"), self._ip())
                    ck = f"{COOKIE}={wert}; Path=/; Max-Age={MAX_AGE}; HttpOnly; SameSite=Lax"
                    if os.environ.get("COOKIE_SECURE", "1") == "1":
                        ck += "; Secure"
                    return self._send(200, {"name": p["name"]}, headers={"Set-Cookie": ck})
                if pfad == "/api/logout":
                    p = self._person()
                    if p:
                        daten.freigeben(p["id"])
                    return self._send(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
                p = self._person()
                if not p:
                    return self._send(401, {"error": "Bitte mit deinem Code anmelden"})
                d = self._json()
                if pfad == "/api/speichern":
                    text = daten.speichern(p["id"], str(d.get("name", "")), d.get("balls"), bool(d.get("discard")))
                    return self._send(200, {"message": text, "stand": daten.zaehler(p["id"])})
                if pfad == "/api/freigeben":
                    daten.freigeben(p["id"])
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "Nicht gefunden"})
            except Fehler as e:
                self._send(e.code, {"error": str(e)})

        def _admin_post(self, pfad):
            if not self._admin():
                return self._send(403, {"error": "Nur für Admins"})
            d = self._json()
            if pfad == "/api/admin/person":
                code = daten.person_anlegen(d.get("name"))
                return self._send(200, {"message": f"Angelegt – Code {code}", "code": code})
            if pfad == "/api/admin/person/code":
                code = daten.person_code_neu(d.get("id"))
                return self._send(200, {"message": f"Neuer Code {code}", "code": code})
            if pfad == "/api/admin/person/aktiv":
                daten.person_aktiv(d.get("id"), bool(d.get("aktiv")))
                return self._send(200, {"message": "Freigeschaltet" if d.get("aktiv") else "Gesperrt"})
            if pfad == "/api/admin/person/loeschen":
                daten.person_loeschen(d.get("id"))
                return self._send(200, {"message": "Entfernt – festgelegte Bilder bleiben"})
            if pfad == "/api/admin/zurueck":
                daten.zurueck_in_runde1(str(d.get("name", "")))
                return self._send(200, {"message": "Bild ist wieder in Runde 1"})
            return self._send(404, {"error": "Nicht gefunden"})

    return Handler


def main():
    daten = Daten(DATA_DIR)
    port = int(os.environ.get("PORT", "8000"))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), make_handler(daten))
    srv.daemon_threads = True
    print(f"Billard-Training auf Port {port}, Daten in {DATA_DIR}, Upload {'an' if UPLOAD_KEY else 'AUS (UPLOAD_KEY fehlt)'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
