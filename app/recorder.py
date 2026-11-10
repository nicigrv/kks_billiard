"""
Trainingsbilder fuer das Coral-Modell.

Ablauf:
  1. Nur per Knopf "Bild aufnehmen" (nichts automatisch) wird die entzerrte Draufsicht
     gespeichert, zusammen mit dem, was der KI-Chip erkannt hat (Position und Kugelnummer).
     -> dataset/pruefen/<name>.jpg + <name>.json
  2. Auf dem iPad wird jedes Bild angesehen: "passt alles" oder korrigieren (oder verwerfen).
     -> dataset/geprueft/images/<name>.jpg
        dataset/geprueft/labels/<name>.json   Kugeln mit Nummer (massgeblich)
        dataset/geprueft/labels/<name>.txt    YOLO-Format: Klasse x_mitte y_mitte breite hoehe (0..1)
     Verworfene Bilder werden NICHT geloescht, sondern landen in dataset/verworfen/.
     Geloescht wird nur mit "Alle Bilder loeschen" (delete_all). Aufgenommen wird, solange
     mehr als 1 GB frei ist.
  3. Nur geprueft/ wird zum Training benutzt (training/train_coral.py). Der Export baut die
     YOLO-Dateien aus den .json neu - so passen auch Bilder aus aelteren Versionen.

Klasse = Kugelnummer: 0 = Weisse, 1-7 Volle, 8 = schwarze 8, 9-15 Halbe.
Jede Kugel braucht eine Nummer; Bilder mit Kugeln ohne Nummer gehen nicht ins Training.
"""

import json
import os
import re
import shutil
import threading
import time
import zipfile

import cv2

from ballnum import ball_number

CLASSES = ["weiss"] + [str(n) for n in range(1, 16)]      # Index = Kugelnummer
NAME_RE = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9]{3}")


def ball_num(b):
    """Nummer einer Kugel (dict): 'number', sonst aus Typ + Farbe (Bilder aelterer Versionen)."""
    n = b.get("number")
    if n is None:
        n = ball_number(b.get("kind"), b.get("color"))
    return None if n is None else int(n)


def _ball_dict(b, number=None):
    if isinstance(b, dict):
        d = {"x": float(b["x"]), "y": float(b["y"]), "kind": b["kind"], "color": b.get("color", "") or ""}
        d["number"] = ball_num(dict(b, **d))
        return d
    n = number if number is not None else getattr(b, "number", None)
    return {"x": float(b.x), "y": float(b.y), "kind": b.kind, "color": getattr(b, "color", "") or "",
            "number": n}


def missing_numbers(balls):
    """Wie viele Kugeln haben keine Nummer? (Volle/Halbe ohne gewaehlte Farbe)"""
    return sum(1 for b in balls if b.get("kind") != "none" and ball_num(b) is None)


def yolo_lines(balls, radius, w, h):
    out = []
    for b in balls:
        n = ball_num(b)
        if n is not None and 0 <= n < len(CLASSES):
            out.append(f"{n} {b['x'] / w:.5f} {b['y'] / h:.5f} {2 * radius / w:.5f} {2 * radius / h:.5f}")
    return "\n".join(out) + ("\n" if out else "")


def write_yolo(path, balls, radius, w, h):
    with open(path, "w") as f:
        f.write(yolo_lines(balls, radius, w, h))


class Recorder:
    def __init__(self, base_dir, min_free_bytes=1_000_000_000):
        self.base_dir = base_dir
        self.pending_dir = os.path.join(base_dir, "pruefen")
        self.ok_img = os.path.join(base_dir, "geprueft", "images")
        self.ok_lbl = os.path.join(base_dir, "geprueft", "labels")
        self.discard_dir = os.path.join(base_dir, "verworfen")
        self.min_free_bytes = min_free_bytes
        self.lock = threading.Lock()

    # ------------------------------------------------------------- Zaehler
    @staticmethod
    def _count(folder, ext):
        try:
            return sum(1 for n in os.listdir(folder) if n.endswith(ext))
        except OSError:
            return 0

    @property
    def pending(self):
        return self._count(self.pending_dir, ".json")

    @property
    def verified(self):
        return self._count(self.ok_img, ".jpg")

    def _new_name(self):
        """Zeitstempel als Name; ist er schon vergeben (gleiche Millisekunde), eins weiter."""
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        ms = int(now * 1000) % 1000
        while True:
            name = f"{stamp}-{ms:03d}"
            if not any(os.path.exists(os.path.join(d, name + ext)) for d, ext in
                       ((self.pending_dir, ".jpg"), (self.ok_img, ".jpg"))):
                return name
            ms += 1
            if ms > 999:
                time.sleep(0.001)
                return self._new_name()

    # ------------------------------------------------------------ 1. aufnehmen
    def save_pending(self, warped, balls, radius, numbers=None, model=None):
        """Knopf "Bild aufnehmen": Bild + Vorschlag des KI-Chips (kann leer sein) zum Festlegen."""
        if not self.disk_ok():
            return None
        numbers = numbers or [None] * len(balls)
        with self.lock:
            os.makedirs(self.pending_dir, exist_ok=True)
            name = self._new_name()
            h, w = warped.shape[:2]
            cv2.imwrite(os.path.join(self.pending_dir, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
            data = {"width": w, "height": h, "radius": float(radius), "model": model,
                    "balls": [_ball_dict(b, n) for b, n in zip(balls, numbers)]}
            tmp = os.path.join(self.pending_dir, name + ".json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, os.path.join(self.pending_dir, name + ".json"))
        return name

    # --------------------------------------------------------- 2. auf dem iPad pruefen
    def next_pending(self, after=None):
        """Aeltestes noch nicht geprueftes Bild (oder das naechste nach 'after')."""
        try:
            names = sorted(n[:-5] for n in os.listdir(self.pending_dir) if n.endswith(".json"))
        except OSError:
            return None
        if after:
            names = [n for n in names if n > after] or names
        for name in names:
            try:
                with open(os.path.join(self.pending_dir, name + ".json")) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            data["name"] = name
            data["pending"] = len(names)
            return data
        return None

    def pending_image(self, name):
        if not NAME_RE.fullmatch(name or ""):
            return None
        p = os.path.join(self.pending_dir, name + ".jpg")
        return p if os.path.isfile(p) else None

    def review(self, name, balls, discard=False):
        """Ergebnis der Pruefung: Bild verwerfen oder mit (ggf. korrigierter) Zuordnung uebernehmen."""
        if not NAME_RE.fullmatch(name or ""):
            raise ValueError("Bildname")
        with self.lock:
            jpg = os.path.join(self.pending_dir, name + ".jpg")
            js = os.path.join(self.pending_dir, name + ".json")
            if not os.path.isfile(js):
                raise FileNotFoundError("Bild wurde schon geprüft")
            k = 0 if discard else missing_numbers(balls)
            if k:
                raise ValueError(f"{k} Kugel(n) ohne Nummer - Kugel antippen und Nummer wählen")
            if discard:
                # aufheben statt loeschen - vielleicht spaeter doch noch nuetzlich
                os.makedirs(self.discard_dir, exist_ok=True)
                for p in (jpg, js):
                    if os.path.exists(p):
                        shutil.move(p, os.path.join(self.discard_dir, os.path.basename(p)))
                return "verworfen (aufgehoben)"
            with open(js) as f:
                meta = json.load(f)
            changed = any(b.get("changed") for b in balls)
            balls = [_ball_dict(b) for b in balls if b["kind"] != "none"]
            self._score(meta, balls)
            os.makedirs(self.ok_img, exist_ok=True)
            os.makedirs(self.ok_lbl, exist_ok=True)
            self._write_verified(name, balls, meta["radius"], meta["width"], meta["height"], changed)
            shutil.move(jpg, os.path.join(self.ok_img, name + ".jpg"))
            os.remove(js)
        return "übernommen"

    # ---------------------------------------------------- Wie gut ist das Modell?
    def _score(self, meta, final):
        """Vorschlag des Modells mit dem gepruefte Ergebnis vergleichen: richtig ist eine
        Kugel, wenn das Modell sie an derselben Stelle mit derselben Nummer gefunden hat."""
        if not meta.get("model"):
            return                                  # ohne Modell gab es keinen Vorschlag
        r = meta["radius"]
        sugg = [dict(b, number=ball_num(b)) for b in meta.get("balls", [])]
        used, correct = set(), 0
        for b in final:
            for i, s in enumerate(sugg):
                if i not in used and (s["x"] - b["x"]) ** 2 + (s["y"] - b["y"]) ** 2 <= (0.8 * r) ** 2:
                    used.add(i)
                    correct += s["number"] == ball_num(b)
                    break
        entry = {"t": time.time(), "model": meta["model"], "balls": len(final), "correct": correct,
                 "extra": len(sugg) - len(used)}
        stats = self._load_stats()
        stats.append(entry)
        with open(os.path.join(self.base_dir, "modell-statistik.json"), "w") as f:
            json.dump(stats[-200:], f)

    def _load_stats(self):
        try:
            with open(os.path.join(self.base_dir, "modell-statistik.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def model_quality(self, last=50):
        """Trefferquote des aktuellen Modells auf den zuletzt geprueften Bildern."""
        stats = self._load_stats()
        if not stats:
            return None
        model = stats[-1]["model"]
        recent = [e for e in stats if e["model"] == model][-last:]
        total = sum(e["balls"] + e["extra"] for e in recent)
        if not total:
            return None
        return {"model": model, "images": len(recent),
                "percent": round(100 * sum(e["correct"] for e in recent) / total)}

    def _write_verified(self, name, balls, radius, w, h, changed, extra=None):
        write_yolo(os.path.join(self.ok_lbl, name + ".txt"), balls, radius, w, h)
        with open(os.path.join(self.ok_lbl, name + ".json"), "w") as f:
            json.dump(dict({"radius": radius, "width": w, "height": h, "corrected": bool(changed),
                            "balls": balls}, **(extra or {})), f)

    # ------------------------------------------- Ergebnisse einer Pruefrunde
    def add_verified(self, name, src_jpg, balls, radius, w, h, extra=None, suggestion_meta=None):
        """Pruefrunde einig: Bild + gemeinsames Ergebnis direkt als festgelegt uebernehmen."""
        with self.lock:
            os.makedirs(self.ok_img, exist_ok=True)
            os.makedirs(self.ok_lbl, exist_ok=True)
            balls = [_ball_dict(b) for b in balls]
            if suggestion_meta:
                self._score(suggestion_meta, balls)
            self._write_verified(name, balls, radius, w, h, True, extra)
            shutil.copy(src_jpg, os.path.join(self.ok_img, name + ".jpg"))

    def add_pending(self, name, src_jpg, data):
        """Pruefrunde uneinig: Bild mit Vorschlag (Mehrheit + offene Stellen) in die offene Liste,
        der Admin entscheidet."""
        with self.lock:
            os.makedirs(self.pending_dir, exist_ok=True)
            shutil.copy(src_jpg, os.path.join(self.pending_dir, name + ".jpg"))
            with open(os.path.join(self.pending_dir, name + ".json"), "w") as f:
                json.dump(data, f)

    def save_verified(self, warped, balls, radius, numbers=None):
        """Live-Bild, das im Portal korrigiert wurde: direkt als geprueft speichern."""
        numbers = numbers or [None] * len(balls)
        with self.lock:
            os.makedirs(self.ok_img, exist_ok=True)
            os.makedirs(self.ok_lbl, exist_ok=True)
            name = self._new_name()
            h, w = warped.shape[:2]
            cv2.imwrite(os.path.join(self.ok_img, name + ".jpg"), warped, [cv2.IMWRITE_JPEG_QUALITY, 92])
            self._write_verified(name, [_ball_dict(b, n) for b, n in zip(balls, numbers)], radius, w, h, True)
        return name

    def delete_all(self):
        """ALLE Trainingsbilder loeschen (offen, festgelegt, verworfen) und die Trefferquote
        zuruecksetzen. Das hochgeladene Modell bleibt. Liefert die Anzahl geloeschter Bilder."""
        with self.lock:
            n = self.pending + self.verified + self.discarded
            for folder in (self.pending_dir, os.path.dirname(self.ok_img), self.discard_dir):
                shutil.rmtree(folder, ignore_errors=True)
            try:
                os.remove(os.path.join(self.base_dir, "modell-statistik.json"))
            except OSError:
                pass
        return n

    def disk_ok(self):
        """Genug Platz auf der Speicherkarte? Darunter wird nichts mehr gesammelt."""
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            st = os.statvfs(self.base_dir)
            return st.f_bavail * st.f_frsize >= self.min_free_bytes
        except OSError:
            return False

    @property
    def discarded(self):
        return self._count(self.discard_dir, ".jpg")

    def export_all_zip(self, path):
        """ALLE Bilder vom Pi (offen, festgelegt, verworfen) mit dem, was dazu bekannt ist -
        zum Aufheben oder um spaeter woanders weiter zu beschriften."""
        n = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for folder, sub in ((self.pending_dir, "offen"), (self.ok_img, "festgelegt"),
                                (self.discard_dir, "verworfen")):
                if not os.path.isdir(folder):
                    continue
                lbl_dir = self.ok_lbl if folder == self.ok_img else folder
                for fn in sorted(os.listdir(folder)):
                    if not fn.endswith(".jpg"):
                        continue
                    z.write(os.path.join(folder, fn), f"{sub}/{fn}")
                    js = os.path.join(lbl_dir, fn[:-4] + ".json")
                    if os.path.isfile(js):
                        z.write(js, f"{sub}/{fn[:-4]}.json")
                    n += 1
        return n

    # ------------------------------------------------------------ 3. Export
    def export_zip(self, path):
        """Alle geprueften Bilder als ZIP fuer das Training (training/train_coral.py)."""
        n = 0
        self.export_skipped = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("classes.txt", "\n".join(CLASSES) + "\n")
            for fn in sorted(os.listdir(self.ok_img)) if os.path.isdir(self.ok_img) else []:
                if not fn.endswith(".jpg"):
                    continue
                stem = fn[:-4]
                js = os.path.join(self.ok_lbl, stem + ".json")
                try:
                    with open(js) as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    self.export_skipped += 1
                    continue
                if missing_numbers(meta["balls"]):
                    self.export_skipped += 1       # unvollstaendig (alte Bilder ohne Nummer)
                    continue
                z.write(os.path.join(self.ok_img, fn), f"images/{fn}")
                z.writestr(f"labels/{stem}.txt",
                           yolo_lines(meta["balls"], meta["radius"], meta["width"], meta["height"]))
                z.write(js, f"labels/{stem}.json")
                n += 1
        return n
