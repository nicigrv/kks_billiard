# Kugelerkennung trainieren

Die Erkennung hat drei Stufen:

1. **Kugeln finden** – YOLOv8n mit *einer* Klasse („Kugel“) auf dem Coral → `billard_edgetpu.tflite`
2. **gestreift?** – Weiße / schwarze 8 / voll / halb (Weiß-, Schwarz- und Farbanteil)  } auf dem Pi,
3. **Farbe** – gelb, blau, rot, violett, orange, grün, weinrot (mittlere Farbe)           } `billard_merkmale.json`

`train_coral.py` macht beides. Stufe 2 + 3 allein dauern nur Sekunden (`--nur-farben`, ohne
Grafikkarte) – oder direkt im Admin-Panel: „Farben aus festgelegten Bildern lernen“.

Ablauf in Kürze:

1. **Bilder speichern** – am Tisch im Spiel-Panel („Bild speichern“ oder automatisch nach jedem
   Stoß), im Admin-Panel oder im Trainingsmodus (`/training`). Festgelegt wird später in Ruhe:
   Prüfer unter `/pruefen` (jeder bekommt ein anderes Bild) oder der Admin unter `/training`.
2. **Kugeln festlegen (iPad)** – Admin-Panel → „KI-Training: Kugeln festlegen“: Kugel antippen, Nummer
   wählen (bzw. mit Modell: Vorschlag prüfen, „Passt alles“). Nur festgelegte Bilder kommen ins
   Training; jede Kugel braucht ihre Nummer.
3. **Trainieren** – Admin-Panel → „KI-Modell (Coral)“ → „Trainingsdaten laden“ (ZIP), dann eine der
   Varianten unten.
4. **Hochladen** – `billard_edgetpu.tflite` und `billard_merkmale.json` im Admin-Panel unter
   „Modell / Farben hochladen“ (beide auf einmal). Der Pi nimmt sie sofort, das Admin-Panel zeigt den
   Stand aller drei Stufen und mit jedem geprüften Bild, wie oft die Erkennung richtig lag.

**Wie viele Bilder?** Brauchbar ab etwa 200–300 geprüften Bildern, besser 500+, bei
verschiedenem Licht (Tag/Abend, Lampen an/aus). Nachtrainieren mit mehr Bildern lohnt sich:
einfach alle ZIPs angeben.

## Variante A: Google Colab (empfohlen, kostenlos, mit GPU)

Der letzte Schritt (Edge-TPU-Compiler) läuft nur unter Linux – Colab ist Linux.

1. https://colab.research.google.com → Neues Notebook → Laufzeit → Laufzeittyp ändern → **GPU**.
2. Links auf das Ordner-Symbol, `train_coral.py`, **`kugel_merkmale.py`** (aus `app/`) und die
   ZIP-Datei hochladen. Optional `start_merkmale.json` (aus `app/models/`) in einen Ordner `models/`
   – dann behalten Kugeln mit zu wenig Beispielen die Startwerte statt eines Fehlers.
3. In eine Zelle schreiben und ausführen (Dateiname anpassen):

```
!pip install -q "ultralytics>=8.3,<8.5"
!python train_coral.py billard-training-20261110.zip --epochs 120 --device 0
```

4. Dauer: ca. 10–30 Minuten. Danach links `ausgabe/billard_edgetpu.tflite` und
   `ausgabe/billard_merkmale.json` herunterladen.

Am Ende steht für Stufe 1 die Genauigkeit auf den Prüfbildern (`mAP50`, unter 0.95: mehr Bilder) und
für Stufe 2 + 3, wie viele Kugeln Art, Farbe und Nummer richtig hatten – mit den häufigsten
Verwechslungen (z. B. `11→3`: halbe rote als volle rote gelesen).

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

## Variante C: nur Farben (Stufe 2 + 3), Sekunden

Im Repo-Ordner `training/` (braucht nur `numpy` und `opencv-python`, keine Grafikkarte):

```
python train_coral.py --nur-farben billard-training-20261110.zip
```

→ `ausgabe/billard_merkmale.json` hochladen. Lohnt sich nach jedem neuen Schwung festgelegter Bilder
oder wenn das Licht anders ist. Noch einfacher: Admin-Panel → „Farben aus festgelegten Bildern lernen“.

## Variante D: Stufe 2 + 3 auf dem KI-Chip (entlastet den Pi)

Statt Statistik auf dem Prozessor bestimmt ein kleines neuronales Netz auf dem Coral Art und Farbe:
Der Pi schneidet um jede gefundene Kugel einen kleinen Ausschnitt aus (40×40 Pixel), der Coral
liefert direkt die Wahrscheinlichkeit für jede Nummer 0–15. Gelernt wird aus derselben ZIP –
die **Einzelkugel-Bilder** sind dafür besonders gut (viele Beispiele je Nummer).

**Am schnellsten – eine Zelle in Colab** (holt Code und ZIP selbst; den `EXPORT_KEY` zeigt
`update.sh server` auf dem VPS an):

```
EXPORT_KEY = "hier-den-schluessel-einfuegen"
!rm -rf kks && git clone -q --depth 1 https://github.com/nicigrv/kks_billiard kks
%cd kks/training
!curl -fL -H "X-Export-Key: {EXPORT_KEY}" -o daten.zip https://billard.server.greylo.de/api/export.zip
!python train_klassen.py daten.zip
import glob; from google.colab import files; [files.download(f) for f in glob.glob("ausgabe/billard_klassen*.tflite")]
```

Oder von Hand – in Colab (TensorFlow ist dort schon installiert, eine GPU hilft, ist aber nicht nötig):

1. `train_klassen.py`, `train_coral.py`, **`kugel_merkmale.py`** (aus `app/`) und die ZIP(s) hochladen.
2. ```
   !python train_klassen.py billard-training-20261118.zip
   ```
3. Dauer: wenige Minuten. Am Ende steht für die Prüfbilder, wie oft Art, Farbe und Nummer stimmen –
   **neues Netz und bisherige Statistik nebeneinander**, damit ihr seht, ob es sich lohnt.
4. `ausgabe/billard_klassen_edgetpu.tflite` herunterladen und im Admin-Panel unter
   „Modell / Farben hochladen“ hochladen. Im Admin-Panel steht dann bei Stufe 2 + 3 „KI-Chip“.

- Den Edge-TPU-Compiler (16.0) lädt das Skript selbst herunter (Googles apt-Quelle gibt es nicht mehr).
  Ohne Compiler (nicht Linux x86) entsteht nur `billard_klassen.tflite`. Das läuft auch,
  aber auf dem Prozessor des Pi. In Colab umwandeln: `!edgetpu_compiler -s billard_klassen.tflite`.
- Fehlt das Modell oder lädt es nicht, rechnet der Pi automatisch wieder mit der Statistik
  (`billard_merkmale.json`). „Klassen-Modell löschen“ im Admin-Panel schaltet zurück.
- Im Training wird jede Kugel gedreht, gespiegelt, leicht verschoben und heller/dunkler gemacht.
  Die Farbe selbst bleibt fast unverändert – sie unterscheidet rot, orange und weinrot.
- Optionen: `--epochs 40` (Standard), `--kopien 6` (veränderte Kopien je Kugel), `--val 0.15`.

## Was im Modell steckt

- Stufe 1: YOLOv8n (klein), Eingabe 640×640, **1 Klasse = Kugel**. Datei ca. 4 MB. Im Training wird
  Farbton/Helligkeit stark verändert – die Farbe ist für Stufe 1 egal, so findet es jede Kugel.
- Stufe 2 + 3 auf dem KI-Chip (Variante D): kleines CNN, Eingabe 40×40, 16 Klassen (Nummern), ca. 100 KB.
- Stufe 2 + 3 als Statistik: pro Art bzw. Farbe eine Normalverteilung über wenige Merkmale (`app/kugel_merkmale.py`),
  ein paar KB als JSON. Fehlen Beispiele für eine Kugel, bleiben dafür die Startwerte aus dem Simulator.
- In der ZIP steht weiter die Kugelnummer als Klasse (0 = Weiße, 1–15); `train_coral.py` macht für
  Stufe 1 daraus „Kugel“ und lernt Stufe 2 + 3 aus den Nummern.
- Auf dem Coral laufen rund 230 von 257 Rechenschritten (alle aufwendigen); nur die letzte
  Auswertung des Netzes läuft auf dem Pi – das ist bei YOLO auf dem Coral normal.
- Ohne Stick oder Modell wird nichts gezählt.
- Ältere Modelle mit 16 Klassen (Kugelnummern) funktionieren als Stufe 1 weiter; die Nummer kommt
  trotzdem aus Stufe 2 + 3. Modelle mit 4 Klassen (voll/halb) werden abgelehnt.
- Ältere festgelegte Bilder werden beim Export übernommen, wenn bei allen Kugeln die Nummer
  bekannt ist; sonst bleiben sie liegen und werden ausgelassen.

Hinweis: Ultralytics steht unter der AGPL-3.0-Lizenz. Für die private Nutzung am eigenen
Tisch ist das kein Thema.

## Testen ohne echte Bilder

`python3 -m sim.make_training_zip 200 sim.zip` (im Ordner `app/`) erzeugt Simulator-Bilder im
selben Format. Nur zum Ausprobieren der Kette – echte Kugeln erkennt so ein Modell schlecht.
