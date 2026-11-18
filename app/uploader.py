"""
Verbindung zur Trainingsseite im Netz (VPS, training_server/). Das KI-Training laeuft nur dort.

Laeuft im Hintergrund, nur wenn TRAINING_UPLOAD_URL und TRAINING_UPLOAD_KEY gesetzt sind:

  * Bilder hinschicken: dataset/pruefen/ (aufgenommen), dataset/einzel/ (Einzelkugel per Knopf,
    mit Bild des leeren Tisches) und - aus aelteren Versionen - dataset/geprueft/ (am Pi festgelegt).
    Was oben angekommen ist, wird auf dem Pi GELOESCHT (spart Speicher); der Name steht danach in
    dataset/hochgeladen.txt, damit nichts doppelt hochgeht.
  * Modelle holen: alle paar Minuten fragt der Pi, welche Modelle der VPS hat (GET /api/modelle).
    Neue oder geaenderte Dateien (Pruefsumme) laedt er (GET /api/modell?name=...) und uebernimmt sie
    mit denselben Pruefungen wie beim Hochladen im Admin-Panel (install, App.save_model).

Ohne Internet wird es spaeter nochmal versucht; am Tisch merkt man davon nichts.
"""

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from recorder import NAME_RE

# Dateien, die der VPS verteilt (dort beim Hochladen auf diese Namen gebracht)
MODEL_NAMES = ("billard_merkmale.json", "billard_edgetpu.tflite", "billard_klassen_edgetpu.tflite")


class Uploader:
    def __init__(self, recorder, url, key, interval=15, install=None, state_path=None, model_every=300):
        self.rec = recorder
        self.base = url.rstrip("/")
        self.url = self.base + "/api/upload"
        self.key = key
        self.interval = interval
        self.install = install                   # (name, bytes) -> Meldung; wirft bei unbrauchbarer Datei
        self.state_path = state_path
        self.model_every = model_every
        self.list_path = os.path.join(recorder.base_dir, "hochgeladen.txt")
        self.lock = threading.Lock()
        self.sent = self._load()
        self.models = self._load_state()         # Name -> {"sha256", "zeit", "ok", "text"}
        self.last_ok = None
        self.error = None
        self.model_error = None
        self.model_check = None                  # wann zuletzt nach Modellen gefragt
        self.waiting = 0
        self.deleted = 0

    # ------------------------------------------------------------ Ablage
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

    def _load_state(self):
        try:
            with open(self.state_path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_state(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.models, f)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ Bilder
    def _todo(self):
        """(name, jpg, json, festgelegt) fuer alles, was noch nicht oben ist - aelteste zuerst.
        Was schon oben ist, aber noch auf dem Pi liegt (aeltere Versionen), wird dabei geloescht."""
        out = []
        for folder, lbl, done in ((self.rec.pending_dir, self.rec.pending_dir, False),
                                  (self.rec.ok_img, self.rec.ok_lbl, True),
                                  (self.rec.single_dir, self.rec.single_dir, False)):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for fn in names:
                stem = fn[:-4]
                if not (fn.endswith(".jpg") and NAME_RE.fullmatch(stem)):
                    continue
                js = os.path.join(lbl, stem + ".json")
                if stem in self.sent:
                    self.rec.remove(os.path.join(folder, fn), js)
                    self.deleted += 1
                elif os.path.isfile(js):
                    out.append((stem, os.path.join(folder, fn), js, done))
        return sorted(out)

    def _clean_empty_tables(self):
        """Bilder des leeren Tisches (leer-*.jpg) weg, wenn kein Einzelkugel-Bild mehr darauf zeigt.
        Das gemerkte Bild bleibt im Speicher und wird beim naechsten Einzelkugel-Bild neu abgelegt."""
        try:
            names = os.listdir(self.rec.single_dir)
        except OSError:
            return
        if any(n.endswith(".json") for n in names):
            return
        for n in names:
            if n.startswith("leer-") and n.endswith(".jpg"):
                try:
                    os.remove(os.path.join(self.rec.single_dir, n))
                except OSError:
                    pass

    def send(self, name, jpg, js, done):
        with open(jpg, "rb") as f:
            image = base64.b64encode(f.read()).decode()
        with open(js) as f:
            meta = json.load(f)
        d = {"name": name, "image": image, "meta": meta, "festgelegt": done,
             "von": meta.get("labeled_by") if done else None}
        if meta.get("single_ball") is not None and os.path.dirname(jpg) == self.rec.single_dir:
            d["einzelkugel"] = int(meta["single_ball"])
            if meta.get("empty"):
                try:
                    with open(os.path.join(self.rec.single_dir, meta["empty"]), "rb") as f:
                        d["leer"] = base64.b64encode(f.read()).decode()
                except OSError:
                    pass
        body = json.dumps(d).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Upload-Key": self.key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200

    def run_once(self):
        """Alles Offene hochladen und danach auf dem Pi loeschen. Liefert die Anzahl hochgeladener Bilder."""
        with self.lock:
            todo = self._todo()
            self.waiting = len(todo)
            n = 0
            for name, jpg, js, done in todo:
                try:
                    if self.send(name, jpg, js, done):
                        self._mark(name)
                        self.rec.remove(jpg, js)
                        n += 1
                        self.waiting -= 1
                        self.last_ok = time.time()
                        self.error = None
                except FileNotFoundError:
                    continue                     # gerade geloescht ("Alle Bilder loeschen") - naechstes Mal
                except urllib.error.HTTPError as e:
                    self.error = f"Server antwortet {e.code}"
                    if e.code in (400, 413):     # kaputtes Bild: nicht ewig wiederholen, weg damit
                        print(f"[Upload] {name}: Server lehnt ab ({e.code}) - Bild geloescht", flush=True)
                        self._mark(name)
                        self.rec.remove(jpg, js)
                        self.waiting -= 1
                        continue
                    break
                except (OSError, ValueError) as e:
                    self.error = "Server nicht erreichbar"
                    print(f"[Upload] {name}: {e}", flush=True)
                    break
            self._clean_empty_tables()
            return n

    # ------------------------------------------------------------ Modelle
    def _get(self, path, timeout=60):
        req = urllib.request.Request(self.base + path, headers={"X-Upload-Key": self.key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def sync_models(self):
        """Neue Modelle vom VPS holen. Liefert die Namen der uebernommenen Dateien."""
        if self.install is None:
            return []
        self.model_check = time.time()
        try:
            listing = json.loads(self._get("/api/modelle", timeout=20))
            files = [f for f in listing.get("dateien", []) if f.get("name") in MODEL_NAMES]
        except urllib.error.HTTPError as e:
            self.model_error = None if e.code == 404 else f"Server antwortet {e.code}"   # 404: alter Server
            return []
        except (OSError, ValueError, AttributeError) as e:
            self.model_error = "Server nicht erreichbar"
            print(f"[Modelle] {e}", flush=True)
            return []
        self.model_error = None
        got = []
        for f in files:
            name, sha = f["name"], str(f.get("sha256", ""))
            if not sha or self.models.get(name, {}).get("sha256") == sha:
                continue
            try:
                data = self._get("/api/modell?name=" + urllib.parse.quote(name), timeout=120)
            except (OSError, ValueError) as e:
                self.model_error = f"{name}: Download fehlgeschlagen ({e})"
                break
            if hashlib.sha256(data).hexdigest() != sha:
                self.model_error = f"{name}: Prüfsumme stimmt nicht - nächstes Mal neu"
                continue
            entry = {"sha256": sha, "zeit": time.time()}
            try:
                entry.update(ok=True, text=self.install(name, data))
                got.append(name)
            except Exception as e:               # unbrauchbare Datei: merken, nicht immer wieder laden
                entry.update(ok=False, text=str(e))
                print(f"[Modelle] {name} nicht übernommen: {e}", flush=True)
            self.models[name] = entry
            self._save_state()
        return got

    # ------------------------------------------------------------ Anzeige, Ablauf
    def status(self):
        return {"url": self.base, "waiting": self.waiting, "last_ok": self.last_ok, "error": self.error,
                "sent": len(self.sent),
                "models": self.models, "model_error": self.model_error, "model_check": self.model_check}

    def start(self):
        def loop():
            next_models = 0.0
            while True:
                try:
                    n = self.run_once()
                    if n:
                        print(f"[Upload] {n} Bild(er) zum Trainingsserver geschickt und auf dem Pi gelöscht",
                              flush=True)
                    if time.monotonic() >= next_models:
                        next_models = time.monotonic() + self.model_every
                        self.sync_models()
                except Exception as e:           # nie den Pi stoeren
                    print(f"[Upload] Fehler: {e}", flush=True)
                time.sleep(self.interval)
        threading.Thread(target=loop, name="upload", daemon=True).start()
        return self
