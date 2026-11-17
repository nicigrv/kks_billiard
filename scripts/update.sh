#!/usr/bin/env bash
# Billard-System aus den GitHub-Releases installieren oder aktualisieren.
#
#   Raspberry Pi:  curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- rasp
#   Server (VPS):  curl -fsSL https://github.com/nicigrv/kks_billiard/releases/latest/download/update.sh | bash -s -- server
#   feste Version: ... | bash -s -- rasp v0.1.2
#
# Ordner: Pi ~/billard, Server ~/billard-training (anders: BILLARD_DIR=/pfad davor setzen).
# Bleibt erhalten: data/, .env und die Einstellungen ganz oben in docker-compose.yml (x-...).
set -euo pipefail

REPO="nicigrv/kks_billiard"
ZIEL="${1:-}"
VERSION="${2:-latest}"

case "$ZIEL" in
  rasp)   ASSET="billard-rasp.zip";   TOP="billard";          DIR="${BILLARD_DIR:-$HOME/billard}" ;;
  server) ASSET="billard-server.zip"; TOP="billard-training"; DIR="${BILLARD_DIR:-$HOME/billard-training}" ;;
  *) echo "Aufruf: update.sh rasp|server [version, z. B. v0.1.2]"; exit 1 ;;
esac

if [ "$VERSION" = "latest" ]; then
  URL="https://github.com/$REPO/releases/latest/download/$ASSET"
else
  case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
  URL="https://github.com/$REPO/releases/download/$VERSION/$ASSET"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -n "${BILLARD_ZIP:-}" ]; then        # lokale Datei statt Download (zum Testen)
  cp "$BILLARD_ZIP" "$TMP/paket.zip"
else
  echo "==> Lade $URL"
  curl -fL --progress-bar -o "$TMP/paket.zip" "$URL" || { echo "Download fehlgeschlagen (gibt es die Version?)"; exit 1; }
fi

echo "==> Entpacke"
if command -v unzip >/dev/null; then
  unzip -q "$TMP/paket.zip" -d "$TMP/neu"
elif command -v python3 >/dev/null; then
  python3 -m zipfile -e "$TMP/paket.zip" "$TMP/neu"
else
  sudo apt-get update -qq && sudo apt-get install -y -qq unzip
  unzip -q "$TMP/paket.zip" -d "$TMP/neu"
fi
NEU="$TMP/neu/$TOP"
[ -d "$NEU" ] || { echo "Paket hat nicht den erwarteten Inhalt"; exit 1; }

ALT_V="$(cat "$DIR/VERSION" 2>/dev/null || echo "keine")"
NEU_V="$(cat "$NEU/VERSION")"
echo "==> $ZIEL: $ALT_V -> $NEU_V  (Ordner $DIR)"
mkdir -p "$DIR"

# Einstellungen ganz oben in docker-compose.yml (x-web-port, x-admin-pin, ...) übernehmen
if [ -f "$DIR/docker-compose.yml" ]; then
  cp "$DIR/docker-compose.yml" "$DIR/docker-compose.yml.alt"
  if ! cmp -s "$DIR/docker-compose.yml" "$NEU/docker-compose.yml"; then
    while IFS= read -r zeile; do
      key="${zeile%%:*}"
      if grep -q "^$key:" "$NEU/docker-compose.yml"; then
        awk -v k="$key:" -v z="$zeile" 'index($0, k) == 1 { print z; next } { print }' \
          "$NEU/docker-compose.yml" > "$TMP/compose" && cat "$TMP/compose" > "$NEU/docker-compose.yml"
        echo "    behalten: $zeile"
      fi
    done < <(grep -E '^x-[A-Za-z0-9_-]+:' "$DIR/docker-compose.yml" || true)
    echo "    Alte docker-compose.yml liegt als docker-compose.yml.alt daneben (andere eigene Änderungen dort nachsehen)."
  fi
fi

# Programmdateien ersetzen (data/ und .env bleiben unberührt)
if [ "$ZIEL" = "rasp" ]; then
  rm -rf "$DIR/app"
fi
cp -a "$NEU/." "$DIR/"

cd "$DIR"
if [ "$ZIEL" = "server" ] && [ ! -f .env ]; then
  KEY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "UPLOAD_KEY=$KEY" > .env
  chmod 600 .env
  echo "==> Neue .env mit UPLOAD_KEY angelegt. Diesen Schlüssel auf dem Pi als TRAINING_UPLOAD_KEY eintragen:"
  echo "    $KEY"
fi

# Docker vorhanden?
if ! command -v docker >/dev/null; then
  echo "==> Docker fehlt, wird installiert (dauert ein paar Minuten)"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

echo "==> Baue und starte die Container (beim ersten Mal 15-30 Minuten)"
$DOCKER compose up -d --build --remove-orphans
$DOCKER image prune -f >/dev/null 2>&1 || true

echo "==> Fertig: $ZIEL läuft mit Version $NEU_V"
