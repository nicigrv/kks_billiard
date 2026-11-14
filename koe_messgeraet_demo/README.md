# Kö-Messgerät: M5Stack Atom Lite + LIS3DH (PlatformIO)

> **Ältere Demo-Version** ohne Pi und ohne Billard-System: Das Gerät öffnet nur einen eigenen Hotspot
> und zeigt die Messwerte auf der Seite an. Die Version für den Billardtisch ist [`cue_sensor`](../cue_sensor).

Der Atom Lite öffnet ein offenes WLAN **KOE-Messgeraet** mit Captive Portal.
Nach dem Verbinden öffnet sich die Seite automatisch (sonst im Browser http://192.168.4.1 aufrufen).
Angezeigt werden Beschleunigung (g), Spitzenwert des letzten Stoßes, Kraft (F = m·a),
Neigung (Pitch/Roll) und ein Diagramm.

**Hinweis:** Der LIS3DH misst nur Beschleunigung, er hat kein Gyroskop. Die Lage (Pitch/Roll)
kommt aus der Erdbeschleunigung und stimmt nur bei ruhigem Kö. Eine Drehung um die Längsachse
des Gravitationsvektors (Gieren) ist mit diesem Sensor nicht messbar.

## Verkabelung (Grove-Port des Atom Lite)
| Grove | LIS3DH-Modul |
|---|---|
| GND (schwarz) | GND |
| 5V (rot) | VIN |
| G32 (gelb) | SCL |
| G26 (weiß) | SDA |

**Achtung:** Der Grove-Port liefert 5 V. Viele Module (z. B. Adafruit) haben einen Spannungsregler
und vertragen 3–5 V an VIN. Ohne Regler darf nur 3,3 V angelegt werden: dann VIN an den 3V3-Pin
des Atom statt an Grove-5V. Datenblatt deines Moduls prüfen!
Wird der Sensor nicht gefunden, blinkt die LED rot. Dann SDA/SCL prüfen oder in `main.cpp`
`SDA_PIN`/`SCL_PIN` tauschen.

## Kompilieren und hochladen
1. VS Code mit der Erweiterung **PlatformIO IDE** installieren.
2. ZIP entpacken, Ordner in VS Code öffnen (File > Open Folder).
3. Atom Lite per USB-C anschließen.
4. Unten in der blauen Leiste: **Haken** = Build, **Pfeil** = Upload, **Stecker** = Serieller Monitor.
5. Bei Upload-Fehlern in `platformio.ini` `upload_speed = 115200` setzen.

## Bedienung
- LED blau = bereit, grün = Nullen läuft, rot blinkend = Sensor nicht gefunden.
- **Nullen** (Button auf der Seite oder Taster am Atom): Kö ruhig und in Spielhaltung halten,
  ca. 1 s. Setzt Pitch/Roll auf 0 und kalibriert den Sensor auf 1 g.
- Achsenzuordnung: Index 0 = Kö-Längsrichtung. Bei anderer Einbaulage `AXIS_MAP`/`AXIS_SGN` in `main.cpp` ändern.
- Kö-Masse auf der Seite eintragen (Standard 0,5 kg).

## Bekannte Grenzen
- Die Kraft ist eine Schätzung (F = m · a des Kös), nicht die Kraft auf die Kugel.
- Die Seite fragt ca. 25-mal pro Sekunde ab. Der Spitzenwert wird mit 400 Hz im Gerät erfasst, kurze Stöße gehen also nicht verloren.
- Die Captive-Portal-Fenster von Handys sind manchmal eingeschränkt. Dann die Seite im normalen Browser öffnen.
- Android meldet "kein Internet" und wechselt evtl. auf Mobilfunk: "Verbunden bleiben" wählen.
