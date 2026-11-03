"""
Trainiert das Kugelmodell fuer den Coral-Stick und wandelt es fuer den Coral um.

    pip install ultralytics
    python train_coral.py billard-training-20261110.zip
    -> ausgabe/billard_edgetpu.tflite      im Admin-Portal unter "KI-Modell (Coral)" hochladen

Eingabe: die ZIP-Datei aus dem Admin-Portal ("Trainingsdaten laden"), mehrere ZIPs
oder entpackte Ordner gehen auch (z. B. von mehreren Abenden). Benutzt werden nur
Bilder, die auf dem iPad geprueft wurden.

Modell: YOLOv8n (klein, laeuft gut auf dem Coral), 4 Klassen in fester Reihenfolge:
    0 cue (Weisse), 1 eight (8), 2 solid (Volle), 3 stripe (Halbe)
Die Kugelfarbe/-nummer bestimmt auf dem Pi weiterhin die klassische Bildverarbeitung.

Der letzte Schritt (Edge-TPU-Compiler) gibt es nur fuer Linux auf x86 - am einfachsten
in Google Colab (siehe README.md). Auf Windows/Mac: mit --no-export trainieren und
die Datei best.pt in Colab umwandeln (--export-only best.pt).
"""

import argparse
import hashlib
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path

CLASSES = ["cue", "eight", "solid", "stripe"]


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
            shutil.copy(lbl, ds / "labels" / part / lbl.name)
    yaml = ds / "data.yaml"
    names = "\n".join(f"  {i}: {n}" for i, n in enumerate(CLASSES))
    yaml.write_text(f"path: {ds.resolve()}\ntrain: images/train\nval: images/val\nnames:\n{names}\n")
    return yaml, len(train), len(val)


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
    args = ap.parse_args()

    from ultralytics import YOLO

    work, out = Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)

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
    yaml, n_train, n_val = build_dataset(pairs, work, args.val)
    print(f"{len(pairs)} Bilder: {n_train} zum Lernen, {n_val} zum Pruefen")
    if len(pairs) < 150:
        print("[!] Wenig Bilder. Brauchbar wird es meist ab 200-300 geprueften Bildern "
              "bei verschiedenem Licht. Es geht trotzdem los.")

    model = YOLO(args.model)
    model.train(
        data=str(yaml), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
        project=str(work.resolve()), name="train", exist_ok=True, device=args.device,
        patience=40, plots=False,
        # Tisch von oben: spiegeln ist erlaubt, drehen/scheren nicht (Kamera ist fest)
        fliplr=0.5, flipud=0.5, degrees=0.0, shear=0.0, perspective=0.0,
        scale=0.15, translate=0.05, mosaic=1.0, close_mosaic=15,
        # Licht schwankt: Helligkeit/Saettigung variieren, Farbton nur wenig
        hsv_h=0.01, hsv_s=0.5, hsv_v=0.4,
    )
    best = work / "train" / "weights" / "best.pt"
    metrics = YOLO(str(best)).val(data=str(yaml), imgsz=args.imgsz, plots=False, device=args.device)
    print(f"\nGenauigkeit auf den Pruefbildern: mAP50 = {metrics.box.map50:.3f} "
          f"(1.0 = perfekt; unter 0.9 lieber mehr Bilder sammeln)")
    for i, n in enumerate(CLASSES):
        try:
            print(f"  {n:7s} mAP50 {metrics.box.ap50[i]:.3f}")
        except (IndexError, AttributeError):
            pass
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, out / "best.pt")

    if args.no_export:
        print(f"\nTrainiert: {out / 'best.pt'}. Umwandeln: python train_coral.py --export-only {out / 'best.pt'}")
        return
    target = export_edgetpu(best, yaml, args.imgsz, out)
    if target:
        print(f"\nFertig: {target}  -> im Admin-Portal unter 'KI-Modell (Coral)' hochladen")


if __name__ == "__main__":
    main()
