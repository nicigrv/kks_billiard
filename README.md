# Billard-System: Kamera → Kugelerkennung → 8-Ball-Scoreboard

```
ESP32-CAM ──WLAN──▶ Raspberry Pi (Docker: "billard") ──HDMI──▶ Beamer
 (oder USB-Kamera / Pi-Kameramodul am Pi, oder Demo)   └── Beamerbild im Browser: http://<pi-ip>:8080/beamer
Queue-Gyroskop ─WLAN (UDP 5005)─▶ │
                                  └── Admin-Portal  http://<pi-ip>:8080  (iPad / Handy)
```

**Kugelerkennung:** ausschließlich über den **KI-Chip (Coral USB Accelerator)** – er erkennt jede
Kugel samt Nummer. Bis ein eigenes Modell trainiert ist, zählt der Pi nichts selbst: Ihr nehmt per
Knopf Bilder auf und legt die Kugeln auf dem iPad fest (Abschnitt 5).

**Beamer:** links das Live-Kamerabild mit Kreisen um die Kugeln, rechts eine gezeichnete
Grafik des Tisches (neu nach jedem Stoß, mit Stoßvorschlag), unten die **Game-Bar**:
Mannschaften, Frames, Punkte und alle 15 Kugeln – versenkte sind durchgestrichen.

| Ordner | Inhalt |
|---|---|
| `app/` | **Hauptprogramm**: Erkennung (KI-Chip), 8-Ball-Regeln, Scoreboard, Admin-Portal |
| `app/sim/` | Simulator + Tests (laufen ohne Tisch) |
| `cue_sensor/` | Firmware für das Gyroskop im Queue (ESP32 + MPU6050, PlatformIO) |
| `training/` | Kugelmodell für den Coral trainieren (PC / Google Colab) |
| `esp32cam_stream/` | Firmware der Kamera |
| `firmware/` | fertige Kamera-Firmware (`.bin`) |
| `tools/` | `update_cam.py`: Kamera-Firmware übers Netzwerk aktualisieren |
| `pi/` | alter Viewer aus Schritt 1 (wird nicht mehr gebraucht) |

---

## 1. Installation (Schritt für Schritt)

Dauer: ca. 1 Stunde, davon 15–30 Minuten Warten beim ersten Bauen.

### 1.1 Was ihr braucht

| Teil | Hinweis |
|---|---|
| **Raspberry Pi 4 oder 5** (4 GB+) | mit Netzteil (Pi 4: 3 A, Pi 5: 5 A) und Kühlung |
| **microSD-Karte** 32 GB+ | oder SSD am USB-3-Port |
| **Beamer/Bildschirm** am HDMI-Ausgang | Pi 4/5: der Anschluss **HDMI 0** (neben dem Strom) |
| **Kamera** über dem Tisch, eine davon: | |
| – ESP32-CAM (AI-Thinker) | per WLAN, Firmware in `esp32cam_stream/` |
| – USB-Kamera | am Pi, möglichst 1280×720 mit MJPG |
| – Pi-Kameramodul | Flachbandkabel; Pi 5 braucht ein Pi-5-Kamerakabel. Weitwinkel (Camera Module 3 **Wide**) hilft, damit der ganze Tisch ins Bild passt |
| iPad oder Handy | für das Admin-Portal, im selben WLAN |
| **Coral USB Accelerator** (KI-Chip) | erkennt die Kugeln, an den **blauen USB-3-Port** (Abschnitt 5) |
| *optional* Gyroskop im Queue | ESP32 + MPU6050, siehe `cue_sensor/README.md` |

Ohne Kamera geht es auch: Im **Demo-Modus** läuft ein simuliertes Spiel (Abschnitt 1a).

### 1.2 Raspberry Pi OS auf die Karte

1. Am PC den **Raspberry Pi Imager** installieren: https://www.raspberrypi.com/software/
2. Gerät wählen, Betriebssystem: **Raspberry Pi OS Lite (64-bit)** (unter „Raspberry Pi OS (other)“).
   *Lite* = ohne Desktop. Wichtig: Ein laufender Desktop belegt den HDMI-Ausgang, dann bleibt der Beamer schwarz.
3. Bei „Einstellungen bearbeiten“ eintragen:
   - Hostname, z. B. `billard`
   - Benutzername und Passwort
   - WLAN (Name, Passwort, Land `DE`) – oder später ein LAN-Kabel stecken
   - Reiter „Dienste“: **SSH aktivieren**
4. Schreiben, Karte in den Pi, Beamer und (falls genutzt) Kamera/Coral anschließen, einschalten.
   Der erste Start dauert 1–2 Minuten.

**Tipp:** Im Router dem Pi eine **feste IP-Adresse** geben (DHCP-Reservierung). Dann ändert sich
die Adresse des Portals nie. Dasselbe für die ESP32-CAM.

### 1.3 Am Pi anmelden und Docker installieren

Vom PC aus (Windows: PowerShell, Mac/Linux: Terminal), `nicolas` durch euren Benutzer ersetzen:

```bash
ssh nicolas@billard.local
```

Auf dem Pi:

```bash
sudo apt update && sudo apt full-upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
sudo reboot
```

Nach dem Neustart wieder per `ssh` anmelden und prüfen:

```bash
docker run --rm hello-world
```

Erscheint „Hello from Docker!“, passt alles.

### 1.4 Programm auf den Pi holen

```bash
sudo apt install -y git
git clone https://github.com/nicigrv/kks_billiard.git ~/billard
cd ~/billard
```

Ist das Repository privat, fragt `git` nach Benutzername und Passwort: als Passwort ein
GitHub-Token (GitHub → Settings → Developer settings → Personal access tokens) eingeben.
Alternativ auf GitHub „Code → Download ZIP“, die ZIP auf den Pi kopieren
(`scp kks_billiard-main.zip nicolas@billard.local:~`) und dort `unzip kks_billiard-main.zip && mv kks_billiard-main billard`.

### 1.5 Einstellungen in `docker-compose.yml`

```bash
nano docker-compose.yml
```

| Eintrag | Bedeutung |
|---|---|
| `x-web-port: &web_port "8080"` (ganz oben) | Port des Portals. Ist 8080 schon belegt, z. B. `"8090"` |
| `x-admin-pin: &admin_pin "1902"` (ganz oben) | PIN fürs Admin-Portal. Leer (`""`) = ohne Anmeldung |
| `AUDIO_DEVICE` | Lautsprecher für den Ding-Ton, Standard HDMI 0 am Pi 4/5 (`hdmi:CARD=vc4hdmi0,DEV=0`) |
| `CAM_URL` | Nur die **erste** Vorgabe für die ESP32-CAM. Später im Portal unter „Kamera“ änderbar |
| `PI_CAMERA: "1"` | Software fürs Pi-Kameramodul mitbauen. Ohne Kameramodul `"0"` = schnellerer Bau |

Speichern: `Strg+O`, `Enter`, `Strg+X`.

### 1.6 Starten

```bash
docker compose up -d --build
```

Das erste Bauen dauert **15–30 Minuten** (lädt und installiert alles). Danach:

```bash
docker compose ps          # "billard" sollte "running" sein
docker logs -f billard     # Ausgaben ansehen, beenden mit Strg+C
```

In den Ausgaben steht die Adresse, z. B. `Admin http://192.168.88.10:8080`. Die steht auch unten
rechts auf dem Beamer.

**Autostart:** Nichts weiter zu tun. Docker startet mit dem Pi, die Container starten von selbst
(`restart: unless-stopped`). Pi einfach mit dem Beamer zusammen einschalten.

### 1.7 Einrichten im Portal

Am iPad `http://<pi-ip>:8080` in Safari öffnen (über „Teilen → Zum Home-Bildschirm“ wie eine App).

1. **Kamera** wählen (Abschnitt 1a). Für die ESP32-CAM: siehe unten.
2. **Tisch einrichten**: vier Ecken antippen, Tischgröße und Tuchfarbe wählen (Abschnitt 2).
3. Unter „Was die Kamera sieht“ prüfen, ob jede Kugel einen Ring hat.
4. Fertig – **Spiel starten** (Abschnitt 3).

### 1.8 ESP32-CAM einrichten (nur wenn ihr sie nutzt)

1. **Erstes Aufspielen** der Firmware per USB (ESP32-CAM-MB-Adapter) mit der Arduino IDE:
   Sketch `esp32cam_stream/esp32cam_stream.ino`, Board „AI Thinker ESP32-CAM“, PSRAM an,
   Partition „Default 4MB with spiffs“. Spätere Updates gehen übers WLAN (Abschnitt 7).
2. Kamera einschalten. Sie öffnet den WLAN-Hotspot **„Billard-Cam-Setup“**. Mit dem Handy
   verbinden, die Einrichtungsseite öffnet sich (sonst `http://192.168.4.1`), euer WLAN eintragen.
3. Die Kamera startet neu und ist im WLAN. Ihre Adresse steht im Router.
4. Im Portal unter „Kamera“: **ESP32-CAM**, Adresse `http://<kamera-ip>:81/stream`, „Kamera übernehmen“.

### 1.9 Aktualisieren

```bash
cd ~/billard
git pull
docker compose up -d --build --remove-orphans
```

Einstellungen, Tisch-Ecken, Trainingsbilder, Modelle und Videos liegen in `~/billard/data/`
und bleiben erhalten. **Sichern:** diesen Ordner kopieren, z. B.
`scp -r nicolas@billard.local:billard/data ./billard-sicherung`.

Wer noch den alten Viewer aus Schritt 1 laufen hat: einmal `docker rm -f billard-viewer`
(belegt sonst den HDMI-Ausgang).

### 1.10 Wenn etwas nicht geht

| Problem | Lösung |
|---|---|
| Beamer bleibt schwarz | Raspberry Pi OS **Lite** nutzen (oder mit `sudo raspi-config` → System Options → Boot → **Console** umstellen). Beamer an **HDMI 0**. Logs: `docker logs billard` |
| Portal nicht erreichbar | Richtige IP/Port? `docker compose ps`. Läuft auf dem Port schon etwas anderes → Port ändern (1.5) |
| „Warte auf Kamera …“ | Portal → „Kamera“: Status/Fehlermeldung lesen. ESP32: Adresse im Browser öffnen. USB: `ls /dev/video*` zeigt die Kamera? |
| Pi-Kameramodul „belegt“ | Läuft eine andere Kamera-Anwendung auf dem Pi? Kabel richtig herum? Test auf dem Pi: `rpicam-hello --list-cameras` |
| „KI-Chip nicht bereit“ | Stick am **blauen USB-3-Port**? Test: `docker compose exec billard python3 coral_check.py`. „Kein Modell“ ist am Anfang normal (Abschnitt 5) |
| Container startet immer neu | `docker logs billard` zeigt den Fehler |

## 1a. Kamera wählen

Portal → **„Kamera“**. Die Tisch-Ecken werden je Kamera gespeichert, ein Wechsel zerstört die Einrichtung nicht.

| Quelle | Was nötig ist |
|---|---|
| **ESP32-CAM (WLAN)** | Adresse des Streams, z. B. `http://192.168.88.200:81/stream` |
| **USB-Kamera am Pi** | einstecken, Gerät meist `/dev/video0` |
| **Pi-Kameramodul** (Flachbandkabel) | Kamera am CSI-Anschluss; das Programm bringt `picamera2` mit (Bau-Option `PI_CAMERA: "1"`). Läuft auf dem Pi nebenher eine andere Kamera-Anwendung, ist die Kamera belegt. |
| **Demo** | Adresse eines Fotos (JPEG/PNG) eines Pooltisches **von oben**, z. B. von Wikimedia Commons. Danach unter „Tisch einrichten“ die vier Ecken antippen. Ohne Adresse (oder wenn das Bild nicht ladbar ist) läuft ein eingebautes **simuliertes Spiel**, das schon eingerichtet ist. |

Der Container läuft dafür mit `privileged: true` (Zugriff auf HDMI, USB- und Pi-Kamera).

## 1b. Beamerbild im Browser

`http://<pi-ip>:8080/beamer` zeigt das komplette Beamerbild live (Kamera, Grafik, Game-Bar) –
z. B. auf einem Fernseher, Laptop oder zweiten Tablet. Antippen = Vollbild.
Qualität: `/beamer?q=fhd&fps=8` (`q` = `sd`, `hd`, `fhd`; `fps` 1–10). Jede offene Ansicht kostet
den Pi etwas Rechenzeit; mehrere Zuschauer teilen sich aber dasselbe Bild.

## 2. Einmalig: Tisch einrichten

Im Portal **„Tisch einrichten“** öffnen:
1. Die **vier Ecken der Spielfläche** antippen, dort wo das Tuch auf die Bande trifft, einmal rundherum.
2. **Tischgröße** wählen (7, 8 oder 9 Fuß). Davon hängt die erwartete Kugelgröße ab.
   **Tuchfarbe** (blau/grün) bestimmt nur die Farbe der Grafik; die Erkennung findet das Tuch selbst.
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
- **Anmeldung:** Das Portal fragt einmal nach dem Admin-PIN (Standard `1902`), danach bleibt das
  iPad 30 Tage angemeldet. „Abmelden“ im Trainingsmodus.
- **Prüfrunde (mehrere Prüfer)**: Prüfer anlegen (jeder bekommt einen Code), Bild an alle schicken
  (siehe 5a)
- **KI-Training: Kugeln festlegen**: Bilder aufnehmen (nur per Knopf), Kugeln festlegen, alle Bilder
  löschen (siehe 5). Großer **Trainingsmodus** unter `/training`
- **KI-Modell (Coral)**: Status des KI-Chips, Trainingsdaten / alle Bilder laden, Modell hochladen
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

Am einfachsten: Portal → „Kamera“ → **Demo** ohne Bildadresse. Dann läuft ein simuliertes
Spiel in Schleife, der Tisch ist schon eingerichtet.

Alternativ als eigene simulierte Kamera (wie eine ESP32-CAM):
```bash
docker compose --profile sim up -d fake-table                     # simulierte Kamera auf Port 8081
```
Im Portal unter „Kamera“: ESP32-CAM, Adresse `http://127.0.0.1:8081/stream`. Ecken: die vier Ecken
des Tuchs antippen.

Simuliertes Queue-Gyroskop: `docker compose --profile sim up -d fake-cue` (Schlagverbesserung im Portal einschalten).

## 5. KI-Chip: Kugeln festlegen, trainieren, Modell aufspielen

Die Kugeln erkennt **nur der KI-Chip** (Coral USB Accelerator). Er läuft direkt im Hauptprogramm,
es gibt keinen eigenen Container mehr. Das Modell erkennt jede Kugel mit ihrer **Nummer**
(Weiße, 1–15). Ohne Stick oder ohne Modell wird nichts gezählt – Bilder werden trotzdem aufgenommen.

**So lernt das System euren Tisch:**

1. **Bilder aufnehmen:** Nur per Knopf – im Portal „Jetzt ein Bild aufnehmen“ oder im
   Trainingsmodus (`/training`) „Bild aufnehmen“. Automatisch wird nichts aufgenommen.
2. **Kugeln festlegen (iPad):** Portal → „KI-Training: Kugeln festlegen“. Auf eine Kugel tippen,
   dann ihre Nummer (W, 1–15). Fehlt eine Kugel: auf die freie Stelle tippen. Unbrauchbare Bilder
   (Arm im Bild): „Verwerfen“. Sobald es ein Modell gibt, sind die Kugeln schon vorgeschlagen –
   dann nur noch prüfen und „Passt alles“.
3. **Trainieren:** „Trainingsdaten laden“ (ZIP) → `training/train_coral.py`, am einfachsten in
   Google Colab. Anleitung: `training/README.md`.
4. **Hochladen:** `billard_edgetpu.tflite` unter „Modell hochladen“. Der KI-Chip nimmt es sofort.
5. **Wiederholen, bis es passt:** Im Portal steht, wie oft der KI-Chip bei euren geprüften Bildern
   richtig lag („… % der Kugeln richtig“). Mit jedem Training mit mehr Bildern wird es besser.

**Die Bilder bleiben auf dem Pi** (`data/dataset/`): `pruefen/` (offen), `geprueft/` (festgelegt,
fürs Training), `verworfen/` (aufgehoben, nicht gelöscht). Aufgenommen wird, solange mehr als 1 GB
frei ist. „Alle Bilder laden“ lädt alles als ZIP herunter.

**Alle Bilder löschen:** Knopf im Portal bzw. „Alle löschen“ im Trainingsmodus, mit Rückfrage. Löscht
offene, festgelegte und verworfene Bilder und setzt die Trefferquote zurück. Das hochgeladene
KI-Modell bleibt. Vorher ggf. „Trainingsdaten laden“ bzw. „Alle Bilder laden“ zum Sichern.

Test des Sticks:
```bash
docker compose exec billard python3 coral_check.py
```
Erwartet: `[OK] Treiber findet den Stick` und – sobald ein Modell da ist – die Zeit pro Bild.

Technik: Googles Coral-Paketquelle gibt es nicht mehr. Der Treiber (`libedgetpu`) und
`tflite_runtime` für Python 3.11 kommen von den gepflegten Builds unter github.com/feranick
(beide gegen TensorFlow 2.17.1, im Dockerfile mit Prüfsummen festgelegt).

## 5a. Prüfrunde: mehrere Prüfer legen dasselbe Bild fest

Damit die Trainingsdaten stimmen, legt jedes Bild mehrere Leute unabhängig voneinander fest.

1. **Prüfer anlegen (Admin):** Portal → „Prüfrunde“ → Name eingeben → „Hinzufügen“. Jeder bekommt
   einen 4-stelligen **Code** („Neuer Code“ erzeugt einen anderen, „Entfernen“ meldet ab).
2. **Prüfer anmelden:** Am eigenen iPad `http://<pi-ip>:8080/pruefen` öffnen, Code eingeben.
   Im Portal leuchtet der Punkt neben dem Namen grün (online).
3. **Bild an alle Prüfer (Admin):** Der Pi nimmt das aktuelle Kamerabild auf und schickt es an alle
   angemeldeten Prüfer – mit dem Vorschlag des KI-Chips, falls es schon ein Modell gibt.
4. **Jeder legt die Kugeln fest** (Kugel antippen → Nummer) und drückt **Speichern**. Auf dem Beamer
   steht, wer fertig ist (✓) und wer noch arbeitet.
5. **Alle fertig:** Der Beamer zeigt „Alle fertig – weiter!“ und über HDMI klingt ein **Ding**.

**Zusammenführen:** Eine Kugel zählt, wenn die Mehrheit sie markiert hat; Position = Mittelwert,
Nummer = Mehrheit. Sind sich alle einig oder entscheidet die Mehrheit, kommt das Bild direkt in die
Trainingsdaten. Gibt es keine Mehrheit (z. B. bei 2 Prüfern: einer sagt 3, einer 11), landet das Bild
beim Admin unter den offenen Bildern, die strittigen Kugeln sind mit „?“ markiert.
Geht jemand weg: „Mit den bisherigen abschließen“ oder „Runde abbrechen“.

**KI-Abgleich (sobald ein Modell hochgeladen ist):** Der KI-Chip macht zu jedem Bild der Prüfrunde
einen Vorschlag. Die Prüfer legen die Kugeln trotzdem selbst fest – standardmäßig **ohne** den
Vorschlag zu sehen (ehrlicher Vergleich; im Portal umschaltbar: „Prüfer sehen den KI-Vorschlag“).
Danach wird das Ergebnis der Prüfer mit der KI verglichen:
- KI überall richtig und Prüfer einig → Bild direkt in die Trainingsdaten.
- Wo die KI falsch lag (andere Nummer, Kugel übersehen, Kugel gesehen, wo keine ist), kommt ein
  **„?“** mit Notiz, z. B. „KI: 3 · Prüfer: 11“. Das Bild liegt dann beim Admin unter den offenen
  Bildern. **„Prüfer-Ergebnis übernehmen“** setzt alle „?“, bei denen die Prüfer eine klare Antwort
  hatten, mit einem Klick – kurz ansehen, speichern.
- Der Beamer zeigt nach der Runde z. B. „KI lag bei 12 von 14 Kugeln richtig – 2 × ?“; im Portal
  steht die Trefferquote über die letzten Bilder.

**Ding-Ton:** „Ding testen (HDMI)“ im Portal. Kommt nichts: am Beamer Lautstärke prüfen; anderer
Ausgang über `AUDIO_DEVICE` in `docker-compose.yml` (Liste: `docker compose exec billard aplay -L`,
für HDMI 1 z. B. `hdmi:CARD=vc4hdmi1,DEV=0`).

## 6. Tests

Wie gut die Erkennung ist, hängt jetzt vom trainierten Modell ab. Das Portal zeigt die Trefferquote
auf euren geprüften Bildern (Abschnitt 5).

Tests selbst laufen lassen (im Ordner `app/`):
```bash
python3 -m sim.test_game          # Regeln + simuliertes Spiel (Kugeln aus dem Simulator)
python3 -m sim.test_features      # Stoßvorschlag, Gyroskop, Game-Bar, Aufzeichnung
python3 -m sim.test_coral         # KI-Chip, Bilder aufnehmen/festlegen, Trefferquote
python3 -m sim.test_review        # Prüfrunden (Mehrheit), Anmeldung, Ding-Ton
```

## 7. Kamera-Firmware aktualisieren

```bash
cp esp32cam_stream.ino.bin ~/billard/firmware/
docker compose run --rm updater
```
