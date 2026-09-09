import asyncio
import html
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost").rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
QBIT_HOST = os.environ.get("QBIT_HOST", "http://qbittorrent:8080")
QBIT_USERNAME = os.environ.get("QBIT_USERNAME", "admin")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "adminadmin")
MAX_TORRENT_SIZE_GB = float(os.environ.get("MAX_TORRENT_SIZE_GB", "5"))
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get("MAX_ACTIVE_JOBS_PER_USER", "1"))
LINK_EXPIRY_HOURS = int(os.environ.get("LINK_EXPIRY_HOURS", "24"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/bot.db")

app = FastAPI()


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        torrent_hash TEXT,
        name TEXT,
        token TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL,
        progress REAL DEFAULT 0,
        save_path TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )
    """)
    conn.commit()
    return conn


class QbitClient:
    def __init__(self):
        self.base = QBIT_HOST.rstrip("/")

    def _post(self, path: str, data=None, files=None):
        response = requests.post(f"{self.base}{path}", data=data or {}, files=files, timeout=30)
        response.raise_for_status()
        return response

    def _get(self, path: str):
        response = requests.get(f"{self.base}{path}", timeout=30)
        response.raise_for_status()
        return response

    def torrents_add(self, urls=None, torrent_files=None, save_path=None):
        data = {"savepath": save_path}
        files = None
        if urls:
            data["urls"] = urls
        if torrent_files:
            files = {"torrents": open(torrent_files, "rb")}
        try:
            return self._post("/api/v2/torrents/add", data=data, files=files)
        finally:
            if files:
                files["torrents"].close()

    def torrents_info(self):
        return [type("Torrent", (), item) for item in self._get("/api/v2/torrents/info").json()]

    def torrents_delete(self, torrent_hashes, delete_files=True):
        return self._post("/api/v2/torrents/delete", data={"hashes": torrent_hashes, "deleteFiles": str(delete_files).lower()})


def qbit():
    return QbitClient()


def allowed(update: Update) -> bool:
    return bool(update.effective_user and (not ADMIN_IDS or update.effective_user.id in ADMIN_IDS))


def active_count(user_id: int) -> int:
    conn = db()
    return conn.execute("SELECT COUNT(*) c FROM jobs WHERE user_id=? AND status IN ('queued','downloading')", (user_id,)).fetchone()["c"]


def make_job(user_id: int) -> tuple[int, str, Path]:
    token = secrets.token_urlsafe(18)
    now = int(time.time())
    save_path = DOWNLOAD_DIR / f"user_{user_id}" / token
    save_path.mkdir(parents=True, exist_ok=True)
    conn = db()
    cur = conn.execute(
        "INSERT INTO jobs(user_id, token, status, save_path, created_at, expires_at) VALUES(?,?,?,?,?,?)",
        (user_id, token, "queued", str(save_path), now, now + LINK_EXPIRY_HOURS * 3600),
    )
    conn.commit()
    return cur.lastrowid, token, save_path


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("This bot is private.")
        return
    await update.message.reply_text(
        "Send me a legal magnet link or .torrent file. I will download it and return only a temporary direct download link."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 5", (update.effective_user.id,)).fetchall()
    if not rows:
        await update.message.reply_text("No jobs yet.")
        return
    lines = []
    for r in rows:
        pct = int((r["progress"] or 0) * 100)
        lines.append(f"#{r['id']} {r['status']} {pct}% {r['name'] or ''}".strip())
    await update.message.reply_text("\n".join(lines))


async def myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM jobs WHERE user_id=? AND status='complete' AND expires_at>? ORDER BY id DESC LIMIT 10", (update.effective_user.id, int(time.time()))).fetchall()
    if not rows:
        await update.message.reply_text("No active download links.")
        return
    await update.message.reply_text("\n".join(f"{r['name'] or 'Download'}: {PUBLIC_BASE_URL}/d/{r['token']}" for r in rows))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE user_id=? AND status IN ('queued','downloading') ORDER BY id DESC LIMIT 1", (update.effective_user.id,)).fetchone()
    if not row:
        await update.message.reply_text("No active job to cancel.")
        return
    try:
        if row["torrent_hash"]:
            qbit().torrents_delete(torrent_hashes=row["torrent_hash"], delete_files=True)
    except Exception:
        pass
    conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (row["id"],))
    conn.commit()
    shutil.rmtree(row["save_path"], ignore_errors=True)
    await update.message.reply_text("Cancelled latest active job.")


async def handle_magnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("This bot is private.")
        return
    text = (update.message.text or "").strip()
    if not text.startswith("magnet:?"):
        await update.message.reply_text("Please send a magnet link or .torrent file.")
        return
    if active_count(update.effective_user.id) >= MAX_ACTIVE_JOBS_PER_USER:
        await update.message.reply_text("You already have an active job. Use /status or /cancel.")
        return
    job_id, token, save_path = make_job(update.effective_user.id)
    try:
        qbit().torrents_add(urls=text, save_path=str(save_path))
        await update.message.reply_text(f"Added job #{job_id}. Use /status to check progress.")
    except Exception as e:
        conn = db()
        conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,))
        conn.commit()
        await update.message.reply_text(f"Failed to add torrent: {e}")


async def handle_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".torrent"):
        await update.message.reply_text("Please send a .torrent file.")
        return
    if active_count(update.effective_user.id) >= MAX_ACTIVE_JOBS_PER_USER:
        await update.message.reply_text("You already have an active job. Use /status or /cancel.")
        return
    job_id, token, save_path = make_job(update.effective_user.id)
    torrent_path = save_path / "input.torrent"
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(torrent_path))
    try:
        qbit().torrents_add(torrent_files=str(torrent_path), save_path=str(save_path))
        await update.message.reply_text(f"Added job #{job_id}. Use /status to check progress.")
    except Exception as e:
        conn = db()
        conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,))
        conn.commit()
        await update.message.reply_text(f"Failed to add torrent: {e}")


async def monitor_loop(bot_app: Application):
    while True:
        try:
            conn = db()
            rows = conn.execute("SELECT * FROM jobs WHERE status IN ('queued','downloading')").fetchall()
            torrents = {str(t.save_path): t for t in qbit().torrents_info()}
            for r in rows:
                t = torrents.get(r["save_path"])
                if not t:
                    continue
                size_gb = (getattr(t, "total_size", 0) or 0) / (1024 ** 3)
                if size_gb > MAX_TORRENT_SIZE_GB:
                    qbit().torrents_delete(torrent_hashes=t.hash, delete_files=True)
                    conn.execute("UPDATE jobs SET status='failed', torrent_hash=?, name=? WHERE id=?", (t.hash, t.name, r["id"]))
                    await bot_app.bot.send_message(r["user_id"], f"Job #{r['id']} failed: torrent is over {MAX_TORRENT_SIZE_GB:g} GB.")
                    continue
                progress = float(t.progress or 0)
                status_value = "complete" if progress >= 1 else "downloading"
                conn.execute("UPDATE jobs SET status=?, progress=?, torrent_hash=?, name=? WHERE id=?", (status_value, progress, t.hash, t.name, r["id"]))
                if status_value == "complete" and r["status"] != "complete":
                    link = f"{PUBLIC_BASE_URL}/d/{r['token']}"
                    await bot_app.bot.send_message(r["user_id"], f"✅ Download ready:\n{link}\n\nThis link expires in {LINK_EXPIRY_HOURS} hours.")
            conn.commit()
        except Exception as e:
            print(f"monitor error: {e}", flush=True)
        await asyncio.sleep(20)


async def cleanup_loop():
    while True:
        try:
            now = int(time.time())
            conn = db()
            rows = conn.execute("SELECT * FROM jobs WHERE expires_at < ? AND status != 'expired'", (now,)).fetchall()
            client = None
            for r in rows:
                try:
                    client = client or qbit()
                    if r["torrent_hash"]:
                        client.torrents_delete(torrent_hashes=r["torrent_hash"], delete_files=True)
                except Exception:
                    pass
                shutil.rmtree(r["save_path"], ignore_errors=True)
                conn.execute("UPDATE jobs SET status='expired' WHERE id=?", (r["id"],))
            conn.commit()
        except Exception as e:
            print(f"cleanup error: {e}", flush=True)
        await asyncio.sleep(3600)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/d/{token}")
def download(token: str):
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE token=?", (token,)).fetchone()
    if not row or row["status"] != "complete" or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    base = Path(row["save_path"])
    files = [p for p in base.rglob("*") if p.is_file() and p.name != "input.torrent"]
    if not files:
        raise HTTPException(status_code=404, detail="No completed file found")
    target = max(files, key=lambda p: p.stat().st_size)
    rel = target.relative_to(DOWNLOAD_DIR)
    headers = {
        "X-Accel-Redirect": "/_protected_downloads/" + quote(str(rel)),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(target.name)}",
    }
    return Response(status_code=200, headers=headers)


async def main():
    db()
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("status", status))
    bot_app.add_handler(CommandHandler("myfiles", myfiles))
    bot_app.add_handler(CommandHandler("cancel", cancel))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_torrent_file))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet))

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    tasks = [asyncio.create_task(server.serve()), asyncio.create_task(monitor_loop(bot_app)), asyncio.create_task(cleanup_loop())]
    try:
        await asyncio.gather(*tasks)
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
