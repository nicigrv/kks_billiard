# Billard-Training im Netz

Die Bilder vom Tisch landen automatisch auf dem Server. Jeder Trainer legt sie zuhause mit seinem
eigenen Code fest, am Ende lädt der Admin die Trainings-ZIP für `training/train_coral.py`.

```
Pi am Tisch ──(„Bild speichern“ / „nach jedem Stoß“)──▶ https://billard.server.greylo.de/api/upload
Trainer zuhause ──▶ https://billard.server.greylo.de        Code (6 Ziffern)
Admin           ──▶ https://billard.server.greylo.de/admin  greylo-Login, Gruppe „admins“
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

Tastatur: Enter = speichern, Ziffern = Nummer (zweistellig schnell hintereinander: 1, 2 = 12),
W = Weiße, Entf = Kugel entfernen, Esc = abwählen.

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
