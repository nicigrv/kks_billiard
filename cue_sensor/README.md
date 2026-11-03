# Gyroskop im Queue

ESP32 + MPU6050 im Queue-Ende. Sendet die Richtung des Queues per WLAN an den Pi
(UDP-Port 5005, Broadcast). Der Pi zeigt damit auf dem Beamer den hellen Queue
(echte Lage) neben dem dunklen Queue (optimale Lage) und den grünen Haken.

## Teile
- Seeed XIAO ESP32-C3 (klein, passt in ein ausgebohrtes Queue-Ende) oder ein anderes ESP32-Board
- MPU6050-Modul (GY-521)
- LiPo-Akku 3,7 V + Lade-/Schutzplatine, Schalter

## Einbau
- Platine fest einbauen (wackelt sie, wackelt der Winkel).
- Pfeil **X** auf dem MPU6050 zeigt Richtung **Queue-Spitze** (sonst `AXIS_SIGN_X -1` in `include/config.h`).

## Flashen
1. `include/config.h`: WLAN-Name und Passwort eintragen.
2. `pio run -e xiao_esp32c3 -t upload` (bzw. `-e esp32dev`).
3. Nach dem Einschalten den Queue 2 Sekunden **ruhig hinlegen** (Gyroskop-Nullpunkt).

## Am Pi einrichten
Im Admin-Portal unter **KI-Schlagverbesserung → Queue mit Gyroskop**:
1. Status muss „verbunden“ zeigen.
2. Queue flach an die lange Bande legen, Spitze zeigt in der Grafik nach **rechts** → „Queue kalibrieren“.
3. Queue drehen: Dreht sich der helle Queue in der Grafik falsch herum → „Drehrichtung umkehren“.

## Grenzen (ehrlich)
- Ein Gyroskop misst nur **Drehungen**, keine Position. Der helle Queue wird deshalb immer
  hinter die Weiße gezeichnet; verglichen wird nur der **Winkel**.
- Ohne Magnetometer driftet der Winkel langsam (typisch unter 1° pro Minute). Liegt der
  Queue ruhig, wird nachgeführt. Wird die Abweichung sichtbar: kurz neu kalibrieren.
- Ohne Hardware testen: `python3 -m sim.fake_cue` im Ordner `app/` sendet einen simulierten Queue.
