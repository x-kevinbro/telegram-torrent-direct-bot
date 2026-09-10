# UpTunnel — Torrent to Direct Download

A self-hosted web app that turns magnet links and `.torrent` files into direct HTTPS download links, with in-browser streaming for media files.

> Only download content you have the legal right to access.

## Features

- Paste a magnet link or upload a `.torrent` file
- Metadata loads first — nothing downloads until you choose
- Select only the files you want (skipped files stay at priority 0)
- Live progress page with speed, ETA, seeds/peers
- In-browser player for video/audio files
- Direct download links per file, or download everything as a ZIP
- Automatic expiry and cleanup
- Optional access-key gate for private use

## Stack

- FastAPI + uvicorn web app
- qBittorrent (nox) as the torrent engine
- Nginx for HTTPS + efficient file serving (X-Accel-Redirect)
- SQLite for job tracking

## Deploy

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

## Environment

See `.env.example`. `SITE_KEY` protects the site with an access key when set.
