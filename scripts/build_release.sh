#!/usr/bin/env bash
# Baut die Release-Pakete (läuft in GitHub Actions, geht aber auch lokal).
#   scripts/build_release.sh v0.1.2 dist
# Ergebnis in dist/:
#   billard-rasp.zip     Raspberry Pi (app/ + docker-compose.yml)
#   billard-server.zip   Trainingsserver für den VPS (training_server/)
#   billard-queue.zip    Queue-Firmware (Quellcode, .bin für USB und für das Update über WLAN)
#   queue-ota-<board>.bin  Firmware-Update über WLAN (einzeln, lädt das Admin-Panel des Pi)
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

# Queue: Firmware für alle Boards bauen. Die Release-Version steckt in der Firmware (RELEASE_VERSION),
# so erkennt das Admin-Panel, ob der Queue aktuell ist.
#   queue-ota-<board>.bin   nur das Programm: Online-Update über WLAN (Admin-Panel, http://<queue>/update)
#   queue-atom-matrix.bin   alles zusammen ab Adresse 0x0: erstes Aufspielen per USB
FW="$BAU/firmware"
mkdir -p "$FW"
QB="$BAU/build-cue_sensor"
cp -a "$SRC/cue_sensor" "$QB"
export PLATFORMIO_BUILD_FLAGS="-DRELEASE_VERSION='\"$VERSION\"'"
pio run -d "$QB" >/dev/null                          # alle Boards (default_envs)
unset PLATFORMIO_BUILD_FLAGS
for env in esp32dev xiao_esp32c3 m5stack-atom m5stack-atoms3; do
  cp "$QB/.pio/build/$env/firmware.bin" "$FW/queue-ota-$env.bin"
  cp "$FW/queue-ota-$env.bin" "$OUT/"               # einzeln im Release: das Admin-Panel lädt sie direkt
done
o="$QB/.pio/build/m5stack-atom"
app0="$(find "$HOME/.platformio/packages/framework-arduinoespressif32" -name boot_app0.bin | head -1)"
esptool --chip esp32 merge-bin -o "$FW/queue-atom-matrix.bin" \
  0x1000 "$o/bootloader.bin" 0x8000 "$o/partitions.bin" 0xe000 "$app0" 0x10000 "$o/firmware.bin" >/dev/null
cat > "$FW/FLASHEN.md" <<MD
# Firmware aufspielen ($VERSION)

| Datei | Wofür |
|---|---|
| \`queue-atom-matrix.bin\` | **erstes Aufspielen per USB-Kabel** (M5Stack Atom Matrix / Atom Lite), ab Adresse 0x0 |
| \`queue-ota-<board>.bin\` | **Update über WLAN**, ohne Kabel (nur das Programm) |

## Update über WLAN (Queue zusammengebaut)
- **Admin-Panel des Pi** → Reiter *Queue* → *Firmware des Queues* → "Neueste Version installieren".
  Der Pi holt die passende Datei selbst von GitHub.
- **Browser:** \`http://<IP des Queues>/update\` öffnen, Castcode eingeben, \`queue-ota-<board>.bin\` wählen.
  Board und IP stehen auf der Statusseite des Queues (\`http://<IP des Queues>/\`).

Die LED blinkt blau, danach startet der Queue neu. Bricht etwas ab, läuft die alte Firmware weiter.

## Erstes Aufspielen per USB
**Im Browser (Chrome/Edge):** https://espressif.github.io/esptool-js/ öffnen, Atom per USB-C anstecken,
*Connect* (Baudrate 115200), Adresse \`0x0\`, \`queue-atom-matrix.bin\` wählen, *Program*.

**Mit esptool:** \`esptool --chip esp32 --baud 115200 write-flash 0x0 queue-atom-matrix.bin\`

Danach den Queue wie in \`cue_sensor/README.md\` beschrieben über den Hotspot einrichten.
Andere Boards per USB: mit PlatformIO bauen und hochladen.
MD
packe billard-queue billard-queue.zip "$SRC/cue_sensor" "$FW" "$SRC/VERSION"

# Gesamt
shopt -s dotglob
packe kks_billiard billard-gesamt.zip "$SRC"/*
shopt -u dotglob

cp "$SRC/scripts/update.sh" "$OUT/update.sh"
echo "fertig: $OUT"
