# Billard-Training im Netz

Die Bilder vom Tisch landen automatisch auf dem Server (und werden danach auf dem Pi gelöscht). Jeder
Trainer legt sie zuhause mit seinem eigenen Code fest. Der Server lernt daraus die Farben (Stufe 2 + 3),
der Admin lädt die Trainings-ZIP für `training/train_coral.py` und lädt die fertigen Modelle hier hoch –
der Pi holt sich alles von selbst. **Auf dem Pi wird nichts festgelegt oder gelernt.**

```
Pi am Tisch ──(„Bild speichern“ / „nach jedem Stoß“)──▶ https://billard.server.greylo.de/api/upload
Trainer zuhause ──▶ https://billard.server.greylo.de        Code (6 Ziffern)
Admin           ──▶ https://billard.server.greylo.de/admin  greylo-Login, Gruppe „admins“
Pi am Tisch ◀──(alle 5 Minuten: neue Modelle?)── GET /api/modelle, /api/modell?name=…
```

## Ablauf der Bilder

1. **Festlegen, dreimal:** Jedes Bild legen **drei verschiedene Personen** unabhängig voneinander fest
   (Kugel antippen → Nummer). Niemand sieht Vorschläge – weder vom KI-Chip noch von den anderen.
   Bilder, denen nur noch wenig fehlt, kommen zuerst; an einem Bild arbeiten nie mehr Leute gleichzeitig,
   als noch Festlegungen fehlen. „Später“ überspringt, „Unbrauchbar“ sortiert aus (Arm im Bild, verwackelt).
2. **Ergebnis:** Nach drei Festlegungen ist das Bild **fertig**. Je Kugel zählt die Mehrheit: mindestens zwei
   haben dieselbe Nummer an fast derselben Stelle gesetzt (0,8 Kugelradien), die Position ist der Mittelwert.
   Haben alle drei dasselbe festgelegt, ist das Bild **einstimmig**.
3. **ZIP:** Admin-Seite → „ZIP laden (fertige Bilder)“. „Nur einstimmige“ nimmt nur Bilder ohne Abweichung,
   „inkl. unfertige“ auch Bilder, die erst ein- oder zweimal festgelegt sind (bei zwei zählt nur, was beide gleich haben).

Am Tisch schon festgelegte Bilder werden genauso dreimal online festgelegt; die Kugeln vom Tisch bleiben nur
in der Datenbank stehen. Wie oft jedes Bild festgelegt wird, stellt `FESTLEGUNGEN` ein (Standard 3).

**Einzelkugel-Bilder** (am Pi im Admin-Panel → Bilder & Modelle → Einzelkugel per Knopf → „Bild jetzt aufnehmen“)
brauchen **keine Menschen**: Die Nummer kommt vom Pi, die Stelle der Kugel findet die Bilderkennung hier auf dem
Server (`einzelkugel.py`):
- Vergleich mit dem Bild des leeren Tisches; Belichtung, Weißabgleich, Schatten von Personen und eine leicht
  verrutschte Kamera werden vorher ausgeglichen. Ohne leeren Tisch: Vergleich mit der Tuchfarbe der Umgebung.
- Kugel = ein runder Fleck in Kugelgröße. Hand, Arm, Queue oder Schattenkanten zählen nicht und stören auch
  nicht, wenn die Kugel daneben frei liegt.
- Mehrere kugelähnliche Stellen: Es entscheidet die Farbe, die zur Nummer passt.

Gefunden → das Bild ist sofort **fertig** (festgelegt von „Bilderkennung“) und kommt in die ZIP. Nicht sicher
gefunden (keine Kugel, zwei gleiche …) → das Bild wird **von selbst aussortiert**. Trainer bekommen
Einzelkugel-Bilder nie. Was aus älteren Versionen noch offen war, holt der Server beim Start im Hintergrund nach.
Auf der Admin-Seite sind die Kreise in den Vorschaubildern eingezeichnet; „Nochmal erkennen“ bei einem
aussortierten Bild probiert es erneut. Braucht OpenCV, numpy und Pillow – das Dockerfile installiert alles.

Tastatur: Enter = speichern, Ziffern = Nummer (zweistellig schnell hintereinander: 1, 2 = 12),
W = Weiße, Entf = Kugel entfernen, Esc = abwählen.

## Modelle für den Pi

Admin-Seite → **„Modelle für den Pi“**:

| Datei | woher | was |
|---|---|---|
| `billard_merkmale.json` | **„Farben aus fertigen Bildern lernen“** (Sekunden) oder hochladen | Stufe 2 + 3 als Statistik (gestreift? Farbe?) |
| `billard_edgetpu.tflite` | `training/train_coral.py` in Colab → hier hochladen | Stufe 1: Kugeln finden (KI-Chip) |
| `billard_klassen_edgetpu.tflite` | `training/train_klassen.py` in Colab → hier hochladen | Stufe 2 + 3 auf dem KI-Chip |

- **Farben lernen** nimmt alle fertigen Bilder (je Kugel die Mehrheit) und prüft dabei ehrlich an
  zurückgehaltenen Kugeln. Unter 50 festgelegten Kugeln wird nichts veröffentlicht – die bisherigen Farben bleiben.
  Nach Änderungen am Licht neu lernen.
- **Hochladen:** `.tflite` und `.json` (auch mehrere auf einmal). Der Server erkennt die Art wie der Pi
  (kleines Modell = Klassen-Modell) und prüft die Datei. Die vorherige Fassung bleibt als `.vorher` in
  `data/modelle/` liegen.
- **Der Pi holt** alle 5 Minuten die Liste (`GET /api/modelle`, mit `X-Upload-Key`), lädt Neues
  (`GET /api/modell?name=…`), prüft die Prüfsumme und die Datei und nimmt sie sofort. In der Tabelle steht,
  wann der Pi eine Datei abgeholt hat; im Admin-Panel des Pi unter „Bilder & Modelle“, was er übernommen hat.
- Braucht OpenCV – das Dockerfile installiert es (nach dem Update einmal neu bauen: `docker compose up -d --build`).
  `kugel_merkmale.py` und `start_merkmale.json` sind Kopien aus `app/` (die Formprüfung in CI achtet darauf).

## Spielaufzeichnung: Videos baut der Server

Der Pi zeichnet **jedes Rack** auf – nur die Kugeln und den Spielstand je Zug – und schickt es nach dem Rack
hierher (`POST /api/aufzeichnung`, mit `X-Upload-Key`). Der Server baut daraus das Video im Hintergrund
(nacheinander, niedrigste Priorität) mit derselben Grafik wie der Beamer (`replay.py`/`scene.py`, Kopien aus
`app/`). Das Spiel-Panel am Pi zeigt die Liste (`GET /api/aufzeichnungen`) und spielt die Videos direkt von hier
ab: `https://…/video/<schlüssel>.mp4` – ohne Anmeldung, aber die Adresse ist nicht zu erraten.
Es bleiben die neuesten 200 Videos (`MAX_VIDEOS` in `aufzeichnung.py`); die Admin-Seite listet sie unter
„Spielaufzeichnungen“. Braucht pygame und ffmpeg – das Dockerfile installiert beides (nach dem Update einmal neu
bauen: `docker compose up -d --build`). Ablage: `data/videos/`.

**Gemeinsamer Code:** `ballnum.py`, `geometry.py`, `notices.py`, `planner.py`, `scene.py`, `replay.py`,
`kugel_merkmale.py`, `start_merkmale.json` und `ui.css` (gemeinsames Aussehen aller Seiten) sind Kopien aus `app/`. Nach Änderungen dort
`scripts/kopien.sh` ausführen; die Formprüfung meldet, wenn eine Kopie abweicht.

## Trainings-ZIP direkt in Google Colab laden

Ohne greylo-Login, mit eigenem Schlüssel (`EXPORT_KEY` in `.env`, legt `update.sh server` an und zeigt ihn an):

```
!curl -fL -H "X-Export-Key: <EXPORT_KEY>" -o daten.zip https://billard.server.greylo.de/api/export.zip
```

`?modus=einig` (nur einstimmige) bzw. `?modus=alle` (inkl. unfertige) wie auf der Admin-Seite.
Ohne `EXPORT_KEY` in `.env` ist der Export aus. Den Schlüssel nicht weitergeben – damit kann man alle fertigen Bilder laden.

## Einrichten (VPS)

```bash
cd /mnt/volume/apps/billard-training
echo "UPLOAD_KEY=$(openssl rand -hex 32)" > .env && chmod 600 .env
docker compose up -d --build
```

Authelia: Regel für `billard.server.greylo.de` mit `resources: ['^/admin([/?].*)?$', '^/api/admin/.*$']`,
`subject: group:admins`, `policy: two_factor`. Alles andere läuft ohne Authelia (eigene Codes).

## Pi verbinden

Im Ordner des Billard-Systems eine Datei `.env` anlegen (neben `docker-compose.yml`):

```
TRAINING_UPLOAD_URL=https://billard.server.greylo.de
TRAINING_UPLOAD_KEY=<UPLOAD_KEY vom Server>
```

Dann `docker compose up -d`. Der Pi schickt alle 15 Sekunden neue Bilder hoch (auch alle, die schon
da sind). Ohne Internet wartet er und versucht es später. Im Admin-Panel des Pi steht hinter den
Trainingsbildern „Trainingsseite: alles hochgeladen“.

## Tests

```bash
python3 test_server.py
```
