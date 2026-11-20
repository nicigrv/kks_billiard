# Billard-System: Kamera → Kugelerkennung → 8-Ball-Scoreboard

## Schnellstart: Installieren und Updaten

Fertige Versionen liegen unter **[Releases](https://github.com/nicigrv/kks_billiard/releases)**.
Versionsnummern nach dem Schema **MAJOR.MINOR.PATCH** (z. B. `v0.1.2`):

| Teil | wird erhöht bei | wie |
|---|---|---|
| PATCH (0.1.**2** → 0.1.**3**) | Fehlerbehebungen, kleine Änderungen | automatisch nach jedem gemergten Pull Request |
| MINOR (0.**1**.3 → 0.**2**.0) | neue Funktionen | Label `minor` an den Pull Request |
| MAJOR (**0**.2.0 → **1**.0.0) | große Umbauten, fertige erste Version | Label `major` an den Pull Request |

Von Hand: **Actions → Release → Run workflow**, dort Stufe (patch/minor/major) oder eine feste Version wählen.
Jede Version hat diese Dateien:

| Datei | Für | Inhalt |
|---|---|---|
| `billard-rasp.zip` | Raspberry Pi | `app/` + `docker-compose.yml` |
| `billard-server.zip` | Server (VPS) | Trainingsseite (`training_server/`) |
| `billard-queue.zip` | Queue (Gyroskop) | Quellcode, `.bin` für USB (Atom Matrix) und für das Update über WLAN, Anleitung in `firmware/FLASHEN.md` |
| `queue-ota-<board>.bin` | Queue | Firmware-Update über WLAN (holt das Admin-Panel selbst) |
| `billard-gesamt.zip` | alles | komplettes Repository |
| `update.sh` | Pi + Server | Skript für Installation und Update |

**Raspberry Pi installieren** (neueste Version, Ordner `~/billard`, installiert Docker falls nötig):

```bash
curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- rasp
```

Vorher Pi-OS einrichten wie in 1.1–1.3 beschrieben. Danach PINs/Port in `~/billard/docker-compose.yml` anpassen (1.5)
und `cd ~/billard && docker compose up -d` ausführen.

**Raspberry Pi updaten** (gleicher Befehl, Einstellungen bleiben erhalten):

```bash
curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- rasp
```

**Server installieren / updaten** (Ordner `~/billard-training`, legt beim ersten Mal eine `.env` mit `UPLOAD_KEY` an
und zeigt den Schlüssel an):

```bash
curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- server
```

**Bestimmte Version** (z. B. zurück auf v0.1.2): Versionsnummer anhängen.

```bash
curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- rasp v0.1.2
curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- server v0.1.2
```

Anderer Ordner: `BILLARD_DIR` setzen, z. B. `... | BILLARD_DIR=/opt/billard bash -s -- server`.
Beim Update bleiben `data/`, `.env` und die Einstellungen ganz oben in `docker-compose.yml`
(`x-web-port`, `x-admin-pin`, `x-game-pin`) erhalten. Die alte Datei liegt als `docker-compose.yml.alt` daneben.
Welche Version läuft, steht in der Datei `VERSION`.

Den **Queue** aktualisiert ihr über das WLAN, ohne USB-Kabel (ab Firmware 3.3.0):
Admin-Panel → Reiter **Queue** → **Firmware des Queues** → „Neueste Version installieren“.
Ohne Pi geht es im Browser unter `http://<IP des Queues>/update` (Datei `queue-ota-<board>.bin`).
Das erste Mal (oder ältere Firmware) per USB: `billard-queue.zip`, Anleitung in `firmware/FLASHEN.md`.

---

```
Pi-Kameramodul / USB-Kamera ──▶ Raspberry Pi (Docker: "billard") ──HDMI──▶ Beamer
 (oder Demo ohne Kamera)          │  ├── Spiel-Panel   http://<pi-ip>:8089         PIN 1212 (Spieler)
                                  │  └── Admin-Panel   http://<pi-ip>:8089/admin   PIN 1902
Queue-Gyroskop (optional) ◀─WLAN─▶│
  Castcode, UDP 5005 + HTTP-API   └──Internet──▶ Trainingsseite (VPS, training_server/)
                                     Bilder hin, Modelle zurück   Kugeln festlegen, Farben lernen
```

**Kugelerkennung in drei Stufen:** 1. der **KI-Chip (Coral USB Accelerator)** findet die Kugeln,
2. der Pi schaut, ob eine Kugel **gestreift** ist (Weiße / 8 / voll / halb), 3. welche **Farbe** sie hat.
Daraus ergibt sich die Nummer. Stufe 2 + 3 lernen aus euren festgelegten Bildern (Sekunden, direkt
im Admin-Panel) – oder laufen als kleines Netz ebenfalls auf dem KI-Chip (`training/train_klassen.py`). Ohne KI-Chip und Modell zählt der Pi nichts: Ihr nehmt per Knopf Bilder auf und
legt die Kugeln auf dem iPad fest (Abschnitt 5).

**Beamer:** links das Live-Kamerabild mit Kreisen um die Kugeln, rechts eine gezeichnete
Grafik des Tisches (neu nach jedem Stoß, mit Stoßvorschlag), unten die **Game-Bar**:
Mannschaften, Frames, Punkte und alle 15 Kugeln – versenkte sind durchgestrichen.
Unten rechts ein **QR-Code** (führt zum Spiel-Panel, im Admin-Panel änderbar). Fehler und Hinweise
erscheinen **oben rechts** als Mitteilung wie bei iPhone/Mac – auf dem Beamer und in den Panels.

| Ordner | Inhalt |
|---|---|
| `app/` | **Hauptprogramm**: Erkennung (KI-Chip), 8-Ball-Regeln, Beamer, Spiel- und Admin-Panel |
| `app/sim/` | Simulator + Tests (laufen ohne Tisch) |
| `cue_sensor/` | Programm für den Queue mit Gyroskop: Richtung + Stoßstärke (ESP32 + MPU6050 oder M5Stack Atom Matrix, PlatformIO) – optional |
| `scripts/` | `update.sh` (Installation/Update) und `build_release.sh` (baut die Release-Pakete) |
| `.github/workflows/check.yml` | Formprüfung bei jedem Pull Request (Syntax, ca. 30 s) |
| `.github/workflows/test.yml` | Tests und Firmware-Build nach dem Merge |
| `.github/workflows/release.yml` | erstellt nach jedem gemergten PR automatisch ein Release (PATCH + 1) |
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
4. *Optional:* **Queue mit Gyroskop** koppeln (Abschnitt 3b), **QR-Code** anpassen (Abschnitt 1b).
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

## 1b. QR-Code und Meldungen auf dem Beamer

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
3. Warten bis unten „Bereit“ steht (Tisch ruhig, gut 1 Sekunde), dann anbrechen.
4. Nach jedem Rack: Kugeln aufbauen, **dann** „Neues Rack“ drücken.

**Was automatisch erkannt wird:** versenkte volle/halbe, versenkte Weiße (Foul), versenkte 8, Gruppenzuteilung, Zugwechsel, Sieg/Niederlage.
**Was NICHT erkannt wird:** falsche Kugel zuerst getroffen, keine Bande berührt, Kugel vom Tisch gesprungen. Dafür gibt es den Knopf **„Foul“**.

**Ganz wichtig: Während die Kugeln rollen und solange sich jemand über den Tisch beugt, wertet das System nichts aus.**
Erst wenn der Tisch frei und ruhig ist. Ein Queue, das quer auf dem Tuch liegt, blockiert deshalb die Auswertung („Tisch wird verdeckt“).

**Wie ein Stoß erkannt wird:** Das System merkt sich den ruhigen Tisch (Ausgangszustand). Nach Bewegung und
1,2 Sekunden Ruhe vergleicht es vorher/nachher: Stoß = Weiße um mehr als 2 Kugelradien bewegt, Weiße
versenkt oder weniger Kugeln auf dem Tisch. Mit dem **Queue mit Gyroskop** kommt Hilfe dazu:
- Misst der Queue einen Stoß, reicht schon eine kleine Bewegung der Weißen (0,6 Radien), und der Stoß wird
  auch erkannt, wenn die Kamera die Bewegung verpasst hat.
- **„Stoß fertig“** – Knopf am Queue während des Spiels (LED in Teamfarbe, blinkt kurz weiß) oder Knopf im
  Spiel-Panel: wertet sofort aus, sobald der Tisch frei ist (ohne 1,2 s Wartezeit), und zählt den Stoß auf
  jeden Fall – auch wenn keine Kugel getroffen wurde.

**Kugel fehlt = im Loch:** Die Kamera liefert 3 Bilder pro Sekunde. Fehlt eine Kugel im Spiel auf **4 Bildern
hintereinander** (gut 1 Sekunde), zählt sie sofort als versenkt – Beamer und Panel zeigen es gleich. Bilder, in
denen jemand über dem Tisch hängt, zählen dabei nicht. Taucht die Kugel im nächsten ruhigen Zustand wieder auf
(war nur verdeckt), kommt sie von selbst zurück. Beim Anstoß gilt das nicht (dort verdecken sich die Kugeln).

**Kugeln antippen:** Im Spiel-Panel unter „Korrigieren“ stehen alle 15 Kugeln. Antippen legt eine Kugel ins Loch,
nochmal antippen holt sie zurück (blass = im Loch). Die 8 fragt vorher nach, denn mit ihr endet das Rack nach den
Regeln.

**Manueller Modus** (Schalter „Manuell zählen“, falls die KI versagt): Die Kamera ändert den Spielstand dann nicht
mehr. Nach jedem Stoß die versenkten Kugeln antippen; wer nichts Eigenes versenkt hat, drückt „Zug wechseln“. Bei
offenem Tisch bekommt das Team, das gerade dran ist, die Gruppe der ersten angetippten Kugel. Der Schalter bleibt
auch nach einem Neustart so.

Geht etwas schief: **„Rückgängig“** macht die letzte Änderung rückgängig, unter „Weitere Korrekturen“ lassen sich Gruppen, Restkugeln und Sieger von Hand setzen.

## 3a. Spiel-Panel und Admin-Panel

| | **Spiel-Panel** `/` (PIN 1212) | **Admin-Panel** `/admin` (PIN 1902) |
|---|---|---|
| Für | Spieler am Tisch | Einrichtung, Training |
| Inhalt | Grafik + Spielstand, Spiel starten / Neues Rack / Abbrechen, Foul, Zug wechseln, Rückgängig, Stoß verwerfen, weitere Korrekturen, **Schlagverbesserung an/aus**, **Queue an/aus** + kalibrieren, Videos der automatischen Spielaufzeichnung | Kamera, Tisch einrichten, Queue koppeln (Castcode), **QR-Code**, Bilder & Modelle (aufnehmen, Upload-Stand, Modelle vom VPS, Einzelkugel per Knopf), Systemstatus, Verlauf |

Beide im Querformat: links die **aktuelle Grafik vom Pi** (wie auf dem Beamer), rechts die Bedienung.
Der Admin darf alles, was das Spiel-Panel darf (Admin-PIN funktioniert auch auf der Startseite).
Kugeln festlegen und das KI-Training laufen nicht auf dem Pi, sondern auf der **Trainingsseite im Netz** (Abschnitt 5).

Alle Seiten (Spiel-Panel, Admin-Panel, Trainingsseite, Admin im Netz und die Seiten des Queues) sehen aus wie
Apps auf iPhone/Mac: Systemschrift, hell oder dunkel je nach Gerät, Schalter und Segmente wie in den
Einstellungen; Knöpfe reagieren schon beim Antippen. Mit „Bewegung reduzieren“, „Transparenz reduzieren“ oder
„Kontrast erhöhen“ am Gerät passen sich die Seiten an. Das gemeinsame Aussehen steht in `app/ui.css`
(Kopie für den VPS mit `scripts/kopien.sh`), keine Schriften aus dem Internet – läuft also auch offline.

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
   antwortet) und abonniert dann seine Lage: Der Queue schickt sie ca. 17× pro Sekunde per UDP an den Pi (je ein Mittelwert aus 3 geprüften Messungen),
   der Pi wiederholt das Abo jede Sekunde (Lebenszeichen + Teamfarbe für die LED). Neue IP nach
   Neustart: findet er von selbst. Zum Nachschauen gibt es weiterhin `GET http://<queue>/api/lage`.
5. Einmal kalibrieren: Queue an die lange Bande legen, Spitze zeigt in der Grafik nach rechts →
   „Queue kalibrieren“ (Spiel- oder Admin-Panel). Dreht er sich falsch herum: „Drehrichtung umkehren“.

**An/aus:** Schalter „Queue mit Gyroskop“ im Spiel-Panel (bzw. „Queue benutzen“ im Admin-Panel).
Aus = der Pi sucht und fragt nichts ab. Details zur Firmware: `cue_sensor/README.md`.

**Firmware-Update ohne Kabel:** Admin-Panel → Queue → „Firmware des Queues“. „Nach Update suchen“
zeigt die Version auf dem Queue und die neueste auf GitHub, „Neueste Version installieren“ lädt
`queue-ota-<board>.bin` aus dem Release und schickt sie per WLAN an den Queue (LED blinkt blau,
danach Neustart). Eigene Builds: „Eigene .bin hochladen“ (`cue_sensor/.pio/build/<board>/firmware.bin`).
Geht etwas schief, läuft die alte Firmware weiter.

## 3c. Spielaufzeichnung

**Jedes Rack wird automatisch aufgezeichnet** – vom Spielstart bzw. „Neues Rack“ bis zum Sieg oder Abbruch.
Gespeichert werden nur die Kugeln und der Spielstand je Zug (keine Kamerabilder, keine Grafik), das kostet
den Pi praktisch nichts. Ist das Rack vorbei, schickt der Pi die Züge zur **Trainingsseite (VPS)** und löscht
sie danach; der VPS baut daraus ein kurzes Video, in dem die Kugeln von Zug zu Zug rollen (gleiche Grafik wie
auf dem Beamer).

Videos: Spiel-Panel → „Spielaufzeichnung“ → „Ansehen“. Das Video kommt direkt vom VPS (unter einer nicht
erratbaren Adresse), der Pi streamt nichts. Auf dem VPS bleiben die neuesten 200 Videos; die Liste steht
auch auf seiner Admin-Seite.

**Ohne Trainingsseite** baut der Pi das Video selbst – nach dem Rack, mit niedrigster Priorität, damit die
Erkennung nicht leidet. Dateien dann in `data/recordings/`. Stürzt der Pi mitten im Rack ab, wird die
angefangene Aufzeichnung beim nächsten Start abgeschlossen.

## 4. Ohne Tisch testen (Simulator)

Admin-Panel → „Kamera“ → **Demo** ohne Bildadresse. Dann läuft ein simuliertes Spiel in Schleife,
der Tisch ist schon eingerichtet.

Simulierter Queue: `docker compose --profile sim up -d fake-cue`, im Admin-Panel Castcode **SMQ234**
eintragen, im Spiel-Panel die Schlagverbesserung einschalten.

## 5. KI-Chip: Bilder sammeln, auf dem VPS festlegen und trainieren

Die Erkennung läuft in **drei Stufen** (`app/coral_detector.py`, `app/kugel_merkmale.py`):

| Stufe | Was | Wo | gelernt mit |
|---|---|---|---|
| 1. Kugeln finden | „hier liegt eine Kugel“ | KI-Chip (YOLOv8n, 1 Klasse) | `train_coral.py` (Colab) → `billard_edgetpu.tflite` |
| 2. gestreift? | Weiße / schwarze 8 / voll / halb | **KI-Chip**, wenn ein Klassen-Modell da ist – sonst Pi (Weiß-, Schwarz-, Farbanteil der Kugel) | `train_klassen.py` (Colab) → `billard_klassen_edgetpu.tflite`; Rückfall: „Farben lernen“ auf dem VPS → `billard_merkmale.json` |
| 3. Farbe | gelb, blau, rot, violett, orange, grün, weinrot | wie Stufe 2 (Pi: mittlere Farbe der Kugel) | wie Stufe 2 |

Aus Art und Farbe ergibt sich die Nummer (z. B. halb + rot = 11); jede Nummer gibt es nur einmal.
Stufe 1 muss nur Kugeln finden – das lernt das Modell mit viel weniger Bildern als 16 Nummern.
Stufe 2 + 3 sehen die Kugel in voller Auflösung. Auf dem KI-Chip bekommt ein kleines Netz einen
Ausschnitt um jede Kugel und liefert direkt die Nummer – das entlastet den Prozessor des Pi. Ohne
dieses Modell (oder wenn es nicht lädt) rechnet der Pi mit Statistik, die der VPS aus euren Bildern lernt.
Ältere Modelle mit 16 Kugelnummern gehen weiter (davon wird nur „Kugel ja/nein“ benutzt).
Ohne Stick oder ohne Modell wird nichts gezählt – Bilder werden trotzdem aufgenommen.

**Der Pi trainiert nichts.** Er nimmt nur Bilder auf und erkennt die Kugeln. Festlegen, Farben lernen und
Trainieren laufen auf der **Trainingsseite im Netz** (VPS, `training_server/`, Abschnitt 5a) bzw. in Google
Colab – so bleibt der Pi frei für Erkennung und Beamer.

**So lernt das System euren Tisch:**

1. **Bilder speichern (am Tisch, kostet keine Zeit):** im Spiel-Panel „Bild speichern“ – oder dort
   den Schalter **„Nach jedem Stoß automatisch“** einschalten: Sobald der Tisch nach einem Stoß
   2 Sekunden ruhig ist, wird ein Bild gespeichert (höchstens eins alle 8 Sekunden, geht auch ohne
   KI-Chip). Auf dem Beamer kommt kurz „Bild gespeichert“. Außerdem: Admin-Panel → „Bilder & Modelle“
   → „Jetzt ein Bild aufnehmen“, oder **Einzelkugel per Knopf** (eine Kugel, Nummer am Pi gewählt –
   die Stelle findet die Trainingsseite selbst, kein Mensch muss diese Bilder festlegen).
2. **Hochladen (von selbst):** Der Pi schickt jedes Bild zur Trainingsseite und **löscht es danach auf
   dem Pi**. Ohne Internet versucht er es später wieder.
3. **Kugeln festlegen – auf der Trainingsseite:** Jeder Trainer meldet sich dort mit seinem Code an;
   jedes Bild legen mehrere Personen unabhängig voneinander fest, die Mehrheit zählt (Abschnitt 5a).
4. **Farben lernen (Stufe 2 + 3):** auf der Admin-Seite der Trainingsseite „Farben aus fertigen Bildern
   lernen“ (Sekunden). Unter 50 festgelegten Kugeln wird nichts veröffentlicht.
5. **Kugel-/Klassen-Modell für den KI-Chip:** Trainings-ZIP der Trainingsseite → `training/train_coral.py`
   bzw. `train_klassen.py` in Google Colab (`training/README.md`) → die fertigen `.tflite` auf der
   Admin-Seite der Trainingsseite hochladen.
6. **Der Pi holt alles von selbst:** Alle paar Minuten fragt er die Trainingsseite nach neuen Modellen,
   prüft sie und nimmt sie sofort. Im Admin-Panel → „Bilder & Modelle“ steht, was er wann übernommen hat.
   „Von Hand hochladen“ geht dort weiterhin (z. B. ohne Trainingsseite).

**Ohne Trainingsseite** (`TRAINING_UPLOAD_URL`/`TRAINING_UPLOAD_KEY` leer) bleiben die Bilder auf dem Pi
(`data/dataset/`, aufgenommen wird, solange mehr als 1 GB frei ist); Modelle dann von Hand hochladen.
**Alle Bilder löschen:** Admin-Panel → „Bilder & Modelle“, mit Rückfrage – auch Bilder, die noch nicht
hochgeladen sind. Was schon auf der Trainingsseite ist, bleibt dort; die Modelle bleiben.

Test des Sticks:
```bash
docker compose exec billard python3 coral_check.py
```
Erwartet: `[OK] Treiber findet den Stick` und – sobald ein Modell da ist – die Zeit pro Bild.

Technik: Googles Coral-Paketquelle gibt es nicht mehr. Der Treiber (`libedgetpu`) und
`tflite_runtime` für Python 3.11 kommen von den gepflegten Builds unter github.com/feranick
(beide gegen TensorFlow 2.17.1, im Dockerfile mit Prüfsummen festgelegt).

## 5a. Trainingsseite im Netz (VPS): festlegen und lernen

Der Pi schickt jedes gespeicherte Bild automatisch auf den Server (`training_server/`) und löscht es danach.
Jeder Trainer meldet sich dort mit seinem Code an.
Jedes Bild legen mehrere Personen (Standard 3, `FESTLEGUNGEN`) unabhängig voneinander fest, ohne
Vorschläge zu sehen; je Kugel zählt die Mehrheit. Einzelkugel-Bilder legt dort nur die Bilderkennung
fest (nicht sicher erkannte werden aussortiert) – Trainer bekommen sie nicht.
Auf der Admin-Seite: Trainings-ZIP für Colab, **„Farben aus fertigen Bildern lernen“** und
**„Modell hochladen“** (`.tflite` aus Colab) – der Pi holt beides von selbst. Einrichtung: `training_server/README.md`.

## 6. Tests

Wie gut die Erkennung ist, hängt vom trainierten Modell ab (Abschnitt 5). Beim Farben-Lernen zeigt die
Trainingsseite, wie viele zurückgehaltene Kugeln richtig erkannt wurden.

**Automatisch auf GitHub** (blockiert das Mergen nicht):

| Wann | Workflow | Was geprüft wird | Dauer |
|---|---|---|---|
| jeder Pull Request | **Formprüfung** (`check.yml`) | Syntax: Python (ruff, compileall), Shell-Skripte (shellcheck), beide `docker-compose.yml`, JavaScript in `.js` und HTML-Seiten, Workflows (actionlint) | ca. 30 s |
| nach dem Merge (Push auf `main`) | **Tests** (`test.yml`) | alle Tests unten + Tests der Trainingsseite, Firmware-Build für alle Boards | einige Minuten |

Ein rotes X nach dem Merge heißt: Fehler im nächsten Pull Request beheben. Beides lässt sich auch von Hand
unter **Actions** starten.

Tests selbst laufen lassen (im Ordner `app/`):
```bash
python3 -m sim.test_game          # Regeln + simuliertes Spiel (Kugeln aus dem Simulator)
python3 -m sim.test_features      # Stoßvorschlag, Gyroskop, Game-Bar, Aufzeichnung
python3 -m sim.test_coral         # KI-Chip (Stufe 1-3), Bilder aufnehmen, nach dem Upload löschen
python3 -m sim.test_merkmale      # Stufe 2 + 3: gestreift? Farbe? lernen und prüfen
python3 -m sim.test_auth          # Anmeldung (Admin-/Spiel-PIN, Sitzungen, Sperre)
python3 -m sim.test_cue_link      # Queue: Castcode, Suche im WLAN, Abfrage der Lage, an/aus
python3 -m sim.test_cue_update    # Queue: Firmware-Update über WLAN (simulierter Queue + GitHub)
python3 -m sim.test_panels        # Spiel-/Admin-Panel (PINs), QR-Code und Meldungen auf dem Beamer
```
