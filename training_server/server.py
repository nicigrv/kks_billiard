"""
Billard-Training im Netz (VPS): Bilder vom Tisch zuhause festlegen.

  Pi am Tisch ──(gespeicherte Bilder, Schluessel)──▶ POST /api/upload
  Jeder Trainer meldet sich mit seinem Code an (/) und legt Kugeln fest:
    Jedes Bild legen FESTLEGUNGEN (3) verschiedene Personen unabhaengig voneinander fest,
    ohne Vorschlaege (weder KI noch andere Personen). Wer ein Bild offen hat, haelt es 10 Minuten.
    Bilder, denen nur noch wenig fehlt, kommen zuerst.
    -> fertig; die Trainings-ZIP nimmt je Kugel die Mehrheit (gleiche Nummer an fast gleicher Stelle).
  Admin (/admin, hinter Authelia): Trainer und Codes, Stand, ZIP fuer training/train_coral.py.
  Modelle fuer den Pi (modelle.py): der VPS lernt die Farben (Stufe 2 + 3) aus den fertigen Bildern,
    Coral-Modelle aus Colab werden hier hochgeladen; der Pi holt alles von selbst
    (GET /api/modelle, GET /api/modell?name=..., beides mit X-Upload-Key). Auf dem Pi wird nichts gelernt.
  Spielaufzeichnung (aufzeichnung.py): der Pi schickt jedes Rack als Daten, der Server baut das Video
    (gleiche Grafik wie der Beamer) und liefert es unter einer nicht erratbaren Adresse aus.
  Einzelkugel-Bilder (am Pi per Knopf, Nummer am Pi festgelegt): die Kugel findet eine einfache
    Bilderkennung hier auf dem Server (einzelkugel.py, Vergleich mit dem leeren Tisch). Genau eine
    Kugel gefunden -> Bild ist sofort fertig ("Bilderkennung"); sonst legen es Trainer wie gewohnt fest.

Python-Standardbibliothek, fuer die Einzelkugel-Erkennung zusaetzlich Pillow + numpy, fuers
Farben-Lernen OpenCV + numpy (kugel_merkmale.py, Kopie aus app/).
Daten in DATA_DIR: billard.sqlite, bilder/<name>.jpg, bilder/leer/<hash>.jpg, geheim.key, modelle/.
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

try:
    import einzelkugel                 # Pillow + numpy; ohne: Einzelkugel-Bilder legen Trainer fest
except ImportError:
    einzelkugel = None
try:
    import modelle                     # OpenCV + numpy; ohne: keine Modelle fuer den Pi
except ImportError:
    modelle = None
import aufzeichnung                    # Videos bauen braucht pygame + ffmpeg erst beim Bauen

HIER = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_KEY = os.environ.get("UPLOAD_KEY", "")
# Trainings-ZIP ohne greylo-Login laden (z. B. direkt in Google Colab): GET /api/export.zip
# mit Kopfzeile X-Export-Key. Leer = aus. scripts/update.sh legt den Schluessel beim Update an.
EXPORT_KEY = os.environ.get("EXPORT_KEY", "")
ADMIN_GRUPPEN = {g.strip() for g in os.environ.get("ADMIN_GRUPPEN", "admins").split(",") if g.strip()}

NAME_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{3}")
CLASSES = ["weiss"] + [str(n) for n in range(1, 16)]
COLOR_NUMBER = {"gelb": 1, "blau": 2, "rot": 3, "violett": 4, "orange": 5, "gruen": 6, "weinrot": 7}
CLAIM_S = 600                     # so lange gehoert ein geoeffnetes Bild dem, der es festlegt
ZIEL = max(1, int(os.environ.get("FESTLEGUNGEN", "3")))   # so oft wird jedes Bild festgelegt
COOKIE = "billard_training"
MAX_AGE = 120 * 24 * 3600
MAX_UPLOAD = 8_000_000
ERKENNUNG = "Bilderkennung"        # "Person", die Einzelkugel-Bilder festlegt (id 0)


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
            raise Fehler("Kugel ohne Position") from None
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


def konsens(listen, radius):
    """Mehrheit aus unabhaengigen Festlegungen: eine Kugel zaehlt, wenn mehr als die Haelfte sie mit
    derselben Nummer an fast derselben Stelle (0,8 Radien) gesetzt hat. Position = Mittelwert.
    Liefert (kugeln, einig) - einig: alle Festlegungen sagen dasselbe."""
    if not listen:
        return [], False
    noetig = len(listen) // 2 + 1
    tol2 = (0.8 * radius) ** 2
    out, einig = [], True
    for n in sorted({ball_num(b) for l in listen for b in l}):
        pts = [b for l in listen for b in l if ball_num(b) == n]
        best = []
        for p in pts:
            nah = [q for q in pts if (q["x"] - p["x"]) ** 2 + (q["y"] - p["y"]) ** 2 <= tol2]
            if len(nah) > len(best):
                best = nah
        if len(best) < len(listen) or len(pts) != len(listen):
            einig = False
        if len(best) >= noetig:
            out.append({"x": round(sum(q["x"] for q in best) / len(best), 1),
                        "y": round(sum(q["y"] for q in best) / len(best), 1),
                        "kind": best[0].get("kind", ""), "color": best[0].get("color", ""),
                        "number": n, "stimmen": len(best)})
    return out, einig


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
        self.claims = {}                       # Bildname -> {person_id: seit}
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
                status TEXT NOT NULL,                          -- offen | fertig | verworfen (frueher runde1|runde2)
                kugeln1 TEXT, von1 INTEGER, von1_name TEXT, zeit1 REAL,
                kugeln2 TEXT, von2 INTEGER, zeit2 REAL, korrigiert INTEGER,
                verworfen_von INTEGER, verworfen_runde INTEGER);
            CREATE INDEX IF NOT EXISTS bilder_status ON bilder(status, name);
            CREATE TABLE IF NOT EXISTS festlegungen (
                bild TEXT NOT NULL, person INTEGER NOT NULL, person_name TEXT NOT NULL,
                kugeln TEXT NOT NULL, zeit REAL NOT NULL, UNIQUE (bild, person));
            CREATE INDEX IF NOT EXISTS festlegungen_person ON festlegungen(person);
        """)
        spalten = {r["name"] for r in self.db.execute("PRAGMA table_info(bilder)")}
        if "einzel" not in spalten:            # Kugelnummer bei Einzelkugel-Bildern, sonst NULL
            self.db.execute("ALTER TABLE bilder ADD COLUMN einzel INTEGER")
            self.db.execute("ALTER TABLE bilder ADD COLUMN erkennung TEXT")
        os.makedirs(os.path.join(self.bilder, "leer"), exist_ok=True)
        # Umstellung auf "jedes Bild mehrfach, ohne Vorschlaege": alte Runden werden zu "offen".
        # Am Tisch festgelegte Kugeln bleiben in kugeln1 stehen, zaehlen aber nicht als Festlegung.
        self.db.execute("""UPDATE bilder SET status = 'offen' WHERE status IN ('runde1', 'runde2', 'fertig')
                           AND name NOT IN (SELECT bild FROM festlegungen)""")
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
                p["festgelegt"] = self.db.execute("SELECT COUNT(*) FROM festlegungen WHERE person = ?", (p["id"],)).fetchone()[0]
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
            raise Fehler("Bild") from None
        if not jpg.startswith(b"\xff\xd8") or len(jpg) > MAX_UPLOAD:
            raise Fehler("Bild ist kein JPEG")
        meta = d.get("meta") or {}
        try:
            w, h, radius = int(meta["width"]), int(meta["height"]), float(meta["radius"])
        except (KeyError, TypeError, ValueError):
            raise Fehler("Bildangaben fehlen") from None
        balls = [b for b in meta.get("balls", []) if isinstance(b, dict)]
        einzel = d.get("einzelkugel")
        if einzel is not None:
            if not isinstance(einzel, int) or not 0 <= einzel <= 15:
                raise Fehler("Kugelnummer")
            return self._upload_einzel(name, jpg, d.get("leer"), w, h, radius, einzel)
        with self.lock:
            if self.db.execute("SELECT 1 FROM bilder WHERE name = ?", (name,)).fetchone():
                return False
            self._bild_speichern(name, jpg)
            festgelegt = bool(d.get("festgelegt")) and all(ball_num(b) is not None for b in balls)
            if festgelegt:
                # am Tisch schon festgelegt -> wird aufgehoben, zaehlt aber nicht: online legen trotzdem alle neu fest
                kugeln = [{"x": float(b["x"]), "y": float(b["y"]), "kind": b.get("kind", ""),
                           "color": b.get("color", "") or "", "number": ball_num(b)} for b in balls]
                von = "am Tisch" + (f" ({str(d['von'])[:30]})" if d.get("von") else "")
                self.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status,
                                   kugeln1, von1_name, zeit1) VALUES (?,?,?,?,?,?,?, 'offen', ?, ?, ?)""",
                                (name, self.clock(), w, h, radius, meta.get("model"), json.dumps(balls),
                                 json.dumps(kugeln), von, self.clock()))
            else:
                self.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status)
                                   VALUES (?,?,?,?,?,?,?, 'offen')""",
                                (name, self.clock(), w, h, radius, meta.get("model"), json.dumps(balls)))
            self.db.commit()
        return True

    def _bild_speichern(self, name, jpg):
        tmp = os.path.join(self.bilder, name + ".jpg.tmp")
        with open(tmp, "wb") as f:
            f.write(jpg)
        os.replace(tmp, os.path.join(self.bilder, name + ".jpg"))

    def _leer_speichern(self, leer_b64):
        """Bild des leeren Tisches (kommt mit jedem Einzelkugel-Bild) einmal ablegen -> Bytes oder None."""
        if not leer_b64:
            return None
        try:
            leer = base64.b64decode(leer_b64, validate=True)
        except (ValueError, TypeError):
            raise Fehler("Bild des leeren Tisches") from None
        if not leer.startswith(b"\xff\xd8") or len(leer) > MAX_UPLOAD:
            raise Fehler("Bild des leeren Tisches ist kein JPEG")
        pfad = os.path.join(self.bilder, "leer", hashlib.sha256(leer).hexdigest()[:20] + ".jpg")
        if not os.path.exists(pfad):
            with open(pfad + ".tmp", "wb") as f:
                f.write(leer)
            os.replace(pfad + ".tmp", pfad)
        return leer

    def _upload_einzel(self, name, jpg, leer_b64, w, h, radius, nummer):
        """Einzelkugel-Bild: Nummer kommt vom Pi, die Position findet die Bilderkennung."""
        leer = self._leer_speichern(leer_b64)
        kugeln, grund = [], "Bilderkennung nicht installiert (Pillow/numpy)"
        if einzelkugel is not None:
            try:
                kugeln, grund = einzelkugel.finde_kugel(jpg, leer, radius)
            except Exception as e:                       # kaputtes Bild o. ae.: dann legen Trainer fest
                kugeln, grund = [], f"Bilderkennung: {e}"
        basis = {"x": 0, "y": 0, "kind": "cue" if nummer == 0 else "eight" if nummer == 8 else
                  ("solid" if nummer < 8 else "stripe"),
                  "color": "" if nummer == 0 else "schwarz" if nummer == 8 else
                  next(c for c, v in COLOR_NUMBER.items() if v == (nummer if nummer < 8 else nummer - 8)),
                  "number": nummer}
        gefunden = len(kugeln) == 1
        now = self.clock()
        with self.lock:
            if self.db.execute("SELECT 1 FROM bilder WHERE name = ?", (name,)).fetchone():
                return False
            self._bild_speichern(name, jpg)
            if gefunden:
                kugel = dict(basis, x=kugeln[0][0], y=kugeln[0][1])
                self.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status,
                                   einzel, erkennung) VALUES (?,?,?,?,?,?,?, 'fertig', ?, ?)""",
                                (name, now, w, h, radius, "Einzelkugel", json.dumps([kugel]), nummer, "gefunden"))
                self.db.execute("INSERT INTO festlegungen (bild, person, person_name, kugeln, zeit) VALUES (?,?,?,?,?)",
                                (name, 0, ERKENNUNG, json.dumps([kugel]), now))
            else:
                self.db.execute("""INSERT INTO bilder (name, eingang, breite, hoehe, radius, modell, vorschlag, status,
                                   einzel, erkennung) VALUES (?,?,?,?,?,?,?, 'offen', ?, ?)""",
                                (name, now, w, h, radius, "Einzelkugel", "[]", nummer, grund or "unsicher"))
            self.db.commit()
        return True

    # --------------------------------------------------------------- Festlegen
    def _andere_dran(self, name, pid, now):
        """Wie viele ANDERE haben dieses Bild gerade offen?"""
        return sum(1 for q, t in self.claims.get(name, {}).items() if q != pid and now - t < CLAIM_S)

    def freigeben(self, pid):
        with self.lock:
            for n in list(self.claims):
                self.claims[n].pop(pid, None)
                if not self.claims[n]:
                    del self.claims[n]

    def _anzahl(self, name):
        return self.db.execute("SELECT COUNT(*) FROM festlegungen WHERE bild = ?", (name,)).fetchone()[0]

    def festlegungen(self, name):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM festlegungen WHERE bild = ? ORDER BY zeit", (name,))]

    def zaehler(self, pid=None):
        with self.lock:
            return self._zaehler(pid)

    def _zaehler(self, pid):
        z = {s: 0 for s in ("offen", "fertig", "verworfen")}
        for r in self.db.execute("SELECT status, COUNT(*) n FROM bilder GROUP BY status"):
            z[r["status"]] = r["n"]
        z["ziel"] = ZIEL
        z["festlegungen"] = self.db.execute("SELECT COUNT(*) FROM festlegungen WHERE person != 0").fetchone()[0]
        z["einzel"] = self.db.execute("SELECT COUNT(*) FROM bilder WHERE einzel IS NOT NULL").fetchone()[0]
        z["einzel_erkannt"] = self.db.execute(
            "SELECT COUNT(*) FROM bilder WHERE einzel IS NOT NULL AND erkennung = 'gefunden'").fetchone()[0]
        if pid is not None:
            z["fuer_mich"] = self.db.execute(
                """SELECT COUNT(*) FROM bilder b WHERE status = 'offen'
                   AND NOT EXISTS (SELECT 1 FROM festlegungen f WHERE f.bild = b.name AND f.person = ?)""", (pid,)).fetchone()[0]
            z["ich"] = self.db.execute(
                "SELECT (SELECT COUNT(*) FROM festlegungen WHERE person = ?) + (SELECT COUNT(*) FROM bilder WHERE verworfen_von = ?)",
                (pid, pid)).fetchone()[0]
        else:
            je = {k: 0 for k in range(ZIEL)}
            for r in self.db.execute("""SELECT (SELECT COUNT(*) FROM festlegungen f WHERE f.bild = b.name) n
                                        FROM bilder b WHERE status = 'offen'"""):
                je[min(r["n"], ZIEL - 1)] += 1
            z["offen_je"] = [je[k] for k in range(ZIEL)]
            z["einig"] = sum(1 for r in self.db.execute("SELECT name, radius FROM bilder WHERE status = 'fertig'")
                             if self._ergebnis(r["name"], r["radius"])[1])
        return z

    def _ergebnis(self, name, radius):
        listen = [json.loads(r["kugeln"]) for r in self.db.execute("SELECT kugeln FROM festlegungen WHERE bild = ?", (name,))]
        return konsens(listen, radius)

    def naechstes(self, pid, nach=None):
        """Ein offenes Bild, das diese Person noch nicht festgelegt hat - Bilder, denen am wenigsten fehlt, zuerst.
        Nie mehr Leute gleichzeitig an einem Bild, als noch Festlegungen fehlen. Ohne Vorschlaege.
        'nach' = Bild, das die Person gerade mit "Spaeter" uebersprungen hat (kommt ganz zum Schluss)."""
        now = self.clock()
        with self.lock:
            rows = [r for r in self.db.execute(
                """SELECT b.*, (SELECT COUNT(*) FROM festlegungen f WHERE f.bild = b.name) n FROM bilder b
                   WHERE status = 'offen' AND NOT EXISTS (SELECT 1 FROM festlegungen f WHERE f.bild = b.name AND f.person = ?)
                   ORDER BY n DESC, name""", (pid,))
                if r["n"] + self._andere_dran(r["name"], pid, now) < ZIEL]
            if nach:
                rows = [r for r in rows if r["name"] != nach] + [r for r in rows if r["name"] == nach]
            if not rows:
                return None
            r = rows[0]
            self.freigeben(pid)
            self.claims.setdefault(r["name"], {})[pid] = now
            return {"name": r["name"], "width": r["breite"], "height": r["hoehe"], "radius": r["radius"],
                    "durchgang": r["n"] + 1, "ziel": ZIEL, "balls": [], "stand": self._zaehler(pid)}

    def speichern(self, pid, name, balls, verwerfen=False):
        if not NAME_RE.fullmatch(name or ""):
            raise Fehler("Bildname")
        now = self.clock()
        with self.lock:
            r = self.db.execute("SELECT * FROM bilder WHERE name = ?", (name,)).fetchone()
            if not r:
                raise Fehler("Bild nicht gefunden", 404)
            if r["status"] != "offen":
                self.claims.pop(name, None)
                raise Fehler("Dieses Bild ist schon fertig – weiter mit dem nächsten", 409)
            if self.db.execute("SELECT 1 FROM festlegungen WHERE bild = ? AND person = ?", (name, pid)).fetchone():
                raise Fehler("Dieses Bild hast du schon festgelegt – weiter mit dem nächsten", 409)
            n = self._anzahl(name)
            if verwerfen:
                self.db.execute("UPDATE bilder SET status = 'verworfen', verworfen_von = ?, verworfen_runde = ? WHERE name = ?",
                                (pid, n + 1, name))
                text = "Bild als unbrauchbar aussortiert"
            else:
                kugeln = saubere_kugeln(balls)
                p = self.person(pid)
                self.db.execute("INSERT INTO festlegungen (bild, person, person_name, kugeln, zeit) VALUES (?,?,?,?,?)",
                                (name, pid, p["name"] if p else "?", json.dumps(kugeln), now))
                if n + 1 >= ZIEL:
                    self.db.execute("UPDATE bilder SET status = 'fertig' WHERE name = ?", (name,))
                    text = f"Gespeichert – Bild ist jetzt {ZIEL}× festgelegt und fertig"
                else:
                    rest = ZIEL - n - 1
                    text = "Gespeichert – noch " + ("eine weitere Person legt" if rest == 1 else f"{rest} weitere Personen legen") + " es fest"
            self.db.commit()
            if name in self.claims:
                self.claims[name].pop(pid, None)
        return text

    def kreise(self, name):
        """Kugeln fuer die Vorschau im Admin (Ergebnis der Festlegungen bzw. der Bilderkennung)."""
        with self.lock:
            r = self.db.execute("SELECT radius FROM bilder WHERE name = ?", (name,)).fetchone()
            if not r:
                return [], 0
            return self._ergebnis(name, r["radius"])[0], r["radius"]

    def bild_pfad(self, name):
        if not NAME_RE.fullmatch(name or ""):
            return None
        p = os.path.join(self.bilder, name + ".jpg")
        return p if os.path.isfile(p) else None

    def zurueck_in_runde1(self, name):
        """Admin: verworfenes Bild doch wieder festlegen lassen (vorhandene Festlegungen bleiben)."""
        with self.lock:
            if not self.db.execute("""UPDATE bilder SET status = 'offen', verworfen_von = NULL, verworfen_runde = NULL
                                      WHERE name = ? AND status = 'verworfen'""", (name,)).rowcount:
                raise Fehler("Bild nicht gefunden", 404)
            self.db.commit()

    def letzte(self, n=40):
        out = []
        namen = {p["id"]: p["name"] for p in self.personen()}
        with self.lock:
            rows = list(self.db.execute("SELECT * FROM bilder ORDER BY name DESC LIMIT ?", (n,)))
            for r in rows:
                fl = [dict(f) for f in self.db.execute("SELECT person, person_name, kugeln FROM festlegungen WHERE bild = ? ORDER BY zeit", (r["name"],))]
                kugeln, einig = konsens([json.loads(f["kugeln"]) for f in fl], r["radius"])
                out.append({"name": r["name"], "status": r["status"], "festlegungen": len(fl),
                            "ziel": 1 if r["erkennung"] == "gefunden" else ZIEL,
                            "einzel": r["einzel"], "erkennung": r["erkennung"],
                            "wer": [namen.get(f["person"], f["person_name"]) for f in fl],
                            "einig": einig if len(fl) >= 2 else None,
                            "verworfen_von": namen.get(r["verworfen_von"]), "kugeln": len(kugeln)})
        return out

    # --------------------------------------------------------------- Lernen (Farben, Stufe 2 + 3)
    def lern_beispiele(self):
        """Fertige Bilder mit dem Ergebnis der Festlegungen (Mehrheit) -> [(bildpfad, [(x, y, nummer)], radius)].
        Nur kleine Daten; die Bilder selbst liest das Lernen eins nach dem anderen."""
        out = []
        with self.lock:
            for r in self.db.execute("SELECT name, radius FROM bilder WHERE status = 'fertig' ORDER BY name"):
                kugeln, _ = self._ergebnis(r["name"], r["radius"])
                pfad = os.path.join(self.bilder, r["name"] + ".jpg")
                if kugeln and os.path.isfile(pfad):
                    out.append((pfad, [(k["x"], k["y"], int(k["number"])) for k in kugeln], float(r["radius"])))
        return out

    # --------------------------------------------------------------- Export
    def zip_bauen(self, ziel, modus="fertig"):
        """Trainings-ZIP wie am Pi ("Trainingsdaten laden"): classes.txt, images/, labels/ (YOLO + json).
        modus fertig: alle ZIEL-mal festgelegten Bilder (Mehrheit je Kugel)
              einig:  nur fertige, bei denen alle Festlegungen uebereinstimmen
              alle:   auch Bilder, die erst 1-2-mal festgelegt sind (Mehrheit der vorhandenen)"""
        stati = ("fertig", "offen") if modus == "alle" else ("fertig",)
        with self.lock:
            rows = list(self.db.execute(f"SELECT * FROM bilder WHERE status IN ({','.join('?' * len(stati))}) ORDER BY name", stati))
            ergebnisse = {r["name"]: (self._anzahl(r["name"]), *self._ergebnis(r["name"], r["radius"])) for r in rows}
        n = 0
        with zipfile.ZipFile(ziel, "w", zipfile.ZIP_STORED) as z:
            z.writestr("classes.txt", "\n".join(CLASSES) + "\n")
            for r in rows:
                anzahl, kugeln, einig = ergebnisse[r["name"]]
                jpg = self.bild_pfad(r["name"])
                if not anzahl or not jpg or (modus == "einig" and not einig):
                    continue
                z.write(jpg, f"images/{r['name']}.jpg")
                z.writestr(f"labels/{r['name']}.txt", yolo_lines(kugeln, r["radius"], r["breite"], r["hoehe"]))
                z.writestr(f"labels/{r['name']}.json", json.dumps({
                    "radius": r["radius"], "width": r["breite"], "height": r["hoehe"], "balls": kugeln,
                    "festlegungen": anzahl, "einig": einig}))
                n += 1
        return n


# ------------------------------------------------------------------ Web
SEITEN = {"/": "trainieren.html", "/index.html": "trainieren.html", "/admin": "admin.html", "/admin/": "admin.html"}
STATISCH = {"/notify.js": ("notify.js", "application/javascript; charset=utf-8")}


def ist_admin_pfad(pfad):
    return pfad in ("/admin", "/admin/") or pfad.startswith("/api/admin/")


def make_handler(daten: Daten, mod=None, auf=None):
    """mod: modelle.Modelle (Farben lernen, Modelle fuer den Pi) oder None (ohne OpenCV).
    auf: aufzeichnung.Aufzeichnungen (Videos der Spielaufzeichnung) oder None."""
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
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise Fehler("Ungültige Länge") from None
            if n < 0:
                raise Fehler("Ungültige Länge")
            if n > MAX_UPLOAD * 1.4:
                raise Fehler("Zu groß", 413)
            try:
                d = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise Fehler("Ungültige Anfrage") from None
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
                if pfad in ("/api/modelle", "/api/modell"):
                    return self._modell_get(pfad, q)
                if pfad == "/api/aufzeichnungen":
                    if not self._pi():
                        return self._send(403, {"error": "Kein Zugriff"})
                    return self._send(200, {"videos": auf.liste() if auf else []})
                if pfad.startswith("/video/") and pfad.endswith(".mp4"):
                    p = auf.pfad(pfad[len("/video/"):-4]) if auf else None
                    if p is None:
                        return self._send(404, {"error": "Video nicht gefunden"})
                    return self._datei_teil(p, "video/mp4")
                if pfad == "/api/export.zip":
                    key = self.headers.get("X-Export-Key", "")
                    if not EXPORT_KEY or not hmac.compare_digest(key, EXPORT_KEY):
                        return self._send(403, {"error": "Kein Zugriff (EXPORT_KEY in .env, Kopfzeile X-Export-Key)"})
                    return self._zip(q)
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

        def _datei_teil(self, pfad, ctype):
            """Datei mit Range-Unterstuetzung (Safari auf iPad/iPhone spielt Videos nur so ab)."""
            size = os.path.getsize(pfad)
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            teil = bool(m) and bool(m.group(1) or m.group(2))
            if teil:
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
                    return None
            self.send_response(206 if teil else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            if teil:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return None
            try:
                with open(pfad, "rb") as f:
                    f.seek(start)
                    rest = end - start + 1
                    while rest > 0:
                        chunk = f.read(min(1 << 16, rest))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        rest -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None

        def _pi(self):
            """Der Pi am Tisch (gleicher Schluessel wie beim Hochladen der Bilder)?"""
            key = self.headers.get("X-Upload-Key", "")
            return bool(UPLOAD_KEY) and hmac.compare_digest(key, UPLOAD_KEY)

        def _modell_get(self, pfad, q):
            """Pi holt Modelle: Liste mit Pruefsummen bzw. eine Datei."""
            if not self._pi():
                return self._send(403, {"error": "Kein Zugriff"})
            if mod is None:
                return self._send(503, {"error": "Modelle auf dem Server nicht verfügbar (OpenCV fehlt)"})
            if pfad == "/api/modelle":
                return self._send(200, {"dateien": [{k: f[k] for k in ("name", "sha256", "groesse", "zeit")}
                                                    for f in mod.liste()]})
            name = (q.get("name") or [""])[0]
            p = mod.pfad(name)
            if p is None:
                return self._send(404, {"error": "Datei nicht gefunden"})
            mod.abgeholt(name)
            return self._datei(p, "application/octet-stream", name)

        def _zip(self, q):
            """Trainings-ZIP bauen und schicken (Admin-Seite oder /api/export.zip mit Schluessel)."""
            modus = (q.get("modus") or ["alle" if (q.get("runde1") or ["0"])[0] == "1" else "fertig"])[0]
            if modus not in ("fertig", "einig", "alle"):
                modus = "fertig"
            fd, tmp = tempfile.mkstemp(suffix=".zip", dir=daten.dir)
            os.close(fd)
            try:
                n = daten.zip_bauen(tmp, modus)
                if not n:
                    return self._send(404, {"error": "Noch keine fertigen Bilder"})
                name = time.strftime("billard-training-%Y%m%d-%H%M") + ("" if modus == "fertig" else "-" + modus) + ".zip"
                size = os.path.getsize(tmp)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Bilder", str(n))
                self.end_headers()
                if self.command != "HEAD":
                    with open(tmp, "rb") as f:
                        while chunk := f.read(1 << 20):
                            self.wfile.write(chunk)
            finally:
                os.remove(tmp)
            return None

        def _admin_get(self, pfad, q):
            if not self._admin():
                return self._send(403, {"error": "Nur für Admins"})
            if pfad == "/api/admin/stand":
                return self._send(200, {"zaehler": daten.zaehler(), "personen": daten.personen(),
                                        "letzte": daten.letzte(), "upload": bool(UPLOAD_KEY),
                                        "letzter_eingang": daten.letzter_eingang(),
                                        "modelle": mod.status() if mod else None,
                                        "videos": dict(auf.status(), liste=auf.liste(20)) if auf else None})
            if pfad == "/api/admin/bild":
                name = (q.get("name") or [""])[0]
                jpg = daten.bild_pfad(name)
                if not jpg:
                    return self._send(404, {"error": "Bild nicht gefunden"})
                if (q.get("kreis") or ["0"])[0] == "1" and einzelkugel is not None:
                    kugeln, radius = daten.kreise(name)
                    if kugeln:
                        with open(jpg, "rb") as f:
                            return self._send(200, einzelkugel.mit_kreisen(f.read(), kugeln, radius), "image/jpeg")
                return self._datei(jpg, "image/jpeg")
            if pfad == "/api/admin/zip":
                return self._zip(q)
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
                if pfad == "/api/aufzeichnung":
                    if not self._pi():
                        return self._send(403, {"error": "Kein Zugriff"})
                    if auf is None:
                        return self._send(503, {"error": "Spielaufzeichnung auf dem Server aus"})
                    try:
                        neu = auf.eingang(self._json())
                    except aufzeichnung.AufzeichnungFehler as e:
                        raise Fehler(str(e)) from None
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
            if pfad.startswith("/api/admin/modell") or pfad == "/api/admin/lernen":
                return self._modell_post(pfad)
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

        def _modell_post(self, pfad):
            if mod is None:
                return self._send(503, {"error": "Modelle auf dem Server nicht verfügbar (OpenCV fehlt)"})
            try:
                if pfad == "/api/admin/modell":
                    # Datei roh im Body, Dateiname in X-Dateiname (Klassen-Modell erkennt man am Namen)
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        n = -1
                    if not 0 < n <= modelle.MAX_MODELL:
                        raise Fehler("Datei fehlt oder ist zu groß", 413 if n > 0 else 400)
                    name = mod.hochladen(self.rfile.read(n), urllib.parse.unquote(self.headers.get("X-Dateiname", "")))
                    return self._send(200, {"message": f"{name} gespeichert – der Pi holt es in den nächsten Minuten",
                                            "modelle": mod.status()})
                d = self._json()
                if pfad == "/api/admin/lernen":
                    beispiele = daten.lern_beispiele()
                    text = mod.lernen(lambda: beispiele, len(beispiele))
                    return self._send(200, {"message": text, "modelle": mod.status()})
                if pfad == "/api/admin/modell/loeschen":
                    name = mod.loeschen(str(d.get("name", "")))
                    return self._send(200, {"message": f"{name} entfernt (liegt als .vorher daneben) – "
                                                       "der Pi behält seine Kopie", "modelle": mod.status()})
            except modelle.ModellFehler as e:
                raise Fehler(str(e)) from None
            return self._send(404, {"error": "Nicht gefunden"})

    return Handler


def main():
    daten = Daten(DATA_DIR)
    mod = modelle.Modelle(DATA_DIR) if modelle else None
    auf = aufzeichnung.Aufzeichnungen(DATA_DIR)
    port = int(os.environ.get("PORT", "8000"))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), make_handler(daten, mod, auf))
    srv.daemon_threads = True
    print(f"Billard-Training auf Port {port}, Daten in {DATA_DIR}, Upload {'an' if UPLOAD_KEY else 'AUS (UPLOAD_KEY fehlt)'}, "
          f"Modelle fuer den Pi {'an' if mod else 'AUS (OpenCV fehlt)'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
