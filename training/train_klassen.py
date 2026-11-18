"""
Stufe 2 + 3 der Kugelerkennung als kleines neuronales Netz fuer den KI-Chip (Coral).

Bisher bestimmt der Pi Art (Weisse / 8 / voll / halb) und Farbe jeder Kugel mit Statistik auf dem
Prozessor (kugel_merkmale.py). Dieses Skript trainiert stattdessen ein kleines CNN, das aus einem
Ausschnitt um die Kugel direkt die Nummer 0-15 bestimmt - und das laeuft auf dem Coral.

    python train_klassen.py billard-training-20261118.zip [weitere.zip ...]
    -> ausgabe/billard_klassen_edgetpu.tflite   im Admin-Panel unter "Modell / Farben hochladen"

Braucht TensorFlow (in Google Colab schon da) und kugel_merkmale.py (aus app/, in Colab mit hochladen).
Der Edge-TPU-Compiler laeuft nur auf Linux x86 (Colab). Fehlt er, entsteht billard_klassen.tflite:
das laeuft auch, aber auf dem Prozessor des Pi statt auf dem Coral. Alle Rechenschritte des Netzes
(Conv, ReLU6, MaxPool, Mittelwert, Dense, Softmax) kann der Coral; Ein-/Ausgabe sind int8 wie bei Stufe 1.

Daten: dieselbe ZIP wie fuer train_coral.py (festgelegte Bilder vom Tisch + Einzelkugel-Bilder).
Jede festgelegte Kugel wird ausgeschnitten und mehrfach leicht veraendert (gedreht, gespiegelt,
etwas verschoben, heller/dunkler) - Kugeln liegen in jeder Drehung, die Erkennung trifft die Mitte
nie ganz genau. Die Farbe selbst wird kaum veraendert: sie unterscheidet rot / orange / weinrot.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from train_coral import collect, load_merkmale, read_labels, split  # noqa: E402

SIZE = 40            # Eingabe des Netzes: SIZE x SIZE Pixel (muss zu app/coral_detector.py passen: liest es aus dem Modell)
PAD = 1.6            # erst groesser ausschneiden, damit Drehen/Verschieben keine Raender erzeugt


def ball_crops(km, pairs, size, pad=1.0):
    """-> (Ausschnitte uint8 BGR [n, s, s, 3], Nummern [n]). s = size * pad."""
    import cv2
    X, y = [], []
    big = int(round(size * pad))
    for img_path, lbl in pairs:
        img = cv2.imread(str(img_path))
        if img is None:
            continue
        H, W = img.shape[:2]
        labels = read_labels(lbl)
        if not labels:
            continue
        r = sum(w * W for _, _, _, w, _ in labels) / len(labels) / 2
        for n, cx, cy, _, _ in labels:
            if not 0 <= n <= 15:
                continue
            X.append(km.crop(img, cx * W, cy * H, r * pad, out=big))
            y.append(n)
    return np.array(X, np.uint8).reshape(-1, big, big, 3), np.array(y, np.int64)


def augment(X, size, rng, copies):
    """Jede Kugel copies-mal: drehen (beliebig), spiegeln, verschieben (+-12 % des Radius),
    Groesse +-8 %, Helligkeit/Kontrast +-15 %, leichter Farbstich +-4 %, Rauschen."""
    import cv2
    big = X.shape[1]
    out = np.empty((len(X) * copies, size, size, 3), np.uint8)
    k = 0
    for img in X:
        for _ in range(copies):
            ang = rng.uniform(0, 360)
            sc = size / big * PAD * rng.uniform(0.92, 1.08)
            shift = rng.normal(0, 0.06, 2) * size
            M = cv2.getRotationMatrix2D((big / 2, big / 2), ang, sc)
            M[:, 2] += (size / 2 - big / 2) + shift
            a = cv2.warpAffine(img, M, (size, size), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            if rng.random() < 0.5:
                a = a[:, ::-1]
            a = a.astype(np.float32)
            a = (a - 128) * rng.uniform(0.85, 1.15) + 128 + rng.uniform(-20, 20)
            a *= rng.uniform(0.96, 1.04, 3)
            a += rng.normal(0, 3, a.shape)
            out[k] = np.clip(a, 0, 255).astype(np.uint8)
            k += 1
    return out


def center(X, size):
    """Ohne Zufall: nur auf SIZE bringen (Pruefbilder, Kalibrierung)."""
    import cv2
    big = X.shape[1]
    c = (big - int(round(big / PAD))) // 2
    inner = X[:, c:big - c, c:big - c]
    return np.array([cv2.resize(i, (size, size), interpolation=cv2.INTER_AREA) for i in inner], np.uint8)


def to_input(X):
    """BGR uint8 -> RGB float 0..1 (genau so rechnet der Pi: Eingabe-Skala 1/255)."""
    return X[..., ::-1].astype(np.float32) / 255.0


def build_model(size, tf):
    L = tf.keras.layers
    reg = tf.keras.regularizers.l2(1e-4)

    def conv(x, f):
        x = L.Conv2D(f, 3, padding="same", use_bias=False, kernel_regularizer=reg)(x)
        x = L.BatchNormalization(momentum=0.9)(x)
        return L.ReLU(max_value=6.0)(x)

    inp = L.Input((size, size, 3))
    x = conv(inp, 16)
    x = conv(x, 16)
    x = L.MaxPooling2D()(x)            # 20
    x = conv(x, 32)
    x = conv(x, 32)
    x = L.MaxPooling2D()(x)            # 10
    x = conv(x, 64)
    x = conv(x, 64)
    x = L.MaxPooling2D()(x)            # 5
    x = conv(x, 96)
    x = L.GlobalAveragePooling2D()(x)
    x = L.Dropout(0.25)(x)
    out = L.Dense(16, activation="softmax")(x)
    return tf.keras.Model(inp, out)


def report(km, y_true, probs, title):
    pred = probs.argmax(1)
    kinds = np.array([km.split_number(n)[0] for n in y_true])
    kinds_p = np.array([km.split_number(n)[0] for n in pred])
    col = np.array([km.split_number(n)[1] or "" for n in y_true])
    col_p = np.array([km.split_number(n)[1] or "" for n in pred])
    has_c = col != ""
    conf = {}
    for a, b in zip(y_true, pred):
        if a != b:
            conf[(int(a), int(b))] = conf.get((int(a), int(b)), 0) + 1
    top = sorted(conf.items(), key=lambda kv: -kv[1])[:8]
    print(f"{title}: {len(y_true)} Kugeln - Art richtig {np.mean(kinds == kinds_p) * 100:.0f} %, "
          f"Farbe richtig {np.mean(col[has_c] == col_p[has_c]) * 100:.0f} %, "
          f"Nummer richtig {np.mean(y_true == pred) * 100:.0f} %")
    if top:
        print("  häufigste Verwechslungen: " + ", ".join(f"{a}→{b} ({k}x)" for (a, b), k in top))
    return float(np.mean(y_true == pred))


def compile_edgetpu(src, out_dir):
    """edgetpu_compiler aufrufen (Linux x86). In Colab wird er bei Bedarf installiert."""
    if shutil.which("edgetpu_compiler") is None and sys.platform.startswith("linux"):
        print("Edge-TPU-Compiler fehlt - versuche ihn zu installieren (wie in Colab) ...")
        cmd = ("curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | "
               "gpg --dearmor -o /usr/share/keyrings/coral-edgetpu.gpg && "
               "echo 'deb [signed-by=/usr/share/keyrings/coral-edgetpu.gpg] https://packages.cloud.google.com/apt "
               "coral-edgetpu-stable main' > /etc/apt/sources.list.d/coral-edgetpu.list && "
               "apt-get update -qq && apt-get install -y -qq edgetpu-compiler")
        if subprocess.run(cmd, shell=True).returncode != 0:
            subprocess.run(f"sudo sh -c \"{cmd}\"", shell=True)
    if shutil.which("edgetpu_compiler") is None:
        return None
    r = subprocess.run(["edgetpu_compiler", "-s", "-o", str(out_dir), str(src)], capture_output=True, text=True)
    print(r.stdout[-1500:])
    if r.returncode != 0:
        print(r.stderr[-800:])
        return None
    compiled = out_dir / (src.stem + "_edgetpu.tflite")
    return compiled if compiled.is_file() else None


def main():
    ap = argparse.ArgumentParser(description="Stufe 2 + 3 (Art und Farbe) als Coral-Modell trainieren")
    ap.add_argument("data", nargs="+", help="ZIP(s) aus dem Admin-Panel / der Trainingsseite oder Ordner")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--kopien", type=int, default=6, help="veraenderte Kopien je Kugel im Training")
    ap.add_argument("--val", type=float, default=0.15, help="Anteil der Bilder zum Pruefen")
    ap.add_argument("--size", type=int, default=SIZE)
    ap.add_argument("--work", default="arbeit")
    ap.add_argument("--out", default="ausgabe")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    import tensorflow as tf
    km = load_merkmale()
    if km is None:
        sys.exit("kugel_merkmale.py nicht gefunden (aus app/ neben dieses Skript legen)")
    work, out = Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    tf.keras.utils.set_random_seed(args.seed)

    pairs = collect(args.data, work)
    if not pairs:
        sys.exit("Keine festgelegten Bilder gefunden (Ordner images/ + labels/).")
    train, val = split(pairs, args.val)
    Xtr_big, ytr = ball_crops(km, train, args.size, PAD)
    Xva_big, yva = ball_crops(km, val, args.size, PAD)
    counts = np.bincount(ytr, minlength=16)
    print(f"{len(pairs)} Bilder: {len(ytr)} Kugeln zum Lernen, {len(yva)} zum Pruefen")
    print("Kugeln je Nummer (Lernen): " + ", ".join(f"{n}: {c}" for n, c in enumerate(counts)))
    if (counts < 10).any():
        print("[!] Wenig Beispiele für: " + ", ".join(str(n) for n in np.nonzero(counts < 10)[0])
              + " - mehr Einzelkugel-Bilder davon aufnehmen")

    t0 = time.time()
    Xtr = augment(Xtr_big, args.size, rng, args.kopien)
    ytr_a = np.repeat(ytr, args.kopien)
    Xva = center(Xva_big, args.size)
    # seltene Nummern staerker gewichten (Einzelkugel-Serien sind ungleich verteilt)
    w = counts.sum() / np.maximum(counts, 1) / 16
    w = np.clip(w, 0.2, 5.0)
    model = build_model(args.size, tf)
    model.compile(optimizer=tf.keras.optimizers.Adam(2e-3), loss="sparse_categorical_crossentropy",
                  metrics=["accuracy"])
    cb = [tf.keras.callbacks.EarlyStopping("val_accuracy", patience=8, restore_best_weights=True),
          tf.keras.callbacks.ReduceLROnPlateau("val_loss", factor=0.5, patience=4)]
    model.fit(to_input(Xtr), ytr_a, sample_weight=w[ytr_a], validation_data=(to_input(Xva), yva),
              epochs=args.epochs, batch_size=128, callbacks=cb, verbose=2)
    print(f"Training: {time.time() - t0:.0f} s")
    print()
    report(km, yva, model.predict(to_input(Xva), verbose=0), "Prüfbilder, neues Netz")

    # Vergleich: bisherige Statistik (QDA) auf denselben Pruefbildern
    try:
        def samples(items):
            import cv2
            for img_path, lbl in items:
                img = cv2.imread(str(img_path))
                labels = read_labels(lbl) if img is not None else []
                if not labels:
                    continue
                H, W = img.shape[:2]
                r = sum(w_ * W for _, _, _, w_, _ in labels) / len(labels) / 2
                yield img, [(cx * W, cy * H, n) for n, cx, cy, _, _ in labels], r
        clf = km.fit(km.collect(samples(train)))
        print(km.report(km.evaluate(clf, km.collect(samples(val))), "Prüfbilder, bisherige Statistik"))
    except (ValueError, np.linalg.LinAlgError) as e:
        print(f"(Vergleich mit der Statistik nicht möglich: {e})")

    # int8 fuer den Coral: Ein- und Ausgabe int8 (wie das Stufe-1-Modell aus Ultralytics), Eingabe 0..1
    calib = to_input(np.concatenate([center(Xtr_big[rng.permutation(len(Xtr_big))[:400]], args.size)]))

    def rep():
        for x in calib:
            yield [x[None]]
    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = rep
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    tfl = out / "billard_klassen.tflite"
    tfl.write_bytes(conv.convert())

    # int8-Modell pruefen (so rechnet auch der Pi)
    it = tf.lite.Interpreter(model_path=str(tfl))
    it.allocate_tensors()
    i_d, o_d = it.get_input_details()[0], it.get_output_details()[0]
    s_in, z_in = i_d["quantization"]
    print(f"Eingabe {list(i_d['shape'])}, Skala {s_in:.6f}, Nullpunkt {z_in}")
    s_out, z_out = o_d["quantization"]
    info = np.iinfo(i_d["dtype"])
    probs = []
    for x in center(Xva_big, args.size):
        q = np.clip(np.round(x[..., ::-1].astype(np.float32) / 255.0 / s_in + z_in), info.min, info.max)
        it.set_tensor(i_d["index"], q.astype(i_d["dtype"])[None])
        it.invoke()
        probs.append((it.get_tensor(o_d["index"])[0].astype(np.float32) - z_out) * s_out)
    report(km, yva, np.array(probs), "Prüfbilder, int8 (wie auf dem Coral)")

    compiled = compile_edgetpu(tfl, out)
    if compiled:
        target = out / "billard_klassen_edgetpu.tflite"
        if compiled != target:
            shutil.move(str(compiled), target)
        print(f"\nFertig: {target}  -> im Admin-Panel unter 'Modell / Farben hochladen'")
    else:
        print(f"\n[!] Kein Edge-TPU-Compiler (nur Linux x86, z. B. Colab). {tfl} geht auch - "
              "läuft dann aber auf dem Prozessor des Pi. In Colab umwandeln: "
              f"!edgetpu_compiler -s {tfl.name}")


if __name__ == "__main__":
    main()
