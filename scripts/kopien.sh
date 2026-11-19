#!/usr/bin/env bash
# Gemeinsamer Code von Pi und Trainingsserver: training_server/ bekommt Kopien aus app/,
# damit der Server genauso lernt (Farben) und genauso zeichnet (Videos der Spielaufzeichnung)
# wie der Pi - und als eigener Ordner (Release billard-server.zip) fuer sich laeuft.
#
#   scripts/kopien.sh            Kopien aktualisieren (nach Aenderungen an diesen Dateien in app/)
#   scripts/kopien.sh --pruefen  nur pruefen, ob alle Kopien gleich sind (Formpruefung in CI)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Quelle in app/ -> Ziel in training_server/
DATEIEN=(
  "ballnum.py:ballnum.py"
  "geometry.py:geometry.py"
  "notices.py:notices.py"
  "planner.py:planner.py"
  "scene.py:scene.py"
  "replay.py:replay.py"
  "kugel_merkmale.py:kugel_merkmale.py"
  "models/start_merkmale.json:start_merkmale.json"
)

fehler=0
for paar in "${DATEIEN[@]}"; do
  quelle="$ROOT/app/${paar%%:*}"
  ziel="$ROOT/training_server/${paar##*:}"
  if [ "${1:-}" = "--pruefen" ]; then
    if ! cmp -s "$quelle" "$ziel"; then
      echo "::error file=training_server/${paar##*:}::weicht von app/${paar%%:*} ab - scripts/kopien.sh ausfuehren"
      fehler=1
    fi
  else
    cp "$quelle" "$ziel"
    echo "kopiert: app/${paar%%:*} -> training_server/${paar##*:}"
  fi
done
if [ "$fehler" = 1 ]; then
  exit 1
fi
if [ "${1:-}" = "--pruefen" ]; then
  echo "alle Kopien gleich"
fi
