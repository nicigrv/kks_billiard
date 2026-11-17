"""
Trainiert die Kugelerkennung (drei Stufen) und wandelt Stufe 1 fuer den Coral um.

    1. Kugeln finden   YOLOv8n mit EINER Klasse "Kugel" -> laeuft auf dem Coral
    2. gestreift?      kleines Statistik-Modell (Weiss-/Schwarz-/Farbanteil)   } laufen auf dem Pi
    3. Farbe           kleines Statistik-Modell (mittlere Farbe der Kugel)      } (kugel_merkmale.py)

    pip install "ultralytics>=8.3,<8.5"   (getestet mit 8.4)
    python train_coral.py billard-training-20261110.zip
    -> ausgabe/billard_edgetpu.tflite   (Stufe 1)       } beide im Admin-Portal unter
    -> ausgabe/billard_merkmale.json    (Stufe 2 + 3)   } "KI-Modell (Coral)" hochladen

    python train_coral.py --nur-farben billard-training-20261110.zip
    -> nur Stufe 2 + 3 (Sekunden, ohne Grafikkarte und ohne ultralytics)

Braucht kugel_merkmale.py (liegt in app/; in Google Colab mit hochladen).

Eingabe: die ZIP-Datei aus dem Admin-Portal ("Trainingsdaten laden"), mehrere ZIPs
oder entpackte Ordner gehen auch (z. B. von mehreren Abenden). Benutzt werden nur
Bilder, die auf dem iPad geprueft wurden. In den Labels steht die Kugelnummer
(0 = Weisse, 1-7 Volle, 8, 9-15 Halbe); fuer Stufe 1 wird daraus "Kugel".

Der letzte Schritt (Edge-TPU-Compiler) gibt es nur fuer Linux auf x86 - am einfachsten
in Google Colab (siehe README.md). Auf Windows/Mac: mit --no-export trainieren und
die Datei best.pt in Colab umwandeln (--export-only best.pt).
"""

import argparse
import hashlib
import platform
import shutil
import sys
import zipfile
from pathlib import Path

CLASSES = ["kugel"]                  # Stufe 1 findet nur Kugeln; die Nummer machen Stufe 2 + 3


def load_merkmale():
    """kugel_merkmale.py aus app/ (Repo) oder aus demselben Ordner (Colab) laden."""
    here = Path(__file__).resolve().parent
    for p in (here, here.parent / "app", Path.cwd()):
        if (p / "kugel_merkmale.py").is_file() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        import kugel_merkmale
        return kugel_merkmale
    except ImportError:
        return None


def read_labels(lbl):
    """YOLO-Label mit Kugelnummern -> [(nummer, cx, cy, w, h)] (normiert)."""
    out = []
    for line in Path(lbl).read_text().splitlines():
        p = line.split()
        if len(p) == 5:
            out.append((int(float(p[0])), *map(float, p[1:])))
    return out


def collect(sources, work):
    """Alle Bild/Beschriftungs-Paare aus ZIPs und Ordnern einsammeln."""
    pairs = []
    for i, src in enumerate(sources):
        src = Path(src)
        if src.is_file() and src.suffix.lower() == ".zip":
            dest = work / f"quelle{i}"
            if dest.exists():
                shutil.rmtree(dest)
            with zipfile.ZipFile(src) as z:
                z.extractall(dest)
            src = dest
        if not src.is_dir():
            sys.exit(f"Nicht gefunden: {src}")
        for img in sorted(src.rglob("*.jpg")):
            if img.parent.name != "images":
                continue
            lbl = img.parent.parent / "labels" / (img.stem + ".txt")
            if lbl.is_file():
                pairs.append((img, lbl))
    # gleiche Bilder (mehrfach exportiert) nur einmal
    unique = {}
    for img, lbl in pairs:
        unique.setdefault(img.name, (img, lbl))
    return list(unique.values())


def split(pairs, val_share):
    """Feste Aufteilung (gleiches Bild landet immer im gleichen Teil)."""
    train, val = [], []
    for img, lbl in pairs:
        h = int(hashlib.md5(img.name.encode()).hexdigest(), 16) % 1000
        (val if h < val_share * 1000 else train).append((img, lbl))
    if not val:
        val.append(train.pop())
    return train, val


def build_dataset(pairs, work, val_share):
    ds = work / "dataset"
    if ds.exists():
        shutil.rmtree(ds)
    train, val = split(pairs, val_share)
    for part, items in (("train", train), ("val", val)):
        (ds / "images" / part).mkdir(parents=True)
        (ds / "labels" / part).mkdir(parents=True)
        for img, lbl in items:
            shutil.copy(img, ds / "images" / part / img.name)
            # Nummer -> Klasse 0 ("Kugel")
            lines = [f"0 {cx:.5f} {cy:.5f} {w:.5f} {h:.5f}" for _, cx, cy, w, h in read_labels(lbl)]
            (ds / "labels" / part / lbl.name).write_text("\n".join(lines) + ("\n" if lines else ""))
    yaml = ds / "data.yaml"
    names = "\n".join(f"  {i}: {n}" for i, n in enumerate(CLASSES))
    yaml.write_text(f"path: {ds.resolve()}\ntrain: images/train\nval: images/val\nnames:\n{names}\n")
    return yaml, len(train), len(val)


def train_merkmale(pairs, val_share, out, km):
    """Stufe 2 + 3: aus jeder festgelegten Kugel Merkmale holen, lernen, auf den Pruefbildern pruefen."""
    import cv2

    def samples(items):
        for img_path, lbl in items:
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            H, W = img.shape[:2]
            labels = read_labels(lbl)
            if not labels:
                continue
            r = sum(w * W for _, _, _, w, _ in labels) / len(labels) / 2
            yield img, [(cx * W, cy * H, n) for n, cx, cy, _, _ in labels], r

    train, val = split(pairs, val_share)
    try:
        return _train_merkmale(samples, train, val, pairs, out, km)
    except ValueError as e:
        print(f"[!] Stufe 2 + 3 nicht gelernt: {e}. Mehr festgelegte Bilder mit allen Kugeln sammeln.")
        return None


def _train_merkmale(samples, train, val, pairs, out, km):
    start = None
    start_path = Path(km.__file__).resolve().parent / "models" / "start_merkmale.json"
    if start_path.is_file():
        start = km.BallClassifier.load(str(start_path))
    print("\nStufe 2 + 3 (gestreift? Farbe?) lernen ...")
    clf_check = km.fit(km.collect(samples(train)), start, "Pruefung")
    res = km.evaluate(clf_check, km.collect(samples(val)))
    print(km.report(res))
    clf = km.fit(km.collect(samples(train + val)), start, f"train_coral.py, {len(pairs)} Bilder")
    clf.info["pruefung"] = {k: (round(v, 3) if isinstance(v, float) else v)
                            for k, v in res.items() if k != "verwechslungen"}
    if clf.info.get("geerbt"):
        print("[!] Zu wenig Beispiele (Startwerte behalten): " + ", ".join(clf.info["geerbt"]))
    out.mkdir(parents=True, exist_ok=True)
    target = out / "billard_merkmale.json"
    clf.save(str(target))
    return target


def export_edgetpu(weights, yaml, imgsz, out):
    from ultralytics import YOLO
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        print("\n[!] Der Edge-TPU-Compiler laeuft nur auf Linux (x86). In Google Colab umwandeln:")
        print(f"    python train_coral.py --export-only {weights}")
        return None
    path = YOLO(str(weights)).export(format="edgetpu", imgsz=imgsz, data=str(yaml) if yaml else None)
    found = Path(path) if path else None
    if found is None or not found.is_file():
        cands = sorted(Path(weights).parent.rglob("*_edgetpu.tflite"))
        found = cands[-1] if cands else None
    if found is None:
        sys.exit("Umwandlung fehlgeschlagen: keine *_edgetpu.tflite gefunden")
    out.mkdir(parents=True, exist_ok=True)
    target = out / "billard_edgetpu.tflite"
    shutil.copy(found, target)
    return target


def main():
    ap = argparse.ArgumentParser(description="Kugelmodell fuer den Coral trainieren")
    ap.add_argument("data", nargs="*", help="ZIP(s) aus dem Admin-Portal oder entpackte Ordner")
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--imgsz", type=int, default=640, help="Eingabegroesse (Kugeln ~18 px bei 640)")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--model", default="yolov8n.pt", help="Startmodell (vortrainiert)")
    ap.add_argument("--val", type=float, default=0.15, help="Anteil der Bilder zum Pruefen")
    ap.add_argument("--work", default="arbeit")
    ap.add_argument("--out", default="ausgabe")
    ap.add_argument("--device", default=None, help="z. B. 0 fuer die erste GPU, cpu fuer Prozessor")
    ap.add_argument("--no-export", action="store_true", help="nur trainieren, nicht fuer den Coral umwandeln")
    ap.add_argument("--export-only", metavar="BEST_PT", help="nur ein trainiertes best.pt umwandeln")
    ap.add_argument("--nur-farben", action="store_true",
                    help="nur Stufe 2 + 3 (gestreift? Farbe?) lernen - schnell, ohne Grafikkarte")
    args = ap.parse_args()

    work, out = Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    km = load_merkmale()

    if args.nur_farben:
        if not args.data:
            ap.error("Trainingsdaten (ZIP) angeben")
        if km is None:
            sys.exit("kugel_merkmale.py nicht gefunden (aus app/ neben dieses Skript legen)")
        pairs = collect(args.data, work)
        if not pairs:
            sys.exit("Keine geprueften Bilder gefunden (Ordner images/ + labels/).")
        target = train_merkmale(pairs, args.val, out, km)
        print(f"\nFertig: {target}  -> im Admin-Portal unter 'KI-Modell (Coral)' hochladen")
        return

    from ultralytics import YOLO

    if args.export_only:
        yaml = None
        if args.data:
            pairs = collect(args.data, work)
            yaml, _, _ = build_dataset(pairs, work, args.val)   # Bilder fuer die int8-Kalibrierung
        target = export_edgetpu(Path(args.export_only), yaml, args.imgsz, out)
        if target:
            print(f"\nFertig: {target}  -> im Admin-Portal hochladen")
        return

    if not args.data:
        ap.error("Trainingsdaten (ZIP) angeben")
    pairs = collect(args.data, work)
    if not pairs:
        sys.exit("Keine geprueften Bilder gefunden (Ordner images/ + labels/).")
    if km is None:
        print("[!] kugel_merkmale.py nicht gefunden - Stufe 2 + 3 (Farben) werden NICHT gelernt.\n"
              "    Datei aus app/ neben train_coral.py legen (in Colab mit hochladen).")
    yaml, n_train, n_val = build_dataset(pairs, work, args.val)
    print(f"{len(pairs)} Bilder: {n_train} zum Lernen, {n_val} zum Pruefen")
    merkmale = train_merkmale(pairs, args.val, out, km) if km else None
    if len(pairs) < 150:
        print("[!] Wenig Bilder. Brauchbar wird es meist ab 200-300 geprueften Bildern "
              "bei verschiedenem Licht. Es geht trotzdem los.")

    model = YOLO(args.model)
    model.train(
        data=str(yaml), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
        project=str(work.resolve()), name="train", exist_ok=True, device=args.device,
        patience=40, plots=False,
        # Tisch von oben: spiegeln ist erlaubt (nur "Kugel ja/nein", Ziffern egal),
        # drehen/scheren nicht (Kamera ist fest)
        fliplr=0.5, flipud=0.5, degrees=0.0, shear=0.0, perspective=0.0,
        scale=0.15, translate=0.05, mosaic=1.0, close_mosaic=15,
        # Farbe ist fuer Stufe 1 egal: Farbton/Helligkeit/Saettigung stark variieren
        # -> findet Kugeln jeder Farbe auch bei anderem Licht
        hsv_h=0.1, hsv_s=0.6, hsv_v=0.4,
    )
    best = work / "train" / "weights" / "best.pt"
    metrics = YOLO(str(best)).val(data=str(yaml), imgsz=args.imgsz, plots=False, device=args.device)
    print(f"\nStufe 1 (Kugeln finden) auf den Pruefbildern: mAP50 = {metrics.box.map50:.3f}, "
          f"gefunden (Recall) {metrics.box.mr:.2f}, richtig (Precision) {metrics.box.mp:.2f} "
          f"(1.0 = perfekt; unter 0.95 lieber mehr Bilder sammeln)")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, out / "best.pt")

    if args.no_export:
        print(f"\nTrainiert: {out / 'best.pt'}. Umwandeln: python train_coral.py --export-only {out / 'best.pt'}")
        return
    target = export_edgetpu(best, yaml, args.imgsz, out)
    if target:
        print(f"\nFertig: {target}" + (f" und {merkmale}" if merkmale else "")
              + "  -> beide im Admin-Portal unter 'KI-Modell (Coral)' hochladen")


if __name__ == "__main__":
    main()
