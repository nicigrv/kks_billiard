# Gyroskop im Queue

ESP32 + MPU6050 im Queue-Ende. Der Pi fragt darüber die **Richtung des Queues** ab und zeigt
auf dem Beamer den hellen Queue (echte Lage) neben dem dunklen (optimale Lage) und den grünen Haken.

## Teile
- Seeed XIAO ESP32-C3 (klein, passt in ein ausgebohrtes Queue-Ende) oder ein anderes ESP32-Board
- MPU6050-Modul (GY-521)
- LiPo-Akku 3,7 V + Lade-/Schutzplatine, Schalter

## Einbau
- Platine fest einbauen (wackelt sie, wackelt der Winkel).
- Pfeil **X** auf dem MPU6050 zeigt Richtung **Queue-Spitze** (sonst `AXIS_SIGN_X -1` in `include/config.h`).

## Flashen
`pio run -e xiao_esp32c3 -t upload` (bzw. `-e esp32dev`). WLAN-Daten müssen **nicht** in den Code –
die Einrichtung läuft übers Handy.

## Einrichten (Captive Portal + Castcode)
1. Queue einschalten und **2 Sekunden ruhig hinlegen** (Gyroskop-Nullpunkt).
2. Ohne gespeichertes WLAN öffnet der Queue den Hotspot **„Billard-Queue-XXXX“**. Mit dem Handy
   verbinden – die Einrichtungsseite öffnet sich von selbst (sonst `http://192.168.4.1`).
3. **WLAN wählen**, Passwort eingeben, „Weiter“.
4. Der Queue zeigt seinen **Castcode** (6 Zeichen, z. B. `K7P2QM`). Im **Admin-Portal** des Pi unter
   „KI-Schlagverbesserung → Queue mit Gyroskop“ eintragen → „Koppeln“.
5. Auf dem Handy den **Haken** „Castcode ist im Admin-Portal eingetragen“ setzen → „Verbinden“.
   Der Queue verbindet sich mit dem WLAN, der Hotspot verschwindet.
6. Im Portal steht nach wenigen Sekunden **„Queue: verbunden“**. Dann einmal kalibrieren: Queue flach
   an die lange Bande legen, Spitze zeigt in der Grafik nach rechts → „Queue kalibrieren“.

Der Castcode bleibt für immer gleich (auch nach Neustart oder WLAN-Wechsel). Bekommt der Queue eine
neue IP, findet der Pi ihn über den Castcode von selbst wieder.

**Neu einrichten** (anderes WLAN): Queue beim Einschalten **senkrecht halten** (Spitze nach oben) –
dann öffnet er den Hotspot. Oder im WLAN seine Seite `http://<queue-ip>` → „WLAN neu einrichten“.
Ist das gespeicherte WLAN nicht erreichbar, öffnet er den Hotspot ebenfalls (und versucht es alle
5 Minuten erneut).

## So reden Pi und Queue
- **Suche:** Der Pi schickt `{"suche": "<Castcode>"}` per UDP-Broadcast an Port 5005; nur der Queue
  mit diesem Code antwortet mit seiner IP.
- **Abfrage:** ca. 25× pro Sekunde `GET http://<queue>/api/lage` mit Kopfzeile `X-Castcode`.
  Antwort: `{"castcode":"K7P2QM","seq":123,"yaw":37.5,"pitch":-4.1,"still":0,"bat":3.91,"ms":123456}`.
  Ohne passenden Castcode antwortet der Queue nicht (403).
- Zusätzlich im Netz als `queue-<castcode>.local` (mDNS).

## Grenzen (ehrlich)
- Ein Gyroskop misst nur **Drehungen**, keine Position. Der helle Queue wird deshalb immer hinter die
  Weiße gezeichnet; verglichen wird nur der **Winkel**.
- Ohne Magnetometer driftet der Winkel langsam (typisch unter 1° pro Minute). Liegt der Queue ruhig,
  wird nachgeführt. Wird die Abweichung sichtbar: kurz neu kalibrieren.
- Ohne Hardware testen: `python3 -m sim.fake_cue` (im Ordner `app/`), Castcode `SMQ234` im Portal eintragen.
