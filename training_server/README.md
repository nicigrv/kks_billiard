# Billard-Training im Netz

Die Bilder vom Tisch landen automatisch auf dem Server. Jeder Trainer legt sie zuhause mit seinem
eigenen Code fest, am Ende lädt der Admin die Trainings-ZIP für `training/train_coral.py`.

```
Pi am Tisch ──(„Bild speichern“ / „nach jedem Stoß“)──▶ https://billard.server.greylo.de/api/upload
Trainer zuhause ──▶ https://billard.server.greylo.de        Code (6 Ziffern)
Admin           ──▶ https://billard.server.greylo.de/admin  greylo-Login, Gruppe „admins“
```

## Ablauf der Bilder

1. **Runde 1 – festlegen:** Jedes neue Bild wird genau einmal festgelegt (Kugel antippen → Nummer).
   Arbeiten mehrere gleichzeitig, bekommt jeder ein anderes Bild. „Später“ überspringt,
   „Unbrauchbar“ sortiert aus (Arm im Bild, verwackelt).
2. **Runde 2 – kontrollieren:** Erst wenn in Runde 1 nichts mehr frei ist, kommen Bilder aus Runde 2 –
   immer von einer **anderen** Person. Sie sieht die Kugeln aus Runde 1 und drückt
   „✓ Stimmt – bestätigen“ oder korrigiert. Danach ist das Bild **fertig**.
3. **ZIP:** Admin-Seite → „ZIP laden (fertige Bilder)“. „ZIP inkl. Runde 1“ nimmt auch Bilder mit, die
   erst einmal festgelegt sind – wenn die Zeit drängt.

Am Tisch schon festgelegte Bilder kommen direkt in Runde 2.

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
