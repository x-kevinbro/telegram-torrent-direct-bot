# Telegram Torrent Direct Bot

A private Telegram bot that accepts permitted magnet links or `.torrent` files, downloads them through qBittorrent, and returns temporary direct download links. It does **not** upload completed files to Telegram.

> Only use this with torrents you have the legal right to download or distribute.

## Features

- Magnet link support
- `.torrent` file support
- qBittorrent Web API integration
- Direct links via an internal FastAPI app
- Nginx `X-Accel-Redirect` for efficient file serving
- Admin allowlist
- Link expiry
- Automatic cleanup
- `/status`, `/cancel`, `/myfiles`

## Deployment

1. Copy `.env.example` to `.env` and fill values.
2. Point your domain to the server.
3. Run:

```bash
docker compose up -d --build
```

## Environment

See `.env.example`.
