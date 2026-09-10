# UpTunnel — Torrent to Direct Download

A self-hosted web app that turns magnet links and `.torrent` files into direct HTTPS download links, with in-browser streaming for media files.

> Only download content you have the legal right to access.

## One-line install (any VPS)

Ubuntu/Debian or Fedora/Amazon Linux, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/install.sh | sudo bash
```

The installer asks for a domain (optional), installs Docker, downloads the app, gets a free SSL certificate, generates an access key, and starts everything.

Non-interactive install with flags:

```bash
curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/install.sh | sudo bash -s -- \
  --domain dl.example.com \
  --email you@example.com \
  --yes
```

| Flag | Meaning | Default |
|---|---|---|
| `--domain` | Domain for HTTPS. Empty = HTTP with server IP | *(prompted)* |
| `--email` | Email for certificate expiry notices | *(prompted)* |
| `--key` | Site access key | random |
| `--name` | Site name shown in the UI | `UpTunnel` |
| `--max-gb` | Max torrent size in GB | `150` |
| `--dir` | Install directory | `/opt/uptunnel` |
| `--yes` | Never prompt, use defaults/flags | off |

After install, open the printed URL and unlock with the printed access key.

Open these ports in your firewall/security list: `80`, `443`, `6881` (tcp+udp).

## Update an existing install

```bash
curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/update.sh | sudo bash
```

Keeps your `.env`, downloads, database and qBittorrent config.

## Features

- Paste a magnet link or upload a `.torrent` file
- Metadata loads first — nothing downloads until you choose
- Select only the files you want (skipped files stay at priority 0)
- Live progress page with speed, ETA, seeds/peers
- In-browser player for video/audio files
- Direct download links per file, or download everything as a ZIP
- Share links work in download managers (token-authenticated)
- Automatic expiry and cleanup
- Access-key gate keeps the site private

## Stack

- FastAPI + uvicorn web app
- qBittorrent (nox) as the torrent engine
- Nginx for HTTPS + efficient file serving (X-Accel-Redirect)
- SQLite for job tracking

## Manual deploy

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

## Environment

See `.env.example`. `SITE_KEY` protects the site with an access key when set.
