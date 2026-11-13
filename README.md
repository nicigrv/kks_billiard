# Billard-System: Kamera → Kugelerkennung → 8-Ball-Scoreboard

```
Pi-Kameramodul / USB-Kamera ──▶ Raspberry Pi (Docker: "billard") ──HDMI──▶ Beamer
 (oder Demo ohne Kamera)          │  ├── Spiel-Panel   http://<pi-ip>:8089         PIN 1212 (Spieler)
                                  │  ├── Admin-Panel   http://<pi-ip>:8089/admin   PIN 1902
Queue-Gyroskop (optional) ◀─WLAN─▶│  ├── Prüfer        http://<pi-ip>:8089/pruefen  eigener Code
  Castcode, UDP 5005 + HTTP-API   │  └── Beamerbild    http://<pi-ip>:8089/beamer
```

**Kugelerkennung:** ausschließlich über den **KI-Chip (Coral USB Accelerator)** – er erkennt jede
Kugel samt Nummer. Bis ein eigenes Modell trainiert ist, zählt der Pi nichts selbst: Ihr nehmt per
Knopf Bilder auf und legt die Kugeln auf dem iPad fest (Abschnitt 5).

**Beamer:** links das Live-Kamerabild mit Kreisen um die Kugeln, rechts eine gezeichnete
Grafik des Tisches (neu nach jedem Stoß, mit Stoßvorschlag), unten die **Game-Bar**:
Mannschaften, Frames, Punkte und alle 15 Kugeln – versenkte sind durchgestrichen.
Unten rechts ein **QR-Code** (führt zum Spiel-Panel, im Admin-Panel änderbar). Fehler und Hinweise
erscheinen **oben rechts** als Mitteilung wie bei iPhone/Mac – auf dem Beamer und in den Panels.

| Ordner | Inhalt |
|---|---|
| `app/` | **Hauptprogramm**: Erkennung (KI-Chip), 8-Ball-Regeln, Beamer, Spiel- und Admin-Panel |
| `app/sim/` | Simulator + Tests (laufen ohne Tisch) |
| `cue_sensor/` | Programm für den Queue mit Gyroskop (ESP32 + MPU6050, PlatformIO) – optional |
| `training/` | Kugelmodell für den Coral trainieren (PC / Google Colab) |

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
| – Pi-Kameramodul | Flachbandkabel; Pi 5 braucht ein Pi-5-Kamerakabel. Weitwinkel (Camera Module 3 **Wide**) hilft, damit der ganze Tisch ins Bild passt |
| – USB-Kamera | am Pi, möglichst 1280×720 mit MJPG |
| iPad oder Handy | für Spiel- und Admin-Panel, im selben WLAN |
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
die Adresse der Panels (und damit der QR-Code auf dem Beamer) nie.

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
| `x-web-port: &web_port "8089"` (ganz oben) | Port der Panels. Ist 8089 schon belegt, z. B. `"8090"` |
| `x-admin-pin: &admin_pin "1902"` (ganz oben) | PIN fürs Admin-Panel (`/admin`). Leer (`""`) = ohne Anmeldung |
| `x-game-pin: &game_pin "1212"` (ganz oben) | PIN fürs Spiel-Panel (Startseite). Leer (`""`) = Spiel-Panel offen |
| `AUDIO_DEVICE` | Lautsprecher für den Ding-Ton, Standard HDMI 0 am Pi 4/5 (`hdmi:CARD=vc4hdmi0,DEV=0`) |
| `PI_CAMERA: "1"` | Software fürs Pi-Kameramodul mitbauen. Nur mit USB-Kamera `"0"` = schnellerer Bau |

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

In den Ausgaben stehen die Adressen, z. B. `Spiel-Panel http://192.168.88.10:8089/  Admin-Panel
http://192.168.88.10:8089/admin`. Der QR-Code unten rechts auf dem Beamer führt zum Spiel-Panel.

**Autostart:** Nichts weiter zu tun. Docker startet mit dem Pi, die Container starten von selbst
(`restart: unless-stopped`). Pi einfach mit dem Beamer zusammen einschalten.

### 1.7 Einrichten im Admin-Panel

Am iPad `http://<pi-ip>:8089/admin` in Safari öffnen, Admin-PIN **1902** (über „Teilen → Zum
Home-Bildschirm“ wie eine App).

1. **Kamera** wählen (Abschnitt 1a).
2. **Tisch einrichten**: vier Ecken antippen, Tischgröße und Tuchfarbe wählen (Abschnitt 2).
3. Unter „Was die Kamera sieht“ prüfen, ob jede Kugel einen Ring hat.
4. *Optional:* **Queue mit Gyroskop** koppeln (Abschnitt 3b), **QR-Code** anpassen (Abschnitt 1c).
5. Fertig – gespielt wird im **Spiel-Panel** (Startseite, PIN **1212**, Abschnitt 3).

### 1.8 Aktualisieren

```bash
cd ~/billard
git pull
docker compose up -d --build --remove-orphans
```

Einstellungen, Tisch-Ecken, Trainingsbilder, Modelle und Videos liegen in `~/billard/data/`
und bleiben erhalten. **Sichern:** diesen Ordner kopieren, z. B.
`scp -r nicolas@billard.local:billard/data ./billard-sicherung`.

Wer noch den alten Viewer aus Schritt 1 laufen hat: einmal `docker rm -f billard-viewer`
(belegt sonst den HDMI-Ausgang). Eine früher eingestellte ESP32-CAM wird beim Start automatisch
auf das Pi-Kameramodul umgestellt.

### 1.9 Wenn etwas nicht geht

| Problem | Lösung |
|---|---|
| Beamer bleibt schwarz | Raspberry Pi OS **Lite** nutzen (oder mit `sudo raspi-config` → System Options → Boot → **Console** umstellen). Beamer an **HDMI 0**. Logs: `docker logs billard` |
| Panel nicht erreichbar | Richtige IP/Port? `docker compose ps`. `Address already in use` in `docker logs billard` → auf dem Port läuft schon etwas anderes → Port ändern (1.5) |
| „Keine Kamerabilder“ (Meldung oben rechts) | Admin-Panel → „Kamera“: Status lesen. USB: `ls /dev/video*` zeigt die Kamera? |
| Pi-Kameramodul „belegt“ | Läuft eine andere Kamera-Anwendung auf dem Pi? Kabel richtig herum? Test auf dem Pi: `rpicam-hello --list-cameras` |
| „KI-Chip nicht bereit“ | Stick am **blauen USB-3-Port**? Test: `docker compose exec billard python3 coral_check.py`. „Kein Modell“ ist am Anfang normal (Abschnitt 5) |
| Container startet immer neu | `docker logs billard` zeigt den Fehler |

## 1a. Kamera wählen

Admin-Panel → **„Kamera“**. Die Tisch-Ecken werden je Kamera gespeichert, ein Wechsel zerstört die Einrichtung nicht.

| Quelle | Was nötig ist |
|---|---|
| **Pi-Kameramodul** (Flachbandkabel, Standard) | Kamera am CSI-Anschluss; das Programm bringt `picamera2` mit (Bau-Option `PI_CAMERA: "1"`). Läuft auf dem Pi nebenher eine andere Kamera-Anwendung, ist die Kamera belegt. |
| **USB-Kamera am Pi** | einstecken, Gerät meist `/dev/video0` |
| **Demo** | Adresse eines Fotos (JPEG/PNG) eines Pooltisches **von oben**, z. B. von Wikimedia Commons. Danach unter „Tisch einrichten“ die vier Ecken antippen. Ohne Adresse (oder wenn das Bild nicht ladbar ist) läuft ein eingebautes **simuliertes Spiel**, das schon eingerichtet ist. |

Der Container läuft dafür mit `privileged: true` (Zugriff auf HDMI, USB- und Pi-Kamera).

## 1b. Beamerbild im Browser

`http://<pi-ip>:8089/beamer` zeigt das komplette Beamerbild live (Kamera, Grafik, Game-Bar) –
z. B. auf einem Fernseher, Laptop oder zweiten Tablet. Antippen = Vollbild.
Qualität: `/beamer?q=fhd&fps=8` (`q` = `sd`, `hd`, `fhd`; `fps` 1–10). Jede offene Ansicht kostet
den Pi etwas Rechenzeit; mehrere Zuschauer teilen sich aber dasselbe Bild.

## 1c. QR-Code und Meldungen auf dem Beamer

**QR-Code unten rechts:** führt standardmäßig zum Spiel-Panel (`http://<pi-ip>:8089/`), wer ihn
scannt, braucht dann noch die Spiel-PIN. Admin-Panel → **„Beamer: QR-Code“**: ein-/ausblenden,
eigenen Inhalt (z. B. eure Webseite) und die Beschriftung darunter festlegen, mit Vorschau.
Der Link zum Admin-Panel wird nirgends auf dem Beamer gezeigt.

**Meldungen oben rechts** (wie die Mitteilungen bei iPhone/Mac): z. B. „Keine Kamerabilder“,
„Tisch nicht eingerichtet“, „KI-Chip nicht bereit“, „Queue nicht verbunden“, „Speicher fast voll“
– und kurz „Queue verbunden“ / „KI-Chip bereit“, wenn etwas wieder geht. Probleme erscheinen erst,
wenn sie ein paar Sekunden anstehen (kein Flackern beim Start), und verschwinden von selbst, sobald
sie behoben sind. In den Panels klappen sie nach ein paar Sekunden zu einem kleinen Knopf oben
rechts zusammen; Antippen zeigt sie wieder.

## 2. Einmalig: Tisch einrichten

Im Admin-Panel **„Tisch einrichten“** öffnen:
1. Die **vier Ecken der Spielfläche** antippen, dort wo das Tuch auf die Bande trifft, einmal rundherum.
2. **Tischgröße** wählen (7, 8 oder 9 Fuß). Davon hängt die erwartete Kugelgröße ab.
   **Tuchfarbe** (blau/grün) bestimmt nur die Farbe der Grafik; die Erkennung findet das Tuch selbst.
3. Speichern.

Danach unter **„Was die Kamera sieht“** prüfen: Jede Kugel sollte einen Ring haben.
Gelb = volle, blau = halbe, weiß = Weiße, grau = 8.

## 3. Spielen (Spiel-Panel)

Das **Spiel-Panel** ist die Startseite `http://<pi-ip>:8089` (oder QR-Code auf dem Beamer
scannen), PIN **1212**. Danach bleibt das Gerät 30 Tage angemeldet.

1. Kugeln aufbauen, Weiße hinlegen.
2. Teamnamen eintragen, wählen wer anbricht, **„Spiel starten“**.
3. Warten bis unten „Bereit“ steht (Tisch ruhig, 1–2 Sekunden), dann anbrechen.
4. Nach jedem Rack: Kugeln aufbauen, **dann** „Neues Rack“ drücken.

**Was automatisch erkannt wird:** versenkte volle/halbe, versenkte Weiße (Foul), versenkte 8, Gruppenzuteilung, Zugwechsel, Sieg/Niederlage.
**Was NICHT erkannt wird:** falsche Kugel zuerst getroffen, keine Bande berührt, Kugel vom Tisch gesprungen. Dafür gibt es den Knopf **„Foul“**.

**Ganz wichtig: Während die Kugeln rollen und solange sich jemand über den Tisch beugt, wertet das System nichts aus.**
Erst wenn der Tisch frei und ruhig ist. Ein Queue, das quer auf dem Tuch liegt, blockiert deshalb die Auswertung („Tisch wird verdeckt“).

Geht etwas schief: **„Rückgängig“** macht die letzte Änderung rückgängig, unter „Weitere Korrekturen“ lassen sich Gruppen, Restkugeln und Sieger von Hand setzen.

## 3a. Spiel-Panel und Admin-Panel

| | **Spiel-Panel** `/` (PIN 1212) | **Admin-Panel** `/admin` (PIN 1902) |
|---|---|---|
| Für | Spieler am Tisch | Einrichtung, Training |
| Inhalt | Grafik + Spielstand, Spiel starten / Neues Rack / Abbrechen, Foul, Zug wechseln, Rückgängig, Stoß verwerfen, weitere Korrekturen, **Schlagverbesserung an/aus**, **Queue an/aus** + kalibrieren, Spielaufzeichnung + Videos | Kamera, Tisch einrichten, Queue koppeln (Castcode), **QR-Code**, Prüfer + Prüfrunden, KI-Training (Bilder aufnehmen/festlegen/löschen/laden), Modell hochladen, Verlauf |

Beide im Querformat: links die **aktuelle Grafik vom Pi** (wie auf dem Beamer), rechts die Bedienung.
Der Admin darf alles, was das Spiel-Panel darf (Admin-PIN funktioniert auch auf der Startseite).
Großer **Trainingsmodus** unter `/training` (Admin).

## 3b. KI-Schlagverbesserung + Queue mit Gyroskop (optional)

Schlagverbesserung an/aus im Spiel-Panel. Ist sie an, rechnet der Pi nach jedem Stoß den **einfachsten direkten Stoß** für die Mannschaft
am Zug aus (Zielkugel, Tasche, Schnittwinkel) und zeichnet ihn in die Grafik:
**dunkler Queue = optimale Lage**. Mit dem Gyroskop im Queue (`cue_sensor/README.md`) kommt der
**helle Queue = echte Lage** dazu. Liegt der Queue innerhalb der Toleranz (Standard ±2°),
erscheint auf dem Beamer ein **großer grüner Haken**.

Ehrlich: Der Vorschlag ist Geometrie (Geisterkugel-Methode, freie Wege, Schnittwinkel), kein
trainiertes Netz, und ohne Stellungsspiel. Das Gyroskop misst nur den **Winkel**, nicht wo der
Queue liegt – der helle Queue wird immer hinter die Weiße gezeichnet.

**Der Queue ist optional.** Ohne ihn läuft alles andere genauso. Programm für den Queue:
`cue_sensor/` (ESP32 + MPU6050). Pi und Queue reden über das Netzwerk, erkannt wird der Queue an
seinem **Castcode**:

1. Queue einschalten → am Handy WLAN **„Billard-Queue-…“** wählen → auf der Seite, die sich
   öffnet, euer WLAN wählen → der Queue zeigt seinen **Castcode** (6 Zeichen).
2. Admin-Panel → „Queue mit Gyroskop“ → Castcode eintragen → „Koppeln“.
3. Am Handy den Haken „Castcode ist eingetragen“ setzen → „Verbinden“.
4. Der Pi sucht den Queue im WLAN (UDP-Broadcast an Port 5005 mit dem Castcode, nur dieser Queue
   antwortet) und fragt dann ca. 25× pro Sekunde seine Lage über die HTTP-API ab
   (`GET http://<queue>/api/lage`, Kopfzeile `X-Castcode`). Neue IP nach Neustart: findet er von selbst.
5. Einmal kalibrieren: Queue an die lange Bande legen, Spitze zeigt in der Grafik nach rechts →
   „Queue kalibrieren“ (Spiel- oder Admin-Panel). Dreht er sich falsch herum: „Drehrichtung umkehren“.

**An/aus:** Schalter „Queue mit Gyroskop“ im Spiel-Panel (bzw. „Queue benutzen“ im Admin-Panel).
Aus = der Pi sucht und fragt nichts ab. Details zur Firmware: `cue_sensor/README.md`.

## 3c. Spielaufzeichnung

Spiel-Panel → „Runde aufzeichnen“. Bei jedem Zug wird die **grafische Ansicht** gespeichert
(keine Kamerabilder). Ist das Rack vorbei (oder „Aufzeichnung beenden“), baut der Pi daraus ein
kurzes Video, in dem die Kugeln von Zug zu Zug rollen. Videos: im Spiel-Panel ansehen/laden, Dateien in
`data/recordings/`.

## 4. Ohne Tisch testen (Simulator)

Admin-Panel → „Kamera“ → **Demo** ohne Bildadresse. Dann läuft ein simuliertes Spiel in Schleife,
der Tisch ist schon eingerichtet.

Simulierter Queue: `docker compose --profile sim up -d fake-cue`, im Admin-Panel Castcode **SMQ234**
eintragen, im Spiel-Panel die Schlagverbesserung einschalten.

## 5. KI-Chip: Kugeln festlegen, trainieren, Modell aufspielen

Die Kugeln erkennt **nur der KI-Chip** (Coral USB Accelerator). Er läuft direkt im Hauptprogramm,
es gibt keinen eigenen Container mehr. Das Modell erkennt jede Kugel mit ihrer **Nummer**
(Weiße, 1–15). Ohne Stick oder ohne Modell wird nichts gezählt – Bilder werden trotzdem aufgenommen.

**So lernt das System euren Tisch:**

1. **Bilder speichern (am Tisch, kostet keine Zeit):** im Spiel-Panel „Bild speichern“ – oder dort
   den Schalter **„Nach jedem Stoß automatisch“** einschalten: Sobald der Tisch nach einem Stoß
   2 Sekunden ruhig ist, wird ein Bild gespeichert (höchstens eins alle 8 Sekunden, geht auch ohne
   KI-Chip). Auf dem Beamer kommt kurz „Bild gespeichert“. Außerdem: Admin-Panel „Jetzt ein Bild
   aufnehmen“ oder Trainingsmodus „Bild aufnehmen“.
2. **Kugeln festlegen – später, in Ruhe:** Prüfer öffnen `http://<pi-ip>:8089/pruefen` (eigener Code,
   Abschnitt 5a). Läuft gerade keine Prüfrunde, bekommen sie die gespeicherten Bilder – arbeiten
   mehrere gleichzeitig, bekommt jeder ein anderes Bild. „Später“ überspringt, „Unbrauchbar“ legt
   das Bild beiseite. Startet der Admin eine Prüfrunde, geht es nach dem aktuellen Bild dort weiter.
   Der Admin kann dasselbe im Trainingsmodus (`/training`) oder im Admin-Panel →
   „KI-Training: Kugeln festlegen“. Auf eine Kugel tippen,
   dann ihre Nummer (W, 1–15). Fehlt eine Kugel: auf die freie Stelle tippen. Unbrauchbare Bilder
   (Arm im Bild): „Verwerfen“. Sobald es ein Modell gibt, sind die Kugeln schon vorgeschlagen –
   dann nur noch prüfen und „Passt alles“.
3. **Trainieren:** „Trainingsdaten laden“ (ZIP) → `training/train_coral.py`, am einfachsten in
   Google Colab. Anleitung: `training/README.md`.
4. **Hochladen:** `billard_edgetpu.tflite` unter „Modell hochladen“. Der KI-Chip nimmt es sofort.
5. **Wiederholen, bis es passt:** Im Admin-Panel steht, wie oft der KI-Chip bei euren geprüften Bildern
   richtig lag („… % der Kugeln richtig“). Mit jedem Training mit mehr Bildern wird es besser.

**Die Bilder bleiben auf dem Pi** (`data/dataset/`): `pruefen/` (offen), `geprueft/` (festgelegt,
fürs Training), `verworfen/` (aufgehoben, nicht gelöscht). Aufgenommen wird, solange mehr als 1 GB
frei ist. „Alle Bilder laden“ lädt alles als ZIP herunter.

**Alle Bilder löschen:** Knopf im Admin-Panel bzw. „Alle löschen“ im Trainingsmodus, mit Rückfrage. Löscht
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

## 5b. Trainingsseite im Netz: zuhause festlegen

Wenig Zeit am Tisch? Dann nur Bilder sammeln und zuhause festlegen: Der Pi schickt jedes gespeicherte
Bild automatisch auf den Server (`training_server/`). Jeder Trainer meldet sich dort mit seinem Code an.
Runde 1: jedes Bild wird einmal festgelegt. Runde 2: eine andere Person kontrolliert es. Die fertige
Trainings-ZIP gibt es auf der Admin-Seite. Einrichtung: `training_server/README.md`.

## 5a. Prüfrunde: mehrere Prüfer legen dasselbe Bild fest

Damit die Trainingsdaten stimmen, legt jedes Bild mehrere Leute unabhängig voneinander fest.

1. **Prüfer anlegen (Admin):** Admin-Panel → „Prüfrunde“ → Name eingeben → „Hinzufügen“. Jeder bekommt
   einen 4-stelligen **Code** („Neuer Code“ erzeugt einen anderen, „Entfernen“ meldet ab).
2. **Prüfer anmelden:** Am eigenen iPad `http://<pi-ip>:8089/pruefen` öffnen, Code eingeben.
   Im Admin-Panel leuchtet der Punkt neben dem Namen grün (online).
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
Vorschlag zu sehen (ehrlicher Vergleich; im Admin-Panel umschaltbar: „Prüfer sehen den KI-Vorschlag“).
Danach wird das Ergebnis der Prüfer mit der KI verglichen:
- KI überall richtig und Prüfer einig → Bild direkt in die Trainingsdaten.
- Wo die KI falsch lag (andere Nummer, Kugel übersehen, Kugel gesehen, wo keine ist), kommt ein
  **„?“** mit Notiz, z. B. „KI: 3 · Prüfer: 11“. Das Bild liegt dann beim Admin unter den offenen
  Bildern. **„Prüfer-Ergebnis übernehmen“** setzt alle „?“, bei denen die Prüfer eine klare Antwort
  hatten, mit einem Klick – kurz ansehen, speichern.
- Der Beamer zeigt nach der Runde z. B. „KI lag bei 12 von 14 Kugeln richtig – 2 × ?“; im Admin-Panel
  steht die Trefferquote über die letzten Bilder.

**Ding-Ton:** „Ding testen (HDMI)“ im Admin-Panel. Kommt nichts: am Beamer Lautstärke prüfen; anderer
Ausgang über `AUDIO_DEVICE` in `docker-compose.yml` (Liste: `docker compose exec billard aplay -L`,
für HDMI 1 z. B. `hdmi:CARD=vc4hdmi1,DEV=0`).

## 6. Tests

Wie gut die Erkennung ist, hängt jetzt vom trainierten Modell ab. Das Admin-Panel zeigt die Trefferquote
auf euren geprüften Bildern (Abschnitt 5).

Tests selbst laufen lassen (im Ordner `app/`):
```bash
python3 -m sim.test_game          # Regeln + simuliertes Spiel (Kugeln aus dem Simulator)
python3 -m sim.test_features      # Stoßvorschlag, Gyroskop, Game-Bar, Aufzeichnung
python3 -m sim.test_coral         # KI-Chip, Bilder aufnehmen/festlegen, Trefferquote
python3 -m sim.test_review        # Prüfrunden (Mehrheit), Anmeldung, Ding-Ton
python3 -m sim.test_cue_link      # Queue: Castcode, Suche im WLAN, Abfrage der Lage, an/aus
python3 -m sim.test_panels        # Spiel-/Admin-Panel (PINs), QR-Code und Meldungen auf dem Beamer
```
