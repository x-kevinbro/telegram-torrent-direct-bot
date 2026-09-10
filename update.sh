#!/usr/bin/env bash
#
# UpTunnel updater — pulls the latest code and rebuilds, keeping your
# .env, downloads, database and qBittorrent config untouched.
#
#   curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/update.sh | sudo bash
#
set -euo pipefail

DIR="${1:-/opt/uptunnel}"
REPO_TARBALL="https://github.com/x-kevinbro/telegram-torrent-direct-bot/archive/refs/heads/main.tar.gz"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
[ -f "$DIR/.env" ] || { echo "No .env found in $DIR — is UpTunnel installed there?" >&2; exit 1; }

cd "$DIR"
rm -rf .update-tmp
mkdir -p .update-tmp
curl -fsSL "$REPO_TARBALL" -o .update-tmp/pkg.tar.gz
tar xzf .update-tmp/pkg.tar.gz -C .update-tmp --strip-components=1

# Update application code only; keep .env, nginx/default.conf, downloads/, data/, qbittorrent-config/
cp -a .update-tmp/app/. app/
cp -a .update-tmp/README.md . 2>/dev/null || true
rm -rf .update-tmp

docker compose up -d --build bot
sleep 5
if curl -fsS --max-time 5 http://127.0.0.1/health >/dev/null 2>&1; then
  echo "UpTunnel updated and healthy."
else
  echo "Updated, but health check did not answer yet — check: cd $DIR && docker compose logs -f bot"
fi
