# UpTunnel — Torrent to Direct Download

A self-hosted web app that turns magnet links and `.torrent` files into direct HTTPS download links, with in-browser streaming for media files.

> Only download content you have the legal right to access.

## One-line install (any VPS)

Ubuntu/Debian or Fedora/Amazon Linux, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/x-kevinbro/telegram-torrent-direct-bot/main/install.sh | sudo bash
```

The installer asks for a domain (optional), installs Docker, downloads the app, gets a free SSL certificate, downloads a static FFmpeg, generates an access key, and starts everything.

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

Keeps your `.env`, downloads, database and qBittorrent config. App code is mounted as a volume, so updates just need a container restart.

## Features

- Paste a magnet link or upload a `.torrent` file
- Metadata loads first — nothing downloads until you choose
- Select only the files you want (skipped files stay at priority 0)
- Live progress page with speed, ETA, seeds/peers and per-file progress
- **Stream-on-the-fly**: watch video/audio while it is still downloading (sequential download + first/last piece priority + HTTP range streaming)
- **MKV/AVI → MP4 one-click conversion** for browser playback (static FFmpeg, video copied — no re-encode)
- **Subtitle auto-load**: a `.srt`/`.vtt` next to a video shows up in the player (SRT converted to WebVTT)
- **Download queue**: `MAX_ACTIVE_DOWNLOADS` slots; extra downloads wait in queue with a position and auto-start
- **Disk guard**: rejects torrents that cannot fit — at metadata time and at start time — keeping a `MIN_FREE_GB` safety buffer
- In-browser player, direct per-file links, and download-all-as-ZIP
- Share links work in download managers (token-authenticated, resumable/range support)
- Automatic expiry and cleanup
- Access-key gate keeps the site private

## Stack

- FastAPI + uvicorn web app
- qBittorrent (nox) as the torrent engine
- Nginx for HTTPS + efficient file serving (X-Accel-Redirect)
- SQLite for job tracking
- Static FFmpeg binary for MKV→MP4 remuxing

## Manual deploy

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

## Environment

See `.env.example`. `SITE_KEY` protects the site with an access key when set. `MAX_ACTIVE_DOWNLOADS` and `MIN_FREE_GB` control the queue and disk guard.
