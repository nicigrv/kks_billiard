# Gyroskop im Queue

ESP32 + MPU6050 im Queue-Ende. Der Pi fragt darüber die **Richtung des Queues** ab und zeigt
auf dem Beamer den hellen Queue (echte Lage) neben dem dunklen (optimale Lage) und den grünen Haken.

**Optional:** Ohne Queue läuft alles andere genauso. Ist einer gekoppelt, lässt er sich im
Spiel-Panel mit dem Schalter „Queue mit Gyroskop“ ein- und ausschalten (aus = der Pi sucht und fragt nichts ab).

So reden Pi und Queue miteinander (alles im WLAN per UDP, erkannt am **Castcode**):

| Wer | Was |
|---|---|
| Pi → alle | UDP-Broadcast an Port 5005: `{"suche": "K7P2QM"}` |
| Queue → Pi | nur der Queue mit diesem Castcode antwortet: `{"castcode": "K7P2QM", "ip": "192.168.88.57", "port": 80}` |
| Pi → Queue | **Abo** an Port 5005: `{"abo": "K7P2QM", "farbe": null}` (während eines Spiels die Teamfarbe, z. B. `[0, 90, 255]`) |
| Queue → Pi | ab dann **von selbst** ca. 50× pro Sekunde: `{"castcode": "K7P2QM", "seq": 123, "yaw": 37.5, "pitch": -4.2, "still": 0, "bat": 3.91, "rssi": -55, "knopf": 0}` |
| Pi → Queue | jede Sekunde das Abo erneut (Lebenszeichen), bei Zugwechsel sofort mit der neuen Farbe |

Kommt 3 s kein Lebenszeichen, hört der Queue auf zu senden (LED rot). Kommen 3 s keine Daten, sucht der
Pi den Queue neu (z. B. neue IP nach Neustart). Zum Nachschauen gibt es weiterhin `GET http://<queue>/api/lage`.

## LED und Knopf (M5Stack Atom)

| LED | Bedeutung |
|---|---|
| weiß | Start: Gyroskop-Nullpunkt, Queue ruhig liegen lassen |
| rot blinkend | Einrichtungs-Hotspot offen |
| **rot** | keine Verbindung zum Pi |
| **grün** | verbunden, es läuft kein Spiel |
| **blau** / **orange** | Spiel läuft: Team A (blau) bzw. Team B (orange) ist dran |

**Knopf** (auf dem Atom gedrückt), wenn die LED **grün** ist: Der Pi startet ein Spiel **Team A gegen
Team B**, Team A bricht an. Während eines Spiels oder ohne Verbindung macht der Knopf nichts.
Atom Lite/Matrix: LED G27, Knopf G39. AtomS3 Lite: LED G35, Knopf G41. Andere Boards:
`LED_PIN`/`BTN_PIN` in `platformio.ini` setzen (WS2812-LED, Knopf gegen GND).

## Teile
- Seeed XIAO ESP32-C3 (klein, passt in ein ausgebohrtes Queue-Ende) oder ein anderes ESP32-Board
- MPU6050-Modul (GY-521)
- **oder M5Stack Atom** (Atom Lite, Atom Matrix, AtomS3, AtomS3 Lite) mit Gyro-Accelerometer
  MPU6050 (GY-521) am Grove-Port – Akku und Lader braucht es trotzdem

### M5Stack Atom
| Grove-Kabel | MPU6050 (GY-521) | Atom Lite / Matrix | AtomS3 / AtomS3 Lite |
|---|---|---|---|
| schwarz | GND | GND | GND |
| rot | VCC | 5V | 5V |
| gelb | SCL | G32 | G1 |
| weiß | SDA | G26 | G2 |

Das GY-521 hat einen eigenen Spannungsregler und verträgt die 5 V vom Grove-Port.
Ist am Grove-Port nichts angeschlossen, nimmt der Atom Matrix bzw. AtomS3 seinen **eingebauten**
Sensor (MPU6886). Dann zeigt die lange Seite des Atom (X-Achse) zur Queue-Spitze; stimmt die
Richtung nicht, `AXIS_SIGN_X -1` in `include/config.h`. Nicht unterstützt: der BMI270 im AtomS3R.
- LiPo-Akku 3,7 V + Lade-/Schutzplatine, Schalter

## Einbau
- Platine fest einbauen (wackelt sie, wackelt der Winkel).
- Pfeil **X** auf dem MPU6050 zeigt Richtung **Queue-Spitze** (sonst `AXIS_SIGN_X -1` in `include/config.h`).

## Flashen
`pio run -e xiao_esp32c3 -t upload` (bzw. `-e esp32dev`, `-e m5stack-atom` für Atom Lite/Matrix,
`-e m5stack-atoms3` für AtomS3).

In **VS Code**: Erweiterung „PlatformIO IDE“ installieren, **nur den Ordner `cue_sensor`** öffnen
(File > Open Folder). Unten in der blauen Leiste das Board wählen (`env:m5stack-atom` usw.), dann
Pfeil = Upload, Stecker = serieller Monitor. Die LED-Bibliothek (Adafruit NeoPixel) lädt PlatformIO selbst. Dort steht nach dem Start, welcher Sensor gefunden wurde. WLAN-Daten müssen **nicht** in den Code –
die Einrichtung läuft übers Handy.

## Einrichten (Captive Portal + Castcode)
1. Queue einschalten und **2 Sekunden ruhig hinlegen** (Gyroskop-Nullpunkt).
2. Ohne gespeichertes WLAN öffnet der Queue den Hotspot **„Billard-Queue-XXXX“**. Mit dem Handy
   verbinden – die Einrichtungsseite öffnet sich von selbst (sonst `http://192.168.4.1`).
3. **WLAN wählen**, Passwort eingeben, „Weiter“.
4. Der Queue zeigt seinen **Castcode** (6 Zeichen, z. B. `K7P2QM`). Im **Admin-Panel** des Pi unter
   „KI-Schlagverbesserung → Queue mit Gyroskop“ eintragen → „Koppeln“.
5. Auf dem Handy den **Haken** „Castcode ist im Admin-Panel eingetragen“ setzen → „Verbinden“.
   Der Queue verbindet sich mit dem WLAN, der Hotspot verschwindet.
6. Im Admin-Panel steht nach wenigen Sekunden **„Queue: verbunden“**. Dann einmal kalibrieren: Queue flach
   an die lange Bande legen, Spitze zeigt in der Grafik nach rechts → „Queue kalibrieren“.

Der Castcode bleibt für immer gleich (auch nach Neustart oder WLAN-Wechsel). Bekommt der Queue eine
neue IP, findet der Pi ihn über den Castcode von selbst wieder.

## Demo-Anzeige (im Hotspot, ohne Pi)
Ganz oben auf der Einrichtungsseite läuft eine **Live-Demo**, damit man den Queue sofort ausprobieren
kann – auch ganz ohne WLAN und ohne Pi:

- **Draufsicht:** Tisch mit Weißer, der **helle Queue dreht sich mit** (Richtung).
- **„Richtung hier auf 0 setzen“:** Queue flach halten, Spitze nach rechts, drücken. Danach erscheint
  der **dunkle Ziel-Queue**. Liegt der Queue bis ±2° darauf, kommt der **grüne Haken** – wie später
  auf dem Beamer.
- **Seitenansicht:** Neigung (Spitze nach unten = negativ).
- **Sensor:** „ruhig“ / „bewegt“, „fehlt!“ wenn keine Werte vom Gyro-Sensor kommen (Verkabelung prüfen).

Großansicht: `http://192.168.4.1/demo`. Im WLAN gibt es dieselbe Anzeige unter `http://<queue-ip>/demo`
(Link auf der Statusseite). Der Nullpunkt gilt nur für die Demo auf diesem Handy, der Pi kalibriert
selbst („Queue kalibrieren“). Ist die Drehrichtung falsch herum (Queue dreht in der Anzeige entgegengesetzt),
die Platine andersherum einbauen oder `AXIS_SIGN_X` in `include/config.h` umdrehen.

**Neu einrichten** (anderes WLAN): Queue beim Einschalten **senkrecht halten** (Spitze nach oben) –
dann öffnet er den Hotspot. Oder im WLAN seine Seite `http://<queue-ip>` → „WLAN neu einrichten“.
Ist das gespeicherte WLAN nicht erreichbar, öffnet er den Hotspot ebenfalls (und versucht es alle
5 Minuten erneut).
