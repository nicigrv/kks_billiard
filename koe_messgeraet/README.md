# Kö-Messgerät: M5Stack Atom Matrix mit MPU6886 (PlatformIO)

> **Eigenständiges Messgerät** ohne Pi und ohne Billard-System: Das Gerät öffnet einen eigenen Hotspot
> und zeigt die Messwerte auf einer Webseite an. Die Version für den Billardtisch ist [`cue_sensor`](../cue_sensor).

Der Atom Matrix öffnet ein offenes WLAN **KOE-Messgeraet** mit Captive Portal.
Nach dem Verbinden öffnet sich die Seite automatisch (sonst im Browser http://192.168.4.1 aufrufen).
Angezeigt werden Beschleunigung (g), Spitzenwert des letzten Stoßes, Kraft (F = m·a),
Neigung (Pitch/Roll) und ein Diagramm. Die 5×5-LED-Matrix zeigt nach jedem Stoß die Stärke als Balken.

## Hardware
Keine Verkabelung nötig: Der **MPU6886** (3-Achsen-Beschleunigung + 3-Achsen-Gyroskop) ist im
Atom Matrix eingebaut.

| Teil | Pin / Adresse |
|---|---|
| MPU6886 (I2C, intern) | SDA G25, SCL G21, Adresse 0x68 |
| LED-Matrix 5×5 (WS2812) | G27 |
| Taster (Druck auf die Matrix) | G39 |

Sensor-Einstellungen: ±16 g (2048 Digits/g), 500 Hz Abtastrate, Tiefpass 218 Hz.
Abgefragt wird mit ca. 400 Hz.

## Kompilieren und hochladen
1. VS Code mit der Erweiterung **PlatformIO IDE** installieren.
2. Den Ordner `koe_messgeraet` in VS Code öffnen (File > Open Folder).
3. Atom Matrix per USB-C anschließen.
4. Unten in der blauen Leiste: **Haken** = Build, **Pfeil** = Upload, **Stecker** = Serieller Monitor.
   Die Bibliothek *Adafruit NeoPixel* lädt PlatformIO beim ersten Build selbst.
5. Bei Upload-Fehlern bleibt `upload_speed = 115200` in `platformio.ini` (ist schon so eingestellt).

## Bedienung
| Matrix | Bedeutung |
|---|---|
| blauer Punkt in der Mitte | bereit |
| ganz grün | Nullen läuft |
| Balken von unten (grün → gelb → rot) | letzter Stoß, 1 Zeile je 3 g, bleibt 2 s stehen |
| rotes X blinkt | MPU6886 antwortet nicht (Gerät neu starten) |

- **Nullen** (Button auf der Seite oder Druck auf die Matrix): Kö ruhig und in Spielhaltung halten,
  ca. 1 s. Setzt Pitch/Roll auf 0 und kalibriert den Sensor auf 1 g.
- Achsenzuordnung: Index 0 = Kö-Längsrichtung. Bei anderer Einbaulage `AXIS_MAP`/`AXIS_SGN` in `main.cpp` ändern.
- Kö-Masse auf der Seite eintragen (Standard 0,5 kg).

## Bekannte Grenzen
- Die Lage (Pitch/Roll) kommt aus der Erdbeschleunigung und stimmt nur bei ruhigem Kö.
  Das Gyroskop des MPU6886 wird hier noch nicht ausgewertet (das macht `cue_sensor` mit dem Madgwick-Filter).
- Die Kraft ist eine Schätzung (F = m · a des Kös), nicht die Kraft auf die Kugel.
- Messbereich bis 16 g. Härtere Stöße werden abgeschnitten.
- Die Seite fragt ca. 25-mal pro Sekunde ab. Der Spitzenwert wird mit 400 Hz im Gerät erfasst, kurze Stöße gehen also nicht verloren.
- Die Captive-Portal-Fenster von Handys sind manchmal eingeschränkt. Dann die Seite im normalen Browser öffnen.
- Android meldet "kein Internet" und wechselt evtl. auf Mobilfunk: "Verbunden bleiben" wählen.
