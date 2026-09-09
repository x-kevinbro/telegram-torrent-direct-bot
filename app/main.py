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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost").rstrip("/")
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
QBIT_HOST = os.environ.get("QBIT_HOST", "http://qbittorrent:8080")
QBIT_USERNAME = os.environ.get("QBIT_USERNAME", "admin")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "adminadmin")
MAX_TORRENT_SIZE_GB = float(os.environ.get("MAX_TORRENT_SIZE_GB", "150"))
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get("MAX_ACTIVE_JOBS_PER_USER", "1"))
LINK_EXPIRY_HOURS = int(os.environ.get("LINK_EXPIRY_HOURS", "24"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/bot.db")

app = FastAPI()

def db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
        expires_at INTEGER NOT NULL,
        chat_id INTEGER,
        progress_message_id INTEGER,
        last_progress_text TEXT
    )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for column, ddl in {
        "chat_id": "ALTER TABLE jobs ADD COLUMN chat_id INTEGER",
        "progress_message_id": "ALTER TABLE jobs ADD COLUMN progress_message_id INTEGER",
        "last_progress_text": "ALTER TABLE jobs ADD COLUMN last_progress_text TEXT",
    }.items():
        if column not in existing:
            conn.execute(ddl)
    conn.commit()
    return conn

class QbitClient:
    def __init__(self):
        self.base = QBIT_HOST.rstrip("/")
    def _post(self, path: str, data=None, files=None):
        response = requests.post(f"{self.base}{path}", data=data or {}, files=files, timeout=30)
        if response.status_code != 404:
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
    def torrents_start(self, torrent_hashes="all"):
        response = self._post("/api/v2/torrents/start", data={"hashes": torrent_hashes})
        if response.status_code == 404:
            response = self._post("/api/v2/torrents/resume", data={"hashes": torrent_hashes})
        return response
    def torrents_pause(self, torrent_hashes="all"):
        response = self._post("/api/v2/torrents/stop", data={"hashes": torrent_hashes})
        if response.status_code == 404:
            response = self._post("/api/v2/torrents/pause", data={"hashes": torrent_hashes})
        return response
    def torrents_recheck(self, torrent_hashes="all"):
        return self._post("/api/v2/torrents/recheck", data={"hashes": torrent_hashes})

def qbit():
    return QbitClient()

def allowed(update: Update) -> bool:
    return bool(update.effective_user and (not ADMIN_IDS or update.effective_user.id in ADMIN_IDS))

def active_count(user_id: int) -> int:
    conn = db()
    return conn.execute("SELECT COUNT(*) c FROM jobs WHERE user_id=? AND status IN ('queued','downloading','stalledDL','paused')", (user_id,)).fetchone()["c"]

def make_job(user_id: int, chat_id: int) -> tuple[int, str, Path]:
    token = secrets.token_urlsafe(18)
    now = int(time.time())
    save_path = DOWNLOAD_DIR / f"user_{user_id}" / token
    save_path.mkdir(parents=True, exist_ok=True)
    os.chmod(save_path, 0o775)
    try:
        os.chown(save_path, 1000, 1000)
        os.chown(save_path.parent, 1000, 1000)
    except PermissionError:
        pass
    conn = db()
    cur = conn.execute("INSERT INTO jobs(user_id, token, status, save_path, created_at, expires_at, chat_id) VALUES(?,?,?,?,?,?,?)", (user_id, token, "queued", str(save_path), now, now + LINK_EXPIRY_HOURS * 3600, chat_id))
    conn.commit()
    return cur.lastrowid, token, save_path

def progress_bar(progress: float, width: int = 18) -> str:
    progress = max(0, min(1, progress or 0))
    filled = round(progress * width)
    return "█" * filled + "░" * (width - filled)

def human_size(num: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024

def human_eta(seconds) -> str:
    if seconds is None or seconds < 0 or seconds >= 8640000:
        return "unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m" if h else (f"{m}m {sec}s" if m else f"{sec}s")

def job_keyboard(job_id: int, status: str = "downloading") -> InlineKeyboardMarkup | None:
    normalized = (status or "").lower()
    if normalized in {"complete", "cancelled", "expired", "failed"} or "error" in normalized:
        return None
    if "pause" in normalized or "stop" in normalized or normalized in {"pauseddl", "pausedup", "stopped"}:
        return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Resume", callback_data=f"resume:{job_id}")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏸ Pause", callback_data=f"pause:{job_id}")]])

def progress_text(job_id: int, status: str, progress: float, name=None, speed=0, seeds=0, peers=0, downloaded=0, total=0, eta=None) -> str:
    pct = int(max(0, min(1, progress or 0)) * 100)
    title = f"\n{name}" if name else ""
    details = f"\nSpeed: {human_size(speed)}/s | Seeds: {seeds} | Peers: {peers}"
    if total:
        details += f"\nDone: {human_size(downloaded)} / {human_size(total)}"
    details += f"\nETA: {human_eta(eta)}"
    icon = "⏸" if status == "paused" else "⏳"
    return f"{icon} Job #{job_id} — {status}{title}\n[{progress_bar(progress)}] {pct}%{details}"

async def send_progress_message(update: Update, job_id: int) -> int:
    text = progress_text(job_id, "queued", 0)
    msg = await update.message.reply_text(text, reply_markup=job_keyboard(job_id, "queued"))
    conn = db()
    conn.execute("UPDATE jobs SET progress_message_id=?, last_progress_text=? WHERE id=?", (msg.message_id, text, job_id))
    conn.commit()
    return msg.message_id

async def update_progress_message(bot_app: Application, row, text: str, status: str = "downloading"):
    if not row["chat_id"] or not row["progress_message_id"]:
        return
    try:
        await bot_app.bot.edit_message_text(chat_id=row["chat_id"], message_id=row["progress_message_id"], text=text, reply_markup=job_keyboard(row["id"], status))
        conn = db()
        conn.execute("UPDATE jobs SET last_progress_text=? WHERE id=?", (text, row["id"]))
        conn.commit()
    except Exception as e:
        if "Message is not modified" not in str(e):
            print(f"progress edit error: {e}", flush=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("This bot is private.")
        return
    await update.message.reply_text("Send me a legal magnet link or .torrent file. I will show a live progress bar and a single Pause/Resume button.")

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
    row = conn.execute("SELECT * FROM jobs WHERE user_id=? AND status IN ('queued','downloading','stalledDL','paused') ORDER BY id DESC LIMIT 1", (update.effective_user.id,)).fetchone()
    if not row:
        await update.message.reply_text("No active job to cancel.")
        return
    try:
        if row["torrent_hash"]:
            qbit().torrents_delete(row["torrent_hash"], delete_files=True)
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
        await update.message.reply_text("You already have an active job. Use /cancel.")
        return
    job_id, token, save_path = make_job(update.effective_user.id, update.effective_chat.id)
    await send_progress_message(update, job_id)
    try:
        qbit().torrents_add(urls=text, save_path=str(save_path))
    except Exception as e:
        conn = db(); conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,)); conn.commit()
        await update.message.reply_text(f"Failed to add torrent: {e}")

async def handle_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".torrent"):
        await update.message.reply_text("Please send a .torrent file.")
        return
    if active_count(update.effective_user.id) >= MAX_ACTIVE_JOBS_PER_USER:
        await update.message.reply_text("You already have an active job. Use /cancel.")
        return
    job_id, token, save_path = make_job(update.effective_user.id, update.effective_chat.id)
    await send_progress_message(update, job_id)
    torrent_path = save_path / "input.torrent"
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(torrent_path))
    try:
        qbit().torrents_add(torrent_files=str(torrent_path), save_path=str(save_path))
    except Exception as e:
        conn = db(); conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job_id,)); conn.commit()
        await update.message.reply_text(f"Failed to add torrent: {e}")

async def button_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.data or ":" not in query.data:
        await query.answer(); return
    action, job_id_text = query.data.split(":", 1)
    job_id = int(job_id_text)
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or (ADMIN_IDS and query.from_user.id not in ADMIN_IDS) or (not ADMIN_IDS and row["user_id"] != query.from_user.id):
        await query.answer("Not allowed", show_alert=True); return
    try:
        client = qbit()
        if action == "pause":
            if row["torrent_hash"]:
                client.torrents_pause(row["torrent_hash"])
            conn.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,)); conn.commit()
            text = (row["last_progress_text"] or progress_text(job_id, "paused", row["progress"] or 0, row["name"])).replace("— downloading", "— paused").replace("— stalledDL", "— paused").replace("⏳", "⏸", 1)
            await query.edit_message_text(text=text, reply_markup=job_keyboard(job_id, "paused"))
            await query.answer("Paused")
        elif action == "resume":
            if row["torrent_hash"]:
                client.torrents_start(row["torrent_hash"])
            conn.execute("UPDATE jobs SET status='downloading' WHERE id=?", (job_id,)); conn.commit()
            text = (row["last_progress_text"] or progress_text(job_id, "downloading", row["progress"] or 0, row["name"])).replace("— paused", "— downloading").replace("⏸", "⏳", 1)
            await query.edit_message_text(text=text, reply_markup=job_keyboard(job_id, "downloading"))
            await query.answer("Resumed")
    except Exception as e:
        await query.answer(f"Action failed: {e}", show_alert=True)

async def monitor_loop(bot_app: Application):
    while True:
        try:
            conn = db()
            rows = conn.execute("SELECT * FROM jobs WHERE status IN ('queued','downloading','stalledDL','paused')").fetchall()
            torrents = {str(t.save_path): t for t in qbit().torrents_info()}
            for r in rows:
                t = torrents.get(r["save_path"])
                if not t:
                    continue
                progress = float(t.progress or 0)
                qstate = str(getattr(t, "state", "unknown"))
                if progress >= 1:
                    status_value = "complete"
                elif "pause" in qstate.lower() or "stop" in qstate.lower():
                    status_value = "paused"
                elif "error" in qstate.lower():
                    status_value = "error"
                else:
                    status_value = qstate
                conn.execute("UPDATE jobs SET status=?, progress=?, torrent_hash=?, name=? WHERE id=?", (status_value, progress, t.hash, t.name, r["id"]))
                if status_value == "complete":
                    link = f"{PUBLIC_BASE_URL}/d/{r['token']}"
                    text = f"✅ Download ready:\n{link}\n\n[{progress_bar(1)}] 100%\nThis link expires in {LINK_EXPIRY_HOURS} hours."
                    await update_progress_message(bot_app, r, text, status_value)
                elif status_value == "error":
                    await update_progress_message(bot_app, r, progress_text(r["id"], "error", progress, t.name, getattr(t,"dlspeed",0) or 0, getattr(t,"num_seeds",0) or 0, getattr(t,"connections_count",0) or 0, getattr(t,"downloaded",0) or 0, getattr(t,"total_size",0) or 0, getattr(t,"eta",None)), status_value)
                else:
                    await update_progress_message(bot_app, r, progress_text(r["id"], status_value, progress, t.name, getattr(t,"dlspeed",0) or 0, getattr(t,"num_seeds",0) or 0, getattr(t,"connections_count",0) or 0, getattr(t,"downloaded",0) or 0, getattr(t,"total_size",0) or 0, getattr(t,"eta",None)), status_value)
            conn.commit()
        except Exception as e:
            print(f"monitor error: {e}", flush=True)
        await asyncio.sleep(10)

async def cleanup_loop():
    while True:
        try:
            now = int(time.time())
            conn = db()
            rows = conn.execute("SELECT * FROM jobs WHERE expires_at < ? AND status != 'expired'", (now,)).fetchall()
            for r in rows:
                try:
                    if r["torrent_hash"]:
                        qbit().torrents_delete(r["torrent_hash"], delete_files=True)
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
    headers = {"X-Accel-Redirect": "/_protected_downloads/" + quote(str(rel)), "Content-Disposition": f"attachment; filename*=UTF-8''{quote(target.name)}"}
    return Response(status_code=200, headers=headers)

async def main():
    db()
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("myfiles", myfiles))
    bot_app.add_handler(CommandHandler("cancel", cancel))
    bot_app.add_handler(CallbackQueryHandler(button_action))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, handle_torrent_file))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_magnet))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info"))
    await bot_app.initialize(); await bot_app.start(); await bot_app.updater.start_polling()
    try:
        await asyncio.gather(server.serve(), monitor_loop(bot_app), cleanup_loop())
    finally:
        await bot_app.updater.stop(); await bot_app.stop(); await bot_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
