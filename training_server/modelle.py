"""
Modelle fuer die Erkennung am Pi - gelernt bzw. abgelegt auf dem VPS, der Pi holt sie von selbst.

  billard_merkmale.json            Stufe 2 + 3 (gestreift? Farbe?) als Statistik. Lernt der VPS aus den
                                   fertig festgelegten Bildern ("Farben lernen", kugel_merkmale.learn) -
                                   oder sie wird hier hochgeladen (training/train_coral.py --nur-farben).
  billard_edgetpu.tflite           Stufe 1 (Kugeln finden) fuer den KI-Chip - aus Colab, hier hochladen.
  billard_klassen_edgetpu.tflite   Stufe 2 + 3 auf dem KI-Chip (training/train_klassen.py) - hochladen.

Der Pi fragt alle paar Minuten GET /api/modelle (Name, Pruefsumme) und laedt Neues mit
GET /api/modell?name=... (beides mit X-Upload-Key). Er prueft jede Datei noch einmal selbst.
Auf dem Pi wird nichts gelernt - so bleibt er frei fuer Erkennung und Beamer.

Ablage: DATA_DIR/modelle/<name> (+ <name>.vorher = vorherige Fassung), abgeholt.json.
"""

import hashlib
import json
import os
import threading
import time

import cv2

import kugel_merkmale as km

HIER = os.path.dirname(os.path.abspath(__file__))
START_FEATURES = os.path.join(HIER, "start_merkmale.json")
MERKMALE = "billard_merkmale.json"
KUGEL = "billard_edgetpu.tflite"
KLASSEN = "billard_klassen_edgetpu.tflite"
NAMEN = (MERKMALE, KUGEL, KLASSEN)
MAX_MODELL = 64 * 1024 * 1024
MIN_KUGELN = 50          # weniger festgelegte Kugeln: nichts veroeffentlichen, die bisherigen Farben bleiben
BESCHREIBUNG = {MERKMALE: "Farben + Streifen (Stufe 2 + 3, Statistik)",
                KUGEL: "Kugeln finden (Stufe 1, KI-Chip)",
                KLASSEN: "Art + Farbe auf dem KI-Chip (Stufe 2 + 3)"}


class ModellFehler(ValueError):
    pass


def erkennen(data: bytes, dateiname=""):
    """Welche Datei ist das? Gleiche Regeln wie am Pi (App.save_model). -> Ablagename oder ModellFehler."""
    if data[:1] == b"{":
        try:
            km.BallClassifier.from_dict(json.loads(data))
        except (ValueError, KeyError, TypeError) as e:
            raise ModellFehler(f"Keine gültige Datei billard_merkmale.json ({e})") from None
        return MERKMALE
    if len(data) < 1000 or data[4:8] != b"TFL3":
        raise ModellFehler("Das ist keine TFLite-Modelldatei (.tflite) und keine billard_merkmale.json")
    # Klassen-Modell (Stufe 2 + 3) ist klein (~0,2 MB), das Kugel-Modell (Stufe 1) mehrere MB
    if "klassen" in dateiname.lower() or len(data) < 1_500_000:
        return KLASSEN
    if b"edgetpu-custom-op" not in data:
        raise ModellFehler("Modell ist nicht für den Coral kompiliert (die Datei *_edgetpu.tflite aus dem Training nehmen)")
    return KUGEL


class Modelle:
    def __init__(self, data_dir, clock=time.time):
        self.dir = os.path.join(data_dir, "modelle")
        os.makedirs(self.dir, exist_ok=True)
        self.clock = clock
        self.lock = threading.Lock()
        self._sha = {}                     # Name -> (mtime, sha256): Pruefsumme nur bei Aenderung neu rechnen
        self.lernen_laeuft = False
        self.lernen_ergebnis = None        # {"zeit", "ok", "text", "bilder", "kugeln"}

    # ------------------------------------------------------------ Dateien
    def pfad(self, name):
        if name not in NAMEN:
            return None
        p = os.path.join(self.dir, name)
        return p if os.path.isfile(p) else None

    def _sha256(self, name, p):
        mtime = os.path.getmtime(p)
        alt = self._sha.get(name)
        if alt and alt[0] == mtime:
            return alt[1]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        self._sha[name] = (mtime, h.hexdigest())
        return self._sha[name][1]

    def liste(self):
        """[{name, sha256, groesse, zeit, text, abgeholt}] - was der Pi holen kann."""
        abgeholt = self._abgeholt()
        out = []
        with self.lock:
            for name in NAMEN:
                p = self.pfad(name)
                if p is None:
                    continue
                out.append({"name": name, "sha256": self._sha256(name, p), "groesse": os.path.getsize(p),
                            "zeit": os.path.getmtime(p), "text": BESCHREIBUNG[name],
                            "abgeholt": abgeholt.get(name)})
        return out

    def _speichern(self, name, data):
        p = os.path.join(self.dir, name)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        if os.path.exists(p):              # vorherige Fassung aufheben (zurueck: Datei umbenennen)
            os.replace(p, p + ".vorher")
        os.replace(tmp, p)

    def hochladen(self, data: bytes, dateiname=""):
        if not data or len(data) > MAX_MODELL:
            raise ModellFehler("Datei fehlt oder ist zu groß")
        name = erkennen(data, dateiname)
        with self.lock:
            self._speichern(name, data)
        return name

    def loeschen(self, name):
        with self.lock:
            p = self.pfad(name)
            if p is None:
                raise ModellFehler("Datei nicht gefunden")
            os.replace(p, p + ".vorher")
        return name

    def _abgeholt(self):
        try:
            with open(os.path.join(self.dir, "abgeholt.json")) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def abgeholt(self, name):
        """Der Pi hat die Datei geladen - fuer die Anzeige im Admin."""
        with self.lock:
            d = self._abgeholt()
            d[name] = self.clock()
            tmp = os.path.join(self.dir, "abgeholt.json.tmp")
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, os.path.join(self.dir, "abgeholt.json"))

    # ------------------------------------------------------------ Farben lernen
    def lernen(self, beispiele, anzahl, warten=False):
        """Stufe 2 + 3 aus festgelegten Bildern lernen (im Hintergrund).
        beispiele: Funktion -> iterierbar ueber (bildpfad, [(x, y, nummer)], radius); anzahl: wie viele Bilder."""
        with self.lock:
            if self.lernen_laeuft:
                raise ModellFehler("Lernen läuft schon")
            if not anzahl:
                raise ModellFehler("Noch keine fertig festgelegten Bilder")
            self.lernen_laeuft = True

        def bilder():
            for pfad, kugeln, radius in beispiele():
                img = cv2.imread(pfad)                       # eins nach dem anderen: wenig Speicher
                if img is not None and kugeln:
                    yield img, kugeln, radius

        def run():
            t0 = time.monotonic()
            try:
                start = km.BallClassifier.load(START_FEATURES)
                clf, res = km.learn(bilder(), fallback=start, source=f"VPS: {anzahl} fertige Bilder")
                if (clf.info.get("kugeln") or 0) < MIN_KUGELN:
                    raise ModellFehler(f"Zu wenig festgelegte Kugeln ({clf.info.get('kugeln') or 0}) - ab {MIN_KUGELN} "
                                       "wird gelernt. Die bisherigen Farben bleiben.")
                data = json.dumps(clf.to_dict()).encode()
                with self.lock:
                    self._speichern(MERKMALE, data)
                text = km.report(res) if res else f"{clf.info.get('kugeln')} Kugeln gelernt"
                if clf.info.get("geerbt"):
                    text += "\nZu wenig Beispiele (Startwerte behalten): " + ", ".join(clf.info["geerbt"])
                self.lernen_ergebnis = {"zeit": self.clock(), "ok": True, "text": text, "bilder": anzahl,
                                        "kugeln": clf.info.get("kugeln"), "dauer": round(time.monotonic() - t0, 1)}
                print(f"[modelle] Farben gelernt: {text.splitlines()[0]}", flush=True)
            except Exception as e:                           # darf den Server nie stoppen
                self.lernen_ergebnis = {"zeit": self.clock(), "ok": False, "text": f"Fehler: {e}", "bilder": anzahl}
                print(f"[modelle] Farben lernen fehlgeschlagen: {e}", flush=True)
            finally:
                self.lernen_laeuft = False

        t = threading.Thread(target=run, name="farben-lernen", daemon=True)
        t.start()
        if warten:
            t.join()
        return f"Lerne Farben und Streifen aus {anzahl} fertigen Bildern ..."

    def status(self):
        return {"dateien": self.liste(), "lernen_laeuft": self.lernen_laeuft, "lernen": self.lernen_ergebnis}
