# Kugelmodell für den Coral trainieren

Ablauf in Kürze:

1. **Bilder sammeln** – läuft von selbst: Nach jedem Spielzug speichert der Pi ein Bild und
   ordnet alle Kugeln zu.
2. **Auf dem iPad prüfen** – Portal → „KI-Training: Bilder prüfen“: „Passt alles“, korrigieren
   oder verwerfen. Nur geprüfte Bilder kommen ins Training.
3. **Trainieren** – Portal → „KI-Modell (Coral)“ → „Trainingsdaten laden“ (ZIP), dann eine der
   Varianten unten.
4. **Hochladen** – die Datei `billard_edgetpu.tflite` im Portal unter „Modell hochladen“.
   Der Coral nimmt sie sofort, das Portal zeigt „Erkennung läuft mit: Coral“.

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

- YOLOv8n (klein), Eingabe 640×640, Klassen: Weiße, 8, Volle, Halbe.
- Die Kugel**nummer** (Farbe) und die genaue Mitte bestimmt auf dem Pi weiter die klassische
  Bildverarbeitung; ebenso, ob jemand über dem Tisch hängt.
- Ohne Modell, ohne Stick oder bei einem Fehler nimmt der Pi automatisch die klassische
  Erkennung. Im Portal lässt sich das auch fest einstellen („Nur OpenCV“), z. B. zum Vergleichen.

Hinweis: Ultralytics steht unter der AGPL-3.0-Lizenz. Für die private Nutzung am eigenen
Tisch ist das kein Thema.

## Testen ohne echte Bilder

`python3 -m sim.make_training_zip 200 sim.zip` (im Ordner `app/`) erzeugt Simulator-Bilder im
selben Format. Nur zum Ausprobieren der Kette – echte Kugeln erkennt so ein Modell schlecht.
