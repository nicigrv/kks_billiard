# Kugelmodell für den Coral trainieren

Ablauf in Kürze:

1. **Bilder aufnehmen** – per Knopf im Portal oder im Trainingsmodus (`/training`).
2. **Kugeln festlegen (iPad)** – Portal → „KI-Training: Kugeln festlegen“: Kugel antippen, Nummer
   wählen (bzw. mit Modell: Vorschlag prüfen, „Passt alles“). Nur festgelegte Bilder kommen ins
   Training; jede Kugel braucht ihre Nummer.
3. **Trainieren** – Portal → „KI-Modell (Coral)“ → „Trainingsdaten laden“ (ZIP), dann eine der
   Varianten unten.
4. **Hochladen** – die Datei `billard_edgetpu.tflite` im Portal unter „Modell hochladen“.
   Der KI-Chip nimmt sie sofort, das Portal zeigt „KI-Chip: bereit“ und mit jedem geprüften Bild,
   wie oft er richtig lag.

**Wie viele Bilder?** Brauchbar ab etwa 200–300 geprüften Bildern, besser 500+, bei
verschiedenem Licht (Tag/Abend, Lampen an/aus). Nachtrainieren mit mehr Bildern lohnt sich:
einfach alle ZIPs angeben.

## Variante A: Google Colab (empfohlen, kostenlos, mit GPU)

Der letzte Schritt (Edge-TPU-Compiler) läuft nur unter Linux – Colab ist Linux.

1. https://colab.research.google.com → Neues Notebook → Laufzeit → Laufzeittyp ändern → **GPU**.
2. Links auf das Ordner-Symbol, `train_coral.py` und die ZIP-Datei hochladen.
3. In eine Zelle schreiben und ausführen (Dateiname anpassen):

```
!pip install -q "ultralytics>=8.3,<8.5"
!python train_coral.py billard-training-20261110.zip --epochs 120 --device 0
```

4. Dauer: ca. 10–30 Minuten. Danach links `ausgabe/billard_edgetpu.tflite` herunterladen.

Am Ende steht die Genauigkeit auf den Prüfbildern (`mAP50`). Unter 0.9: mehr Bilder sammeln.

## Variante B: eigener PC

```
pip install "ultralytics>=8.3,<8.5"
python train_coral.py billard-training-20261110.zip --no-export
```

- Mit NVIDIA-Grafikkarte schnell, nur mit Prozessor dauert es Stunden.
- Unter **Linux (x86)** ohne `--no-export` – dann entsteht die Coral-Datei direkt.
- Unter **Windows/Mac** entsteht `ausgabe/best.pt`. Diese in Colab umwandeln:
  `!python train_coral.py --export-only best.pt billard-training-20261110.zip`
  (die ZIP wird für die int8-Kalibrierung gebraucht).

## Was im Modell steckt

- YOLOv8n (klein), Eingabe 640×640, **16 Klassen = Kugelnummern** (0 = Weiße, 1–15). Datei ca. 4 MB.
- Auf dem Coral laufen rund 230 von 257 Rechenschritten (alle aufwendigen); nur die letzte
  Auswertung des Netzes läuft auf dem Pi – das ist bei YOLO auf dem Coral normal.
- Die Kugeln erkennt **nur** dieses Modell. Ohne Stick oder Modell wird nichts gezählt.
- Ältere Modelle mit 4 Klassen (voll/halb) passen nicht mehr und werden abgelehnt.
- Ältere festgelegte Bilder werden beim Export übernommen, wenn bei allen Kugeln die Nummer
  bekannt ist; sonst bleiben sie liegen und werden ausgelassen.

Hinweis: Ultralytics steht unter der AGPL-3.0-Lizenz. Für die private Nutzung am eigenen
Tisch ist das kein Thema.

## Testen ohne echte Bilder

`python3 -m sim.make_training_zip 200 sim.zip` (im Ordner `app/`) erzeugt Simulator-Bilder im
selben Format. Nur zum Ausprobieren der Kette – echte Kugeln erkennt so ein Modell schlecht.
