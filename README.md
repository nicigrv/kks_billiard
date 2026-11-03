# Billard-System: Kamera → Kugelerkennung → 8-Ball-Scoreboard

```
ESP32-CAM ──WLAN──▶ Raspberry Pi (Docker: "billard") ──HDMI──▶ Beamer
Queue-Gyroskop ─WLAN (UDP 5005)─▶ │
                                  └── Admin-Portal  http://<pi-ip>:8080  (iPad / Handy)
```

**Beamer:** links das Live-Kamerabild mit Kreisen um die Kugeln, rechts eine gezeichnete
Grafik des Tisches (neu nach jedem Stoß, mit Stoßvorschlag), unten die **Game-Bar**:
Mannschaften, Frames, Punkte und alle 15 Kugeln – versenkte sind durchgestrichen.

| Ordner | Inhalt |
|---|---|
| `app/` | **Hauptprogramm**: Erkennung, 8-Ball-Regeln, Scoreboard, Admin-Portal |
| `app/sim/` | Simulator + Tests (laufen ohne Tisch) |
| `cue_sensor/` | Firmware für das Gyroskop im Queue (ESP32 + MPU6050, PlatformIO) |
| `coral/` | Hardwaretest für den Coral USB Accelerator |
| `esp32cam_stream/` | Firmware der Kamera |
| `firmware/` | fertige Kamera-Firmware (`.bin`) |
| `tools/` | `update_cam.py`: Kamera-Firmware übers Netzwerk aktualisieren |
| `pi/` | alter Viewer aus Schritt 1 (wird nicht mehr gebraucht) |

---

## 1. Installieren / Aktualisieren auf dem Pi

```bash
# Ordner "billard" auf den Pi kopieren (vom PC):  scp billard.zip nicolas@nicolas10:~
cd ~ && unzip -o billard.zip && cd billard

docker rm -f billard-viewer        # alter Viewer aus Schritt 1 muss weg (belegt sonst HDMI)
nano docker-compose.yml            # CAM_URL = IP der Kamera, ADMIN_PIN setzen
docker compose up -d --build --remove-orphans
docker logs -f billard
```

Dann am Handy `http://<pi-ip>:8080` öffnen. Die Adresse steht auch unten rechts auf dem Beamer.

## 2. Einmalig: Tisch einrichten

Im Portal **„Tisch einrichten“** öffnen:
1. Die **vier Ecken der Spielfläche** antippen, dort wo das Tuch auf die Bande trifft, einmal rundherum.
2. **Tischgröße** wählen (7, 8 oder 9 Fuß). Davon hängt die erwartete Kugelgröße ab.
3. Speichern.

Danach unter **„Was die Kamera sieht“** prüfen: Jede Kugel sollte einen Ring haben.
Gelb = volle, blau = halbe, weiß = Weiße, grau = 8.

## 3. Spielen

1. Kugeln aufbauen, Weiße hinlegen.
2. Im Portal Teamnamen eintragen, wählen wer anbricht, **„Spiel starten“**.
3. Warten bis unten „Bereit“ steht (Tisch ruhig, 1–2 Sekunden), dann anbrechen.
4. Nach jedem Rack: Kugeln aufbauen, **dann** „Neues Rack“ drücken.

**Was automatisch erkannt wird:** versenkte volle/halbe, versenkte Weiße (Foul), versenkte 8, Gruppenzuteilung, Zugwechsel, Sieg/Niederlage.
**Was NICHT erkannt wird:** falsche Kugel zuerst getroffen, keine Bande berührt, Kugel vom Tisch gesprungen. Dafür gibt es den Knopf **„Foul“**.

**Ganz wichtig: Während die Kugeln rollen und solange sich jemand über den Tisch beugt, wertet das System nichts aus.**
Erst wenn der Tisch frei und ruhig ist. Ein Queue, das quer auf dem Tuch liegt, blockiert deshalb die Auswertung („Tisch wird verdeckt“).

Geht etwas schief: **„Rückgängig“** macht die letzte Änderung rückgängig, unter „Weitere Korrekturen“ lassen sich Gruppen, Restkugeln und Sieger von Hand setzen.

## 3a. iPad als Admin-Portal

`http://<pi-ip>:8080` in Safari öffnen, „Zum Home-Bildschirm“ hinzufügen. Im Querformat:
links die **aktuelle Grafik vom Pi** (wie auf dem Beamer, aktualisiert sich selbst), rechts die Bedienung.

- **Spiel starten / Neues Rack / Abbrechen**
- **Letzten erkannten Stoß verwerfen**: wenn das System einen Stoß gezählt hat, der keiner war.
  Der Spielstand springt auf vor diesem Stoß zurück.
- **Kugeln korrigieren (KI-Training)**: Kugel antippen → Weiße / 8 / Volle / Halbe / keine Kugel
  und Farbe wählen; fehlende Kugel: auf die freie Stelle tippen. Gespeichert wird Bild + richtige
  Beschriftung in `data/dataset/korrigiert/` (fürs Training), auf Wunsch auch sofort im laufenden Spiel.
- **KI-Schlagverbesserung an/aus** (siehe 3b)
- **Spielaufzeichnung** (siehe 3c)

## 3b. KI-Schlagverbesserung + Gyroskop im Queue

Ist sie an, rechnet der Pi nach jedem Stoß den **einfachsten direkten Stoß** für die Mannschaft
am Zug aus (Zielkugel, Tasche, Schnittwinkel) und zeichnet ihn in die Grafik:
**dunkler Queue = optimale Lage**. Mit dem Gyroskop im Queue (`cue_sensor/README.md`) kommt der
**helle Queue = echte Lage** dazu. Liegt der Queue innerhalb der Toleranz (Standard ±2°),
erscheint auf dem Beamer ein **großer grüner Haken**.

Ehrlich: Der Vorschlag ist Geometrie (Geisterkugel-Methode, freie Wege, Schnittwinkel), kein
trainiertes Netz, und ohne Stellungsspiel. Das Gyroskop misst nur den **Winkel**, nicht wo der
Queue liegt – der helle Queue wird immer hinter die Weiße gezeichnet.

Einrichten: Portal → „Queue mit Gyroskop“ → Queue an die lange Bande legen, Spitze zeigt in der
Grafik nach rechts → „Queue kalibrieren“. Dreht er sich falsch herum: „Drehrichtung umkehren“.

## 3c. Spielaufzeichnung

Portal → „Runde aufzeichnen“. Bei jedem Zug wird die **grafische Ansicht** gespeichert
(keine Kamerabilder). Ist das Rack vorbei (oder „Aufzeichnung beenden“), baut der Pi daraus ein
kurzes Video, in dem die Kugeln von Zug zu Zug rollen. Videos: im Portal ansehen/laden, Dateien in
`data/recordings/`.

## 4. Ohne Tisch testen (Simulator)

```bash
docker compose --profile sim up -d fake-table                     # simulierte Kamera auf Port 8081
# in docker-compose.yml:  CAM_URL: http://127.0.0.1:8081/stream
docker compose up -d
```
Ecken für den Simulator: einfach die vier Ecken des grünen Bereichs antippen. Das Spiel läuft in Schleife.

Simuliertes Queue-Gyroskop: `docker compose --profile sim up -d fake-cue` (Schlagverbesserung im Portal einschalten).

## 5. Coral-Stick testen

```bash
docker compose build coral-check      # einmalig, lädt Googles Coral-Pakete
docker compose run --rm coral-check
```
Erwartet: `[OK] Coral funktioniert: ~3-5 ms pro Bild`. Den Stick an einen **blauen USB-3-Port** stecken.

**Ehrlich:** Der Test beweist nur, dass Stick und Treiber laufen. Ein Modell, das Billardkugeln erkennt,
gibt es nicht fertig. Dafür braucht es Trainingsbilder **von eurem Tisch**:
im Portal unter **„Trainingsdaten für Coral“** sammeln (landen in `data/dataset/`), ein paar Hundert Bilder,
Beschriftungen prüfen, dann trainieren (nächster Schritt).

## 6. Genauigkeit (gemessen im Simulator)

| Messung | Ergebnis |
|---|---|
| Kugeln gefunden (Einzelbild) | 99–100 % |
| Typ richtig (weiß / 8 / voll / halb) | 96–100 % |
| Kugeln im Startdreieck gefunden | 13–16 von 16 |
| Einzelstoß komplett richtig ausgewertet | 93 % (Foul mit der Weißen: 100 %) |
| Simuliertes Spiel (3 Partien, je 8 Stöße) | alle Stöße richtig |
| Rechenzeit pro Bild | ~31 ms auf x86, auf dem Pi 4 geschätzt 100–130 ms |

**Auf deinem echten Tisch wird es schlechter sein.** Licht, Spiegelungen, Kameraqualität und Tuchfarbe
bildet kein Simulator vollständig ab. Grüne Kugeln auf grünem Tuch sind am schwierigsten.
Deshalb: erste Abende mit dem Portal in der Hand spielen und Fehler korrigieren.

Tests selbst laufen lassen (im Ordner `app/`):
```bash
python3 -m sim.test_detector      # Erkennungsqualität
python3 -m sim.test_game          # Regeln + simuliertes Spiel
python3 -m sim.benchmark_shots    # Trefferquote pro Stoß
python3 -m sim.test_features      # Stoßvorschlag, Gyroskop, Game-Bar, Aufzeichnung
```

## 7. Kamera-Firmware aktualisieren

```bash
cp esp32cam_stream.ino.bin ~/billard/firmware/
docker compose run --rm updater
```
