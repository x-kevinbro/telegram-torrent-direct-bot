#!/usr/bin/env bash
#
# UpTunnel one-line installer
#
#   curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/install.sh | sudo bash
#
# Non-interactive example:
#   curl -fsSL .../install.sh | sudo bash -s -- --domain dl.example.com --email you@example.com --yes
#
set -euo pipefail

REPO_TARBALL="https://github.com/x-kevinbro/telegram-torrent-direct-bot/archive/refs/heads/main.tar.gz"

DOMAIN=""
EMAIL=""
SITE_KEY=""
SITE_NAME="UpTunnel"
MAX_GB="150"
EXPIRY_H="24"
DIR="/opt/uptunnel"
ASSUME_YES=0

log()  { printf '\033[1;36m[uptunnel]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[uptunnel]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[uptunnel] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------ args ------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email)  EMAIL="$2";  shift 2 ;;
    --key)    SITE_KEY="$2"; shift 2 ;;
    --name)   SITE_NAME="$2"; shift 2 ;;
    --max-gb) MAX_GB="$2"; shift 2 ;;
    --dir)    DIR="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    *) die "Unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Run as root:  curl ... | sudo bash"

# ------------------------------ prompts ------------------------------
ask() { # ask VAR "Prompt"
  local var="$1" prompt="$2" val=""
  [ "$ASSUME_YES" = "1" ] && return 0
  [ -r /dev/tty ] || return 0
  printf '%s: ' "$prompt" > /dev/tty
  read -r val < /dev/tty || true
  [ -n "$val" ] && printf -v "$var" '%s' "$val"
}

[ -z "$DOMAIN" ] && ask DOMAIN "Domain for HTTPS (leave empty to use plain HTTP with the server IP)"
if [ -n "$DOMAIN" ] && [ -z "$EMAIL" ]; then
  ask EMAIL "Email for SSL certificate expiry notices (optional)"
fi
if [ -z "$SITE_KEY" ]; then
  SITE_KEY="utn-$(head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 12)"
fi

log "Domain:   ${DOMAIN:-<none, HTTP mode>}"
log "Site key: $SITE_KEY"
log "Dir:      $DIR"

# ------------------------------ packages ------------------------------
log "Installing packages..."
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || true
  apt-get install -y curl tar gzip xz-utils ca-certificates || true
  command -v docker >/dev/null 2>&1 || apt-get install -y docker.io || true
  docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-v2 2>/dev/null || apt-get install -y docker-compose-plugin 2>/dev/null || true
  [ -n "$DOMAIN" ] && { command -v certbot >/dev/null 2>&1 || apt-get install -y certbot; }
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl tar gzip xz ca-certificates || true
  command -v docker >/dev/null 2>&1 || dnf install -y docker || true
  docker compose version >/dev/null 2>&1 || dnf install -y docker-compose-plugin 2>/dev/null || dnf install -y docker-compose 2>/dev/null || true
  [ -n "$DOMAIN" ] && { command -v certbot >/dev/null 2>&1 || dnf install -y certbot; }
else
  die "Unsupported distro (need apt-get or dnf)."
fi

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker from get.docker.com..."
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  warn "Compose plugin missing, retrying via get.docker.com..."
  curl -fsSL https://get.docker.com | sh || true
fi
docker compose version >/dev/null 2>&1 || die "docker compose is unavailable after install."
systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
docker info >/dev/null 2>&1 || die "Docker daemon is not running."

# ------------------------------ files ------------------------------
log "Downloading UpTunnel into $DIR..."
mkdir -p "$DIR"
curl -fsSL "$REPO_TARBALL" -o "$DIR/.pkg.tar.gz"
tar xzf "$DIR/.pkg.tar.gz" -C "$DIR" --strip-components=1
rm -f "$DIR/.pkg.tar.gz"

mkdir -p "$DIR/downloads" "$DIR/data" "$DIR/nginx" "$DIR/qbittorrent-config/qBittorrent"

# qBittorrent pre-config: accept notice + allow the app container to use the API
if [ ! -f "$DIR/qbittorrent-config/qBittorrent/qBittorrent.conf" ]; then
  cat > "$DIR/qbittorrent-config/qBittorrent/qBittorrent.conf" <<'EOF'
[LegalNotice]
Accepted=true

[Preferences]
WebUI\Address=*
WebUI\ServerDomains=*
WebUI\AuthSubnetWhitelistEnabled=true
WebUI\AuthSubnetWhitelist=172.16.0.0/12,127.0.0.1/32
Downloads\SavePath=/downloads/
Downloads\TempPath=/downloads/incomplete/

[BitTorrent]
Session\DefaultSavePath=/downloads/
Session\TempPath=/downloads/incomplete/
EOF
fi

# static ffmpeg (lightweight, no apt bloat) for MKV->MP4 browser playback
mkdir -p "$DIR/ffmpeg-bin"
if [ ! -x "$DIR/ffmpeg-bin/ffmpeg" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64)        FFURL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" ;;
    aarch64|arm64) FFURL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz" ;;
    *)             FFURL="" ;;
  esac
  if [ -n "$FFURL" ]; then
    log "Downloading static ffmpeg..."
    if curl -fsSL "$FFURL" -o "$DIR/.ffmpeg.tar.xz" && tar xJf "$DIR/.ffmpeg.tar.xz" -C "$DIR"; then
      cp "$DIR"/ffmpeg-*-static/ffmpeg "$DIR/ffmpeg-bin/ffmpeg"
      chmod +x "$DIR/ffmpeg-bin/ffmpeg"
    else
      warn "ffmpeg download failed — MKV-to-MP4 conversion will be unavailable"
    fi
    rm -rf "$DIR"/ffmpeg-*-static "$DIR/.ffmpeg.tar.xz"
  fi
fi

# timezone in compose
TZ_VAL="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
[ -z "$TZ_VAL" ] && TZ_VAL="UTC"
sed -i "s|TZ=Asia/Colombo|TZ=$TZ_VAL|" "$DIR/docker-compose.yml" 2>/dev/null || true

# ------------------------------ ssl + nginx ------------------------------
write_http_conf() {
  cat > "$DIR/nginx/default.conf" <<'EOF'
server {
    listen 80 default_server;
    server_name _;
    client_max_body_size 32m;

    location / {
        proxy_pass http://bot:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /_protected_downloads/ {
        internal;
        alias /downloads/;
    }
}
EOF
}

write_https_conf() {
  cat > "$DIR/nginx/default.conf" <<EOF
server {
    listen 80;
    server_name __DOMAIN__;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name __DOMAIN__;

    ssl_certificate /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/__DOMAIN__/privkey.pem;

    client_max_body_size 32m;

    location / {
        proxy_pass http://bot:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }

    location /_protected_downloads/ {
        internal;
        alias /downloads/;
    }
}
EOF
  sed -i "s/__DOMAIN__/$DOMAIN/g" "$DIR/nginx/default.conf"
}

USE_HTTPS=0
if [ -n "$DOMAIN" ]; then
  if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    log "Issuing SSL certificate for $DOMAIN (port 80 must be free and DNS must point here)..."
    EMAIL_ARGS=(--register-unsafely-without-email)
    [ -n "$EMAIL" ] && EMAIL_ARGS=(-m "$EMAIL")
    if certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos "${EMAIL_ARGS[@]}" --keep-until-expiring; then
      USE_HTTPS=1
    else
      warn "Certificate failed. Is $DOMAIN pointing at this server? Starting in HTTP mode for now."
      warn "Fix DNS, then re-run this installer with --domain $DOMAIN to enable HTTPS."
    fi
  else
    USE_HTTPS=1
  fi
fi

if [ "$USE_HTTPS" = "1" ]; then
  write_https_conf
  BASE="https://$DOMAIN"
  # auto-renew + reload nginx after renewal
  if [ -d /etc/cron.d ]; then
    cat > /etc/cron.d/uptunnel-renew <<EOF
0 3 * * * root certbot renew --quiet --deploy-hook "cd $DIR && docker compose restart nginx"
EOF
  fi
else
  write_http_conf
  PUBLIC_IP="$(curl -4 -fsSL --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  [ -z "$PUBLIC_IP" ] && PUBLIC_IP="127.0.0.1"
  BASE="http://$PUBLIC_IP"
fi

# ------------------------------ env ------------------------------
cat > "$DIR/.env" <<EOF
PUBLIC_BASE_URL=$BASE
SITE_NAME=$SITE_NAME
SITE_KEY=$SITE_KEY
QBIT_HOST=http://qbittorrent:8080
MAX_TORRENT_SIZE_GB=$MAX_GB
MAX_ACTIVE_DOWNLOADS=2
MIN_FREE_GB=2
LINK_EXPIRY_HOURS=$EXPIRY_H
DOWNLOAD_DIR=/downloads
DATABASE_PATH=/data/web.db
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
S3_ENDPOINT=
S3_REGION=auto
S3_BUCKET=
S3_ACCESS_KEY=
S3_SECRET_KEY=
S3_PUBLIC_URL=
S3_AUTO_OFFLOAD=0
EOF
chmod 600 "$DIR/.env"

# ------------------------------ start ------------------------------
log "Starting services (first build takes a few minutes)..."
cd "$DIR"
docker compose up -d --build

log "Waiting for the app to come up..."
ok=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 3 http://127.0.0.1/health >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
[ "$ok" = "1" ] || warn "Health check not answering yet — run: cd $DIR && docker compose logs -f bot"

echo
printf '\033[1;32m'
echo "============================================"
echo "  UpTunnel is installed"
echo "============================================"
printf '\033[0m'
echo "  URL:        $BASE"
echo "  Access key: $SITE_KEY"
echo "  Unlock:     $BASE/unlock?key=$SITE_KEY"
echo "  Downloads:  $BASE/jobs"
echo
echo "  Firewall: open ports 80, 443 and 6881 (tcp+udp)."
echo "  (On Oracle Cloud also open them in the VCN security list.)"
echo
echo "  Manage:  cd $DIR && docker compose ps"
echo "  Update:  curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/update.sh | sudo bash"
echo
echo "  Only download content you have the rights to access."
