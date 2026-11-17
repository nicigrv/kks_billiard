#!/usr/bin/env bash
# Baut die Release-Pakete (läuft in GitHub Actions, geht aber auch lokal).
#   scripts/build_release.sh v0.1.2 dist
# Ergebnis in dist/:
#   billard-rasp.zip     Raspberry Pi (app/ + docker-compose.yml)
#   billard-server.zip   Trainingsserver für den VPS (training_server/)
#   billard-queue.zip    Queue-Firmware + Kö-Messgerät (Quellcode + fertige .bin für den Atom Matrix)
#   billard-gesamt.zip   das komplette Repository
#   update.sh            Installations-/Update-Skript für Pi und Server
# Braucht: git, zip, PlatformIO (pio) und esptool.
set -euo pipefail

VERSION="${1:?Version fehlt, z. B. v0.1.2}"
OUT="$(realpath -m "${2:-dist}")"
ROOT="$(git rev-parse --show-toplevel)"
BAU="$(mktemp -d)"
trap 'rm -rf "$BAU"' EXIT

rm -rf "$OUT" && mkdir -p "$OUT"
SRC="$BAU/src"
mkdir -p "$SRC"
git -C "$ROOT" archive HEAD | tar -x -C "$SRC"      # nur Dateien aus Git, keine Daten/Caches
echo "$VERSION" > "$SRC/VERSION"

packe() {   # packe <ordner-im-zip> <zipname> <dateien...>
  local top="$1" name="$2"; shift 2
  local d="$BAU/pkg/$top"
  rm -rf "$BAU/pkg" && mkdir -p "$d"
  for f in "$@"; do cp -a "$f" "$d/"; done
  (cd "$BAU/pkg" && zip -qr -X "$OUT/$name" "$top")
  echo "  $name ($(du -h "$OUT/$name" | cut -f1))"
}

echo "Pakete für $VERSION:"

# Raspberry Pi
packe billard billard-rasp.zip "$SRC/app" "$SRC/docker-compose.yml" "$SRC/README.md" \
  "$SRC/scripts/update.sh" "$SRC/VERSION"

# Server (Inhalt von training_server/ direkt im Ordner)
packe billard-training billard-server.zip "$SRC"/training_server/* "$SRC/scripts/update.sh" "$SRC/VERSION"

# Queue: Firmware für den Atom Matrix bauen und zu einer .bin zusammenfügen (Flash ab Adresse 0x0)
FW="$BAU/firmware"
mkdir -p "$FW"
baue() {   # baue <projektordner> <env> <zielname>
  local p="$1" env="$2" ziel="$3"
  cp -a "$p" "$BAU/build-$(basename "$p")"
  local b="$BAU/build-$(basename "$p")"
  pio run -d "$b" -e "$env" >/dev/null
  local o="$b/.pio/build/$env"
  local app0
  app0="$(find "$HOME/.platformio/packages/framework-arduinoespressif32" -name boot_app0.bin | head -1)"
  esptool --chip esp32 merge-bin -o "$FW/$ziel" \
    0x1000 "$o/bootloader.bin" 0x8000 "$o/partitions.bin" 0xe000 "$app0" 0x10000 "$o/firmware.bin" >/dev/null
}
baue "$SRC/cue_sensor" m5stack-atom queue-atom-matrix.bin
baue "$SRC/koe_messgeraet" m5stack-atom koe-messgeraet-atom-matrix.bin
cat > "$FW/FLASHEN.md" <<MD
# Fertige Firmware aufspielen ($VERSION)

| Datei | Programm |
|---|---|
| \`queue-atom-matrix.bin\` | Queue mit Gyroskop (\`cue_sensor\`), M5Stack Atom Matrix / Atom Lite |
| \`koe-messgeraet-atom-matrix.bin\` | Kö-Messgerät (\`koe_messgeraet\`), M5Stack Atom Matrix |

Die Dateien enthalten alles (Bootloader, Partitionen, Programm) und werden ab **Adresse 0x0** geschrieben.

**Im Browser (Chrome/Edge):** https://espressif.github.io/esptool-js/ öffnen, Atom per USB-C anstecken,
*Connect* (Baudrate 115200), Adresse \`0x0\`, Datei wählen, *Program*.

**Mit esptool:** \`esptool --chip esp32 --baud 115200 write-flash 0x0 queue-atom-matrix.bin\`

Danach den Queue wie in \`cue_sensor/README.md\` beschrieben über den Hotspot einrichten.
Andere Boards (XIAO, AtomS3, ESP32-DevKit): Quellcode mit PlatformIO selbst bauen.
MD
packe billard-queue billard-queue.zip "$SRC/cue_sensor" "$SRC/koe_messgeraet" "$FW" "$SRC/VERSION"

# Gesamt
shopt -s dotglob
packe kks_billiard billard-gesamt.zip "$SRC"/*
shopt -u dotglob

cp "$SRC/scripts/update.sh" "$OUT/update.sh"
echo "fertig: $OUT"
