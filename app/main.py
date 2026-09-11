import html
import json
import os
import re
import secrets
import subprocess
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import requests
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SITE_NAME = os.environ.get("SITE_NAME", "UpTunnel")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost").rstrip("/")
SITE_KEY = os.environ.get("SITE_KEY", "").strip()
QBIT_HOST = os.environ.get("QBIT_HOST", "http://qbittorrent:8080").rstrip("/")
MAX_TORRENT_SIZE_GB = float(os.environ.get("MAX_TORRENT_SIZE_GB", "150"))
MAX_ACTIVE_DOWNLOADS = int(os.environ.get("MAX_ACTIVE_DOWNLOADS", "2"))
MIN_FREE_BYTES = int(float(os.environ.get("MIN_FREE_GB", "2")) * (1024 ** 3))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "").strip()
S3_REGION = os.environ.get("S3_REGION", "auto").strip()
S3_BUCKET = os.environ.get("S3_BUCKET", "").strip()
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "").strip()
S3_PUBLIC_URL = os.environ.get("S3_PUBLIC_URL", "").rstrip("/")
S3_AUTO_OFFLOAD = os.environ.get("S3_AUTO_OFFLOAD", "0") == "1"
S3B_ENDPOINT = os.environ.get("S3B_ENDPOINT", "").strip()
S3B_REGION = os.environ.get("S3B_REGION", "auto").strip()
S3B_BUCKET = os.environ.get("S3B_BUCKET", "").strip()
S3B_ACCESS_KEY = os.environ.get("S3B_ACCESS_KEY", "").strip()
S3B_SECRET_KEY = os.environ.get("S3B_SECRET_KEY", "").strip()
S3B_PUBLIC_URL = os.environ.get("S3B_PUBLIC_URL", "").rstrip("/")
S3_SPLIT_BYTES = int(float(os.environ.get("S3_SPLIT_GB", "20")) * (1024 ** 3))
OCI_HOOK_SECRET = os.environ.get("OCI_HOOK_SECRET", "").strip()
OFFLOAD_JOBS = {}
LINK_EXPIRY_HOURS = int(os.environ.get("LINK_EXPIRY_HOURS", "24"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/web.db")

app = FastAPI(title=SITE_NAME)

PLAYABLE = {".mp4", ".webm", ".mov", ".m4v", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac"}
REMUXABLE = {".mkv", ".avi"}
SUBTITLES = {".srt", ".vtt"}
REMUX_JOBS = {}
MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac",
}

# ----------------------------- database -----------------------------

def db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        torrent_hash TEXT,
        name TEXT,
        status TEXT NOT NULL,
        progress REAL DEFAULT 0,
        total_size INTEGER DEFAULT 0,
        save_path TEXT NOT NULL,
        error TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        selected_ids TEXT,
        link_password TEXT,
        files_json TEXT,
        offloaded INTEGER DEFAULT 0
    )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, ddl in {
        "selected_ids": "ALTER TABLE jobs ADD COLUMN selected_ids TEXT",
        "link_password": "ALTER TABLE jobs ADD COLUMN link_password TEXT",
        "files_json": "ALTER TABLE jobs ADD COLUMN files_json TEXT",
        "offloaded": "ALTER TABLE jobs ADD COLUMN offloaded INTEGER DEFAULT 0",
    }.items():
        if col not in existing:
            conn.execute(ddl)
    conn.commit()
    return conn

def get_job(token):
    conn = db()
    row = conn.execute("SELECT * FROM jobs WHERE token=?", (token,)).fetchone()
    conn.close()
    return row

def set_job(token, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = db()
    conn.execute(f"UPDATE jobs SET {cols} WHERE token=?", (*fields.values(), token))
    conn.commit()
    conn.close()

def new_job():
    token = secrets.token_urlsafe(18)
    now = int(time.time())
    save_path = DOWNLOAD_DIR / token
    save_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(save_path, 0o775)
        os.chown(save_path, 1000, 1000)
    except PermissionError:
        pass
    conn = db()
    conn.execute(
        "INSERT INTO jobs(token, status, save_path, created_at, expires_at) VALUES(?,?,?,?,?)",
        (token, "metadata", str(save_path), now, now + LINK_EXPIRY_HOURS * 3600),
    )
    conn.commit()
    conn.close()
    return token, save_path

# ----------------------------- qBittorrent -----------------------------

class QbitClient:
    def __init__(self):
        self.base = QBIT_HOST

    def _post(self, path, data=None, files=None):
        r = requests.post(f"{self.base}{path}", data=data or {}, files=files, timeout=60)
        if r.status_code >= 400 and r.status_code not in (404, 409):
            r.raise_for_status()
        return r

    def _get(self, path, params=None):
        r = requests.get(f"{self.base}{path}", params=params or {}, timeout=30)
        r.raise_for_status()
        return r

    def add(self, urls=None, torrent_file=None, save_path=None):
        data = {"savepath": save_path}
        files = None
        if urls:
            data["urls"] = urls
        if torrent_file:
            files = {"torrents": open(torrent_file, "rb")}
        try:
            r = self._post("/api/v2/torrents/add", data=data, files=files)
            if r.status_code == 409:
                return "duplicate"
            r.raise_for_status()
            return "added"
        finally:
            if files:
                files["torrents"].close()

    def info(self):
        return self._get("/api/v2/torrents/info").json()

    def files(self, torrent_hash):
        return self._get("/api/v2/torrents/files", params={"hash": torrent_hash}).json()

    def set_prio(self, torrent_hash, file_id, priority):
        return self._post("/api/v2/torrents/filePrio", data={"hash": torrent_hash, "id": str(file_id), "priority": str(priority)})

    def start(self, torrent_hash):
        r = self._post("/api/v2/torrents/start", data={"hashes": torrent_hash})
        if r.status_code == 404:
            r = self._post("/api/v2/torrents/resume", data={"hashes": torrent_hash})
        return r

    def stop(self, torrent_hash):
        r = self._post("/api/v2/torrents/stop", data={"hashes": torrent_hash})
        if r.status_code == 404:
            r = self._post("/api/v2/torrents/pause", data={"hashes": torrent_hash})
        return r

    def delete(self, torrent_hash, delete_files=True):
        return self._post("/api/v2/torrents/delete", data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()})

    def enable_streaming(self, t):
        # Sequential piece order + first/last piece priority lets playback start
        # long before the download finishes.
        if not t.get("seq_dl"):
            self._post("/api/v2/torrents/toggleSequentialDownload", data={"hashes": t["hash"]})
        if not t.get("f_l_piece_prio"):
            self._post("/api/v2/torrents/toggleFirstLastPiecePrio", data={"hash": t["hash"]})

def qbit():
    return QbitClient()

def find_torrent(client, row):
    try:
        torrents = client.info()
    except Exception:
        return None
    h = (row["torrent_hash"] or "").lower()
    if h:
        for t in torrents:
            if str(t.get("hash", "")).lower() == h:
                return t
    for t in torrents:
        if str(t.get("save_path", "")) == row["save_path"]:
            return t
    return None

# ----------------------------- helpers -----------------------------

def human_size(num):
    num = float(num or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024

def human_eta(seconds):
    if seconds is None or seconds < 0 or seconds >= 8640000:
        return "unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def bar(fraction):
    pct = int(max(0, min(1, fraction or 0)) * 100)
    return f'<div class="bar"><i style="width:{pct}%"></i></div>'

def safe_rel_path(base: Path, rel_path: str) -> Path:
    rel_path = unquote(rel_path).lstrip("/")
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    return target

def find_subtitle(base: Path, video: Path):
    folder = video.parent
    stem = video.stem
    subs = sorted([p for p in folder.glob("*") if p.suffix.lower() in SUBTITLES], key=lambda x: x.name.lower())
    for sub in subs:
        if sub.stem == stem:
            return sub
    return subs[0] if subs else None

def srt_to_vtt(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text.strip() + "\n"

def do_remux(key, src: Path, out: Path):
    tmp = out.with_name(out.stem + ".remuxing.mp4")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(tmp)],
            capture_output=True, timeout=3600,
        )
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.rename(out)
            REMUX_JOBS[key] = "done"
        else:
            err = proc.stderr.decode(errors="replace")[-400:] if proc.stderr else "conversion failed"
            REMUX_JOBS[key] = "error: " + err
            tmp.unlink(missing_ok=True)
    except Exception as e:
        REMUX_JOBS[key] = f"error: {e}"
        tmp.unlink(missing_ok=True)

def notify(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=15)
        print(f"notify sent: {r.status_code}", flush=True)
    except Exception as e:
        print(f"notify error: {e}", flush=True)

def s3_target_a():
    return {"endpoint": S3_ENDPOINT, "region": S3_REGION, "bucket": S3_BUCKET,
            "access": S3_ACCESS_KEY, "secret": S3_SECRET_KEY, "public": S3_PUBLIC_URL}

def s3_target_b():
    return {"endpoint": S3B_ENDPOINT, "region": S3B_REGION, "bucket": S3B_BUCKET,
            "access": S3B_ACCESS_KEY, "secret": S3B_SECRET_KEY, "public": S3B_PUBLIC_URL}

def s3_target_ok(t):
    return bool(t["endpoint"] and t["bucket"] and t["access"] and t["secret"] and t["public"])

def s3_enabled():
    return s3_target_ok(s3_target_a())

def s3_client(t=None):
    import boto3
    from botocore.config import Config
    t = t or s3_target_a()
    cfg = Config(s3={"addressing_style": "path"},
                 request_checksum_calculation="when_required",
                 response_checksum_validation="when_required")
    return boto3.client("s3", endpoint_url=t["endpoint"], region_name=t["region"],
                        aws_access_key_id=t["access"], aws_secret_access_key=t["secret"], config=cfg)

def s3_bucket_used_bytes(t):
    try:
        c = s3_client(t)
        total = 0
        kwargs = {}
        while True:
            resp = c.list_objects_v2(Bucket=t["bucket"], **kwargs)
            for obj in resp.get("Contents", []):
                total += obj.get("Size", 0)
            if not resp.get("IsTruncated"):
                break
            kwargs["ContinuationToken"] = resp.get("NextContinuationToken")
        return total
    except Exception as e:
        print(f"bucket usage check error: {e}", flush=True)
        return 0

def s3_target_for_job(total_size):
    # Route by TOTAL download size. Everything goes to the primary (free-tier)
    # target unless the total exceeds S3_SPLIT_BYTES, or the primary bucket
    # would overflow its free-tier capacity — then fall back to the secondary.
    if not s3_target_ok(s3_target_b()):
        return "a", s3_target_a()
    if total_size > S3_SPLIT_BYTES:
        return "b", s3_target_b()
    a = s3_target_a()
    used = s3_bucket_used_bytes(a)
    if used + total_size > S3_SPLIT_BYTES:
        print(f"primary bucket nearly full ({used} used), falling back to secondary", flush=True)
        return "b", s3_target_b()
    return "a", a

def job_local_files(base: Path):
    return sorted(
        [x for x in base.rglob("*") if x.is_file() and x.name != "input.torrent" and not x.name.endswith(".zip")
         and not x.name.endswith(".remuxing.mp4") and "_zip_extract" not in x.parts],
        key=lambda x: str(x).lower(),
    )

def job_manifest(row):
    if row["offloaded"]:
        try:
            return json.loads(row["files_json"] or "[]")
        except Exception:
            return []
    base = Path(row["save_path"])
    return [{"rel": str(f.relative_to(base)), "size": f.stat().st_size} for f in job_local_files(base)]

def manifest_entry(row, rel_path):
    rel_path = unquote(rel_path).lstrip("/")
    try:
        manifest = json.loads(row["files_json"] or "[]")
    except Exception:
        return None
    for m in manifest:
        if m["rel"] == rel_path:
            return m
    return None

def manifest_public_url(m):
    t = s3_target_b() if m.get("store") == "b" else s3_target_a()
    return f"{t['public']}/{quote(m['key'])}"

def do_offload(token):
    try:
        row = get_job(token)
        if not row or row["offloaded"]:
            OFFLOAD_JOBS[token] = "done"
            return
        base = Path(row["save_path"])
        files = job_local_files(base)
        total = sum(f.stat().st_size for f in files)
        store, t = s3_target_for_job(total)
        client = s3_client(t)
        manifest = []
        for f in files:
            rel = str(f.relative_to(base))
            key = f"{token}/{rel}"
            client.upload_file(str(f), t["bucket"], key)
            manifest.append({"rel": rel, "size": f.stat().st_size, "key": key, "store": store})
        set_job(token, files_json=json.dumps(manifest), offloaded=1)
        try:
            if row["torrent_hash"]:
                qbit().delete(row["torrent_hash"], False)
        except Exception:
            pass
        shutil.rmtree(base, ignore_errors=True)
        OFFLOAD_JOBS[token] = "done"
        print(f"offload done: {token} ({len(manifest)} files, {total} bytes -> {store})", flush=True)
    except Exception as e:
        OFFLOAD_JOBS[token] = f"error: {e}"
        print(f"offload error: {e}", flush=True)

def delete_s3_files(row):
    if not row["offloaded"]:
        return
    try:
        clients = {}
        buckets = {}
        for m in json.loads(row["files_json"] or "[]"):
            store = m.get("store", "a")
            if store not in clients:
                t = s3_target_b() if store == "b" else s3_target_a()
                clients[store] = s3_client(t)
                buckets[store] = t["bucket"]
            clients[store].delete_object(Bucket=buckets[store], Key=m["key"])
    except Exception as e:
        print(f"s3 cleanup error: {e}", flush=True)

def link_denied(row, request: Request):
    pw = row["link_password"]
    if not pw:
        return None
    if request.query_params.get("key") == pw:
        return None
    if request.cookies.get(f"link_{row['token']}") == pw:
        return None
    return RedirectResponse(f"/gate/{row['token']}?next={quote(request.url.path)}", status_code=303)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__REFRESH__
<style>
:root{--bg:#0a0f1e;--card:#111a2ecc;--line:#22314f;--txt:#e6edf7;--mut:#8ea0bf;--acc:#22d3ee;--acc2:#34d399}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:radial-gradient(1200px 600px at 80% -10%,#12324a55,transparent),radial-gradient(900px 500px at -10% 10%,#1a2a5255,transparent),var(--bg);color:var(--txt);min-height:100vh}
a{color:var(--acc);text-decoration:none}
.wrap{max-width:860px;margin:0 auto;padding:28px 18px 60px}
.nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
.brand{font-weight:800;font-size:22px;letter-spacing:.5px;color:var(--txt)}
.brand span{color:var(--acc)}
.nav a{color:var(--mut);margin-left:16px;font-size:14px}
.nav a:hover{color:var(--txt)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin:14px 0;backdrop-filter:blur(6px)}
h1{margin:0 0 6px;font-size:26px;overflow-wrap:anywhere}
h2{margin:0 0 10px;font-size:18px}
p{color:var(--mut);line-height:1.55}
textarea,input{width:100%;background:#0a1322;border:1px solid var(--line);border-radius:12px;color:var(--txt);padding:14px;font-size:15px}
textarea:focus,input:focus{outline:none;border-color:var(--acc)}
.btn,button.btn{display:inline-block;background:linear-gradient(135deg,var(--acc),#0ea5b7);color:#04202a;font-weight:700;border:0;border-radius:12px;padding:12px 20px;font-size:15px;cursor:pointer;margin:6px 6px 6px 0}
.btn:hover{filter:brightness(1.12)}
.btn.ghost{background:#1b2942;color:var(--txt);border:1px solid var(--line)}
.btn.danger{background:#ef4444;color:#fff}
.row{display:flex;justify-content:space-between;gap:14px;align-items:center;padding:12px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.row:first-child{border-top:0}
.name{overflow-wrap:anywhere;font-size:14px;flex:1;min-width:200px}
.size{color:var(--mut);white-space:nowrap;font-size:13px}
.bar{height:12px;background:#0a1322;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:12px 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));border-radius:999px;transition:width .4s}
.chip{display:inline-block;background:#132742;border:1px solid var(--line);color:var(--acc);border-radius:999px;padding:3px 12px;font-size:12px;font-weight:600}
.mut{color:var(--mut);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:14px 0}
.stat{background:#0a1322;border:1px solid var(--line);border-radius:12px;padding:12px;text-align:center}
.stat b{display:block;font-size:16px;margin-bottom:2px}
.stat span{font-size:12px}
label.row{cursor:pointer}
label.row input{margin-right:10px;transform:scale(1.25)}
video{width:100%;border-radius:12px;background:#000;margin-top:10px}
.center{text-align:center}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:8px}
.step{background:#0a1322;border:1px solid var(--line);border-radius:12px;padding:14px}
.step b{color:var(--acc);font-size:18px}
</style>
</head>
<body>
<div class="wrap">
<div class="nav"><div class="brand">__BRAND__</div><div><a href="/">Home</a><a href="/jobs">Downloads</a></div></div>
__BODY__
<p class="mut center" style="margin-top:34px">Only download content you have the rights to access. Links expire automatically.</p>
</div>
</body>
</html>"""

def html_page(title, body, refresh=None):
    tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        PAGE.replace("__TITLE__", html.escape(title))
        .replace("__REFRESH__", tag)
        .replace("__BRAND__", html.escape(SITE_NAME))
        .replace("__BODY__", body)
    )

# ----------------------------- access gate -----------------------------

def lock_page():
    body = """
    <div class="card" style="max-width:460px;margin:60px auto">
      <h1>Private access</h1>
      <p>This service is private. Enter the access key to continue.</p>
      <form method="get" action="/unlock">
        <input type="password" name="key" placeholder="Access key" autocomplete="off">
        <p><button class="btn" type="submit">Unlock</button></p>
      </form>
    </div>
    """
    return html_page("Locked", body)

@app.middleware("http")
async def access_gate(request: Request, call_next):
    if not SITE_KEY:
        return await call_next(request)
    path = request.url.path
    # Share links authenticate via their unguessable token, so download managers
    # (no cookies) can fetch them. Everything else needs the site key cookie.
    token_paths = ("/file/", "/stream/", "/zip/", "/files/", "/watch/", "/remux/", "/subtitle/", "/links/", "/gate/", "/oci-events/")
    if path in ("/health", "/unlock") or path.startswith(token_paths):
        return await call_next(request)
    if request.cookies.get("site_key") == SITE_KEY:
        return await call_next(request)
    if path == "/" and request.method == "GET":
        return HTMLResponse(lock_page())
    return RedirectResponse("/", status_code=303)

@app.get("/unlock")
def unlock(key: str = ""):
    if not SITE_KEY or key == SITE_KEY:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("site_key", key, max_age=30 * 24 * 3600, httponly=True, secure=True, samesite="lax")
        return resp
    return HTMLResponse(lock_page(), status_code=403)

# ----------------------------- pages -----------------------------

@app.get("/", response_class=HTMLResponse)
def home():
    usage = shutil.disk_usage(DOWNLOAD_DIR)
    used_frac = (usage.used / usage.total) if usage.total else 0
    body = f"""
    <div class="card center" style="padding:34px">
      <h1 style="font-size:32px">Torrent to direct download</h1>
      <p>Paste a magnet link or upload a .torrent file. We fetch it on the server, you pick the files, then stream or download them over HTTPS.</p>
      <form method="post" action="/add-magnet">
        <textarea name="magnet" rows="4" placeholder="magnet:?xt=urn:btih:..." required></textarea>
        <p><button class="btn" type="submit">Fetch torrent</button></p>
      </form>
      <form method="post" action="/add-torrent" enctype="multipart/form-data">
        <input type="file" name="torrent" accept=".torrent" required>
        <p><button class="btn ghost" type="submit">Upload .torrent</button></p>
      </form>
    </div>
    <div class="card">
      <h2>Server storage</h2>
      {bar(used_frac)}
      <p class="mut">Free {human_size(usage.free)} of {human_size(usage.total)} &middot; links expire after {LINK_EXPIRY_HOURS}h</p>
    </div>
    <div class="card">
      <h2>How it works</h2>
      <div class="steps">
        <div class="step"><b>1</b><p>Add a magnet link or .torrent file.</p></div>
        <div class="step"><b>2</b><p>We load the torrent and list its files.</p></div>
        <div class="step"><b>3</b><p>Choose only the files you need.</p></div>
        <div class="step"><b>4</b><p>Stream in your browser or download directly.</p></div>
      </div>
    </div>
    """
    return HTMLResponse(html_page(SITE_NAME, body))

@app.post("/add-magnet")
def add_magnet(magnet: str = Form(...)):
    magnet = magnet.strip().replace("\\:", ":")
    if not magnet.startswith("magnet:?"):
        body = '<div class="card"><h1>Invalid magnet link</h1><p>Please paste a valid magnet URI starting with magnet:?xt=</p><p><a class="btn ghost" href="/">Back</a></p></div>'
        return HTMLResponse(html_page("Invalid link", body), status_code=400)
    token, save_path = new_job()
    try:
        result = qbit().add(urls=magnet, save_path=str(save_path))
    except Exception as e:
        set_job(token, status="error", error=f"Failed to add torrent: {e}")
        return RedirectResponse(f"/job/{token}", status_code=303)
    if result == "duplicate":
        shutil.rmtree(save_path, ignore_errors=True)
        set_job(token, status="error", error="This torrent is already on the server. Delete the existing download first.")
        return RedirectResponse(f"/job/{token}", status_code=303)
    return RedirectResponse(f"/job/{token}", status_code=303)

@app.post("/add-torrent")
async def add_torrent(torrent: UploadFile = File(...)):
    if not torrent.filename or not torrent.filename.lower().endswith(".torrent"):
        body = '<div class="card"><h1>Invalid file</h1><p>Please upload a .torrent file.</p><p><a class="btn ghost" href="/">Back</a></p></div>'
        return HTMLResponse(html_page("Invalid file", body), status_code=400)
    token, save_path = new_job()
    tpath = save_path / "input.torrent"
    with open(tpath, "wb") as out:
        shutil.copyfileobj(torrent.file, out)
    client = qbit()
    try:
        result = client.add(torrent_file=str(tpath), save_path=str(save_path))
    except Exception as e:
        set_job(token, status="error", error=f"Failed to add torrent: {e}")
        return RedirectResponse(f"/job/{token}", status_code=303)
    if result == "duplicate":
        shutil.rmtree(save_path, ignore_errors=True)
        set_job(token, status="error", error="This torrent is already on the server. Delete the existing download first.")
        return RedirectResponse(f"/job/{token}", status_code=303)
    # .torrent files have metadata immediately: set all priorities to 0 now
    try:
        t = find_torrent(client, {"torrent_hash": None, "save_path": str(save_path)})
        if t:
            files = client.files(t["hash"])
            for f in files:
                client.set_prio(t["hash"], f["index"], 0)
            if len(files) == 1:
                client.set_prio(t["hash"], files[0]["index"], 1)
                client.start(t["hash"])
                set_job(token, status="downloading", torrent_hash=t["hash"], name=t.get("name"), total_size=t.get("total_size") or 0)
            else:
                set_job(token, status="waiting_selection", torrent_hash=t["hash"], name=t.get("name"), total_size=t.get("total_size") or 0)
    except Exception as e:
        print(f"post-add error: {e}", flush=True)
    return RedirectResponse(f"/job/{token}", status_code=303)

@app.get("/job/{token}", response_class=HTMLResponse)
def job_page(token: str):
    row = get_job(token)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    status = row["status"]
    name = html.escape(row["name"] or "Loading…")

    if status == "metadata":
        body = f"""
        <div class="card center">
          <h1>Fetching torrent metadata</h1>
          <p>Contacting peers and reading the file list. This page refreshes automatically.</p>
          {bar(0.04)}
        </div>"""
        return HTMLResponse(html_page("Loading metadata", body, refresh=3))

    if status == "waiting_selection":
        try:
            files = qbit().files(row["torrent_hash"])
        except Exception:
            files = []
        if not files:
            body = f'<div class="card center"><h1>Loading file list…</h1><p>This page refreshes automatically.</p>{bar(0.1)}</div>'
            return HTMLResponse(html_page("Loading files", body, refresh=3))
        rows_html = []
        for f in files:
            idx = f.get("index")
            rows_html.append(
                f'<label class="row"><span class="name"><input type="checkbox" name="ids" value="{idx}" checked> {html.escape(f.get("name", "file"))}</span><span class="size">{human_size(f.get("size", 0))}</span></label>'
            )
        body = f"""
        <div class="card">
          <h1>{name}</h1>
          <span class="chip">Ready &middot; {len(files)} file(s) &middot; {human_size(row['total_size'])}</span>
          <p>Nothing is downloading yet. Select the files you want, then start.</p>
          <form method="post" action="/start/{token}">
            <p><button class="btn" type="submit">Download selected</button> <a class="btn danger" href="/action/{token}/delete">Cancel &amp; delete</a></p>
            <div class="card" style="background:#0a1322">{''.join(rows_html)}</div>
            <p><button class="btn" type="submit">Download selected</button></p>
          </form>
        </div>"""
        return HTMLResponse(html_page("Choose files", body))

    if status in ("downloading", "paused"):
        client = qbit()
        t = find_torrent(client, row)
        pct = int((row["progress"] or 0) * 100)
        speed, downloaded, total, eta, seeds, peers = "—", "—", human_size(row["total_size"]), "—", "—", "—"
        if t:
            pct = int(float(t.get("progress") or 0) * 100)
            speed = human_size(t.get("dlspeed", 0)) + "/s"
            downloaded = human_size(t.get("downloaded", 0))
            total = human_size(t.get("total_size", 0))
            eta = human_eta(t.get("eta"))
            seeds = t.get("num_seeds", 0)
            peers = t.get("num_leechs", 0)
        files_section = ""
        if row["torrent_hash"]:
            try:
                tfiles = client.files(row["torrent_hash"])
            except Exception:
                tfiles = []
            rows2 = []
            for f in tfiles:
                if int(f.get("priority", 0) or 0) == 0:
                    continue
                fname = f.get("name", "file")
                fprog = float(f.get("progress") or 0)
                fpct = int(fprog * 100)
                fsize = human_size(f.get("size", 0))
                qname = quote(fname)
                action = ""
                ext = Path(fname).suffix.lower()
                if ext in PLAYABLE:
                    if (f.get("size", 0) * fprog) > 8 * 1024 * 1024 or fprog > 0.01:
                        action = f'<a class="btn" href="/watch/{token}/{qname}">Play</a>'
                    else:
                        action = '<span class="mut">Preparing stream…</span>'
                elif ext in REMUXABLE:
                    action = '<span class="mut">Playable after download</span>'
                rows2.append(f'<div class="row"><span class="name">{html.escape(fname)}</span><span class="size">{fsize} &middot; {fpct}%</span><span>{action}</span></div>{bar(fprog)}')
            if rows2:
                files_section = '<div class="card"><h2>Files</h2>' + ''.join(rows2) + '<p class="mut">Playable files can be watched while they download.</p></div>'
        label = "Paused" if status == "paused" else "Downloading"
        toggle = f'<a class="btn ghost" href="/action/{token}/resume">Resume</a>' if status == "paused" else f'<a class="btn ghost" href="/action/{token}/pause">Pause</a>'
        body = f"""
        <div class="card">
          <h1>{name}</h1>
          <span class="chip">{label} &middot; {pct}%</span>
          {bar(pct / 100)}
          <div class="grid">
            <div class="stat"><b>{speed}</b><span class="mut">Speed</span></div>
            <div class="stat"><b>{downloaded}</b><span class="mut">Downloaded</span></div>
            <div class="stat"><b>{total}</b><span class="mut">Total</span></div>
            <div class="stat"><b>{eta}</b><span class="mut">ETA</span></div>
            <div class="stat"><b>{seeds} / {peers}</b><span class="mut">Seeds / Peers</span></div>
          </div>
          <p>{toggle} <a class="btn danger" href="/action/{token}/delete">Delete</a></p>
          <p class="mut">This page refreshes automatically every 5 seconds.</p>
        </div>
        {files_section}"""
        return HTMLResponse(html_page(label, body, refresh=5))

    if status == "queued":
        conn = db()
        ahead = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='queued' AND id<?", (row["id"],)).fetchone()["c"]
        conn.close()
        body = f"""
        <div class="card center">
          <h1>{name}</h1>
          <span class="chip">In queue &middot; position {ahead + 1}</span>
          <p>This download will start automatically when a slot is free. This page refreshes automatically.</p>
          {bar(0.02)}
          <p><a class="btn danger" href="/action/{token}/delete">Remove from queue</a></p>
        </div>"""
        return HTMLResponse(html_page("In queue", body, refresh=5))

    if status == "complete":
        return HTMLResponse(html_page(row["name"] or "Download", completed_body(row)))

    if status == "error":
        body = f"""
        <div class="card">
          <h1>Something went wrong</h1>
          <p>{html.escape(row["error"] or "Unknown error")}</p>
          <p><a class="btn ghost" href="/">Home</a> <a class="btn danger" href="/action/{token}/delete">Delete</a></p>
        </div>"""
        return HTMLResponse(html_page("Error", body))

    body = '<div class="card"><h1>Expired</h1><p>This download expired or was deleted.</p><p><a class="btn ghost" href="/">Home</a></p></div>'
    return HTMLResponse(html_page("Expired", body))

@app.post("/start/{token}")
def start_selected(token: str, ids: list[int] = Form(None)):
    row = get_job(token)
    if not row or row["status"] not in ("waiting_selection", "paused"):
        return RedirectResponse(f"/job/{token}", status_code=303)
    client = qbit()
    files = client.files(row["torrent_hash"])
    if not files:
        return RedirectResponse(f"/job/{token}", status_code=303)
    if not ids:
        body = f'<div class="card"><h1>No files selected</h1><p>Select at least one file to download.</p><p><a class="btn ghost" href="/job/{token}">Back</a></p></div>'
        return HTMLResponse(html_page("No files selected", body), status_code=400)
    idset = {int(i) for i in ids}
    selected_size = sum(f.get("size", 0) for f in files if f.get("index") in idset)
    free = shutil.disk_usage(DOWNLOAD_DIR).free
    if selected_size > free - MIN_FREE_BYTES:
        body = f'<div class="card"><h1>Not enough storage</h1><p>Selected files need {human_size(selected_size)} but only {human_size(free)} is free. Delete old downloads first.</p><p><a class="btn ghost" href="/jobs">Manage downloads</a></p></div>'
        return HTMLResponse(html_page("Not enough storage", body), status_code=400)
    set_job(token, selected_ids=",".join(str(i) for i in sorted(idset)))
    conn = db()
    active = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='downloading'").fetchone()["c"]
    conn.close()
    if active >= MAX_ACTIVE_DOWNLOADS:
        set_job(token, status="queued")
        return RedirectResponse(f"/job/{token}", status_code=303)
    for f in files:
        client.set_prio(row["torrent_hash"], f["index"], 0)
    for i in idset:
        client.set_prio(row["torrent_hash"], i, 1)
    client.start(row["torrent_hash"])
    set_job(token, status="downloading")
    return RedirectResponse(f"/job/{token}", status_code=303)

@app.get("/action/{token}/{action}")
def job_action(token: str, action: str):
    row = get_job(token)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    client = qbit()
    if action == "pause" and row["torrent_hash"]:
        client.stop(row["torrent_hash"])
        set_job(token, status="paused")
        return RedirectResponse(f"/job/{token}", status_code=303)
    if action == "resume" and row["torrent_hash"]:
        client.start(row["torrent_hash"])
        set_job(token, status="downloading")
        return RedirectResponse(f"/job/{token}", status_code=303)
    if action == "delete":
        try:
            if row["torrent_hash"]:
                client.delete(row["torrent_hash"], True)
        except Exception:
            pass
        delete_s3_files(row)
        shutil.rmtree(row["save_path"], ignore_errors=True)
        set_job(token, status="deleted")
        return RedirectResponse("/jobs", status_code=303)
    raise HTTPException(status_code=404, detail="Unknown action")

@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
    conn = db()
    rows = conn.execute("SELECT * FROM jobs WHERE status NOT IN ('deleted','expired') ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    items = []
    for r in rows:
        pct = int((r["progress"] or 0) * 100)
        items.append(f"""
        <div class="card">
          <div class="row"><span class="name"><b>#{r['id']}</b> {html.escape(r['name'] or 'Loading…')}</span>
          <span class="chip">{html.escape(r['status'])} &middot; {pct}%</span></div>
          {bar(r["progress"] or 0)}
          <p class="mut">{human_size(r['total_size'])}</p>
          <p><a class="btn ghost" href="/job/{r['token']}">Open</a> <a class="btn danger" href="/action/{r['token']}/delete">Delete</a></p>
        </div>""")
    usage = shutil.disk_usage(DOWNLOAD_DIR)
    body = f"""
    <div class="card"><h1>Downloads</h1><p class="mut">Free {human_size(usage.free)} of {human_size(usage.total)}</p></div>
    {''.join(items) if items else '<div class="card"><p>No downloads yet.</p></div>'}
    """
    return HTMLResponse(html_page("Downloads", body))

# ----------------------------- completed files -----------------------------

def completed_body(row):
    token = row["token"]
    offloaded = bool(row["offloaded"])
    manifest = job_manifest(row)
    if not manifest:
        return '<div class="card"><h1>No files found</h1><p>The download folder is empty.</p><p><a class="btn ghost" href="/">Home</a></p></div>'
    items = []
    for m in manifest:
        rel = m["rel"]
        qrel = quote(rel)
        play = ""
        ext = Path(rel).suffix.lower()
        if ext in PLAYABLE:
            play = f'<a class="btn" href="/watch/{token}/{qrel}">Play</a>'
        elif ext in REMUXABLE and not offloaded:
            play = f'<a class="btn" href="/remux/{token}/{qrel}">Play</a>'
        items.append(
            f'<div class="row"><span class="name">{html.escape(rel)}</span><span class="size">{human_size(m["size"])}</span><span>{play} <a class="btn ghost" href="/file/{token}/{qrel}">Download</a></span></div>'
        )
    title = html.escape(row["name"] or "Download")
    player = ""
    if len(manifest) == 1 and Path(manifest[0]["rel"]).suffix.lower() in PLAYABLE:
        qrel = quote(manifest[0]["rel"])
        player = f'<div class="card"><video controls preload="metadata" src="/stream/{token}/{qrel}"></video></div>'
    key_q = f"?key={quote(row['link_password'])}" if row["link_password"] else ""
    all_links = "\n".join(f"{PUBLIC_BASE_URL}/file/{token}/{quote(m['rel'])}{key_q}" for m in manifest)
    exp_hours_left = max(0, (row["expires_at"] - int(time.time())) // 3600)
    cloud = '<span class="chip">&#9729; Stored in cloud storage</span>' if offloaded else ""
    zip_btn = "" if offloaded else f'<a class="btn" href="/zip/{token}">Download all as ZIP</a>'
    offload_note = ""
    if s3_enabled() and not offloaded:
        if OFFLOAD_JOBS.get(token) == "running":
            offload_note = '<p class="mut">&#9729; Uploading to cloud storage… the page will keep working when it finishes.</p>'
        else:
            offload_note = f'<form method="post" action="/offload/{token}"><button class="btn ghost" type="submit">&#9729; Offload to cloud &amp; free local space</button></form>'
    pw_state = "Password protected" if row["link_password"] else "No password"
    return f"""
    <div class="card">
      <h1>{title}</h1>
      <span class="chip">Complete &middot; expires in ~{exp_hours_left}h</span> {cloud}
      <p>{zip_btn} <a class="btn danger" href="/action/{token}/delete">Delete files</a></p>
      {offload_note}
    </div>
    {player}
    <div class="card">{''.join(items)}</div>
    <div class="card">
      <h2>Batch download</h2>
      <p>Copy all links and paste them into IDM / aria2:</p>
      <textarea readonly rows="6" onclick="this.select()">{html.escape(all_links)}</textarea>
      <p><a class="btn ghost" href="/links/{token}">Download links.txt</a></p>
    </div>
    <div class="card">
      <h2>Share controls</h2>
      <p class="mut">{pw_state} &middot; links expire in ~{exp_hours_left}h</p>
      <form method="post" action="/link-settings/{token}">
        <p>Password for links (empty = no password):</p>
        <input type="password" name="password" placeholder="optional password" autocomplete="off">
        <p>Expire after (hours from now):</p>
        <input type="number" name="expiry_hours" min="1" max="8760" placeholder="24">
        <p><button class="btn" type="submit">Save</button></p>
      </form>
    </div>"""

@app.get("/files/{token}", response_class=HTMLResponse)
def browse_files(token: str, request: Request):
    row = get_job(token)
    if not row or row["status"] != "complete" or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    return HTMLResponse(html_page(row["name"] or "Files", completed_body(row)))

@app.get("/file/{token}/{rel_path:path}")
def download_file(token: str, rel_path: str, request: Request):
    row = get_job(token)
    if not row or row["status"] != "complete" or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    if row["offloaded"]:
        m = manifest_entry(row, rel_path)
        if not m:
            raise HTTPException(status_code=404, detail="File not found")
        return RedirectResponse(manifest_public_url(m), status_code=302)
    target = safe_rel_path(Path(row["save_path"]), rel_path)
    if not target.exists() or not target.is_file() or target.name == "input.torrent":
        raise HTTPException(status_code=404, detail="File not found")
    rel = target.relative_to(DOWNLOAD_DIR)
    headers = {
        "X-Accel-Redirect": "/_protected_downloads/" + quote(str(rel)),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(target.name)}",
    }
    return Response(status_code=200, headers=headers)

@app.get("/stream/{token}/{rel_path:path}")
def stream_file(token: str, rel_path: str, request: Request):
    row = get_job(token)
    if not row or row["status"] not in ("downloading", "paused", "complete") or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    if row["offloaded"]:
        m = manifest_entry(row, rel_path)
        if not m:
            raise HTTPException(status_code=404, detail="File not found")
        return RedirectResponse(manifest_public_url(m), status_code=302)
    target = safe_rel_path(Path(row["save_path"]), rel_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    rel = target.relative_to(DOWNLOAD_DIR)
    headers = {
        "X-Accel-Redirect": "/_protected_downloads/" + quote(str(rel)),
        "Content-Type": MIME.get(target.suffix.lower(), "application/octet-stream"),
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(target.name)}",
        "Accept-Ranges": "bytes",
    }
    return Response(status_code=200, headers=headers)

@app.get("/watch/{token}/{rel_path:path}", response_class=HTMLResponse)
def watch_file(token: str, rel_path: str, request: Request):
    row = get_job(token)
    if not row or row["status"] not in ("downloading", "paused", "complete") or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    key_q = ""
    if request.query_params.get("key"):
        key_q = "?key=" + quote(request.query_params.get("key"))
    base = Path(row["save_path"])
    track = ""
    if row["offloaded"]:
        stem = Path(unquote(rel_path)).stem
        for m in job_manifest(row):
            mp = Path(m["rel"])
            if mp.suffix.lower() in SUBTITLES and mp.stem.startswith(stem):
                track = f'<track kind="subtitles" label="Subtitles" srclang="en" src="/subtitle/{token}/{quote(m["rel"])}{key_q}" default>'
                break
    else:
        try:
            video_path = safe_rel_path(base, rel_path)
        except Exception:
            video_path = None
        if video_path and video_path.exists():
            sub = find_subtitle(base, video_path)
            if sub:
                track = f'<track kind="subtitles" label="Subtitles" srclang="en" src="/subtitle/{token}/{quote(str(sub.relative_to(base)))}{key_q}" default>'
    name = html.escape(Path(unquote(rel_path)).name)
    note = ""
    dl_btn = f'<a class="btn" href="/file/{token}/{rel_path}">Download file</a>'
    if row["status"] != "complete":
        note = '<p class="mut">Streaming while downloading — seeking past the downloaded part will buffer until it arrives.</p>'
        dl_btn = ""
    body = f"""
    <div class="card">
      <h1>{name}</h1>
      <video controls preload="metadata" src="/stream/{token}/{rel_path}{key_q}">{track}</video>
      {note}
      <p><a class="btn ghost" href="/job/{token}">Back</a> {dl_btn}</p>
    </div>"""
    return HTMLResponse(html_page(name, body))

@app.get("/remux/{token}/{rel_path:path}", response_class=HTMLResponse)
def remux_file(token: str, rel_path: str, request: Request):
    row = get_job(token)
    if not row or row["status"] != "complete" or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    if row["offloaded"]:
        raise HTTPException(status_code=404, detail="Stored in cloud storage — remux unavailable")
    base = Path(row["save_path"])
    target = safe_rel_path(base, rel_path)
    if not target.exists() or not target.is_file() or target.suffix.lower() not in REMUXABLE:
        raise HTTPException(status_code=404, detail="File not found or not convertible")
    out = target.with_suffix(".mp4")
    if out.exists():
        return RedirectResponse(f"/watch/{token}/{quote(str(out.relative_to(base)))}", status_code=303)
    key = f"{token}:{rel_path}"
    state = REMUX_JOBS.get(key)
    if state and state.startswith("error"):
        body = f'<div class="card"><h1>Conversion failed</h1><p>{html.escape(state[7:])}</p><p><a class="btn ghost" href="/job/{token}">Back</a></p></div>'
        return HTMLResponse(html_page("Conversion failed", body))
    if state != "running":
        REMUX_JOBS[key] = "running"
        threading.Thread(target=do_remux, args=(key, target, out), daemon=True).start()
    body = f"""
    <div class="card center">
      <h1>Preparing browser playback</h1>
      <p>Converting {html.escape(target.name)} to MP4 — video is copied, not re-encoded, so this is usually fast. The page refreshes automatically.</p>
      {bar(0.5)}
    </div>"""
    return HTMLResponse(html_page("Converting", body, refresh=4))

@app.get("/subtitle/{token}/{rel_path:path}")
def subtitle_file(token: str, rel_path: str, request: Request):
    row = get_job(token)
    if not row or row["status"] not in ("downloading", "paused", "complete") or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    if row["offloaded"]:
        m = manifest_entry(row, rel_path)
        if not m:
            raise HTTPException(status_code=404, detail="Subtitle not found")
        return RedirectResponse(manifest_public_url(m), status_code=302)
    target = safe_rel_path(Path(row["save_path"]), rel_path)
    if not target.exists() or not target.is_file() or target.suffix.lower() not in SUBTITLES:
        raise HTTPException(status_code=404, detail="Subtitle not found")
    raw = target.read_bytes()
    if target.suffix.lower() == ".vtt":
        return Response(content=raw, media_type="text/vtt")
    return Response(content=srt_to_vtt(raw), media_type="text/vtt")

@app.get("/zip/{token}")
def download_zip(token: str, request: Request):
    row = get_job(token)
    if not row or row["status"] != "complete" or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    if row["offloaded"]:
        body = f'<div class="card"><h1>Stored in cloud storage</h1><p>Files are in cloud storage — download them individually below.</p><p><a class="btn ghost" href="/files/{token}">Back to files</a></p></div>'
        return HTMLResponse(html_page("Cloud storage", body))
    base = Path(row["save_path"])
    files = [x for x in base.rglob("*") if x.is_file() and x.name != "input.torrent" and not x.name.endswith(".zip") and "_zip_extract" not in x.parts]
    if not files:
        raise HTTPException(status_code=404, detail="No files found")
    zip_path = base / f"{token}.zip"
    if not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for f in files:
                zf.write(f, f.relative_to(base))
    rel = zip_path.relative_to(DOWNLOAD_DIR)
    headers = {
        "X-Accel-Redirect": "/_protected_downloads/" + quote(str(rel)),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote((row['name'] or token) + '.zip')}",
    }
    return Response(status_code=200, headers=headers)

@app.get("/links/{token}")
def links_txt(token: str, request: Request):
    row = get_job(token)
    if not row or row["status"] != "complete" or row["expires_at"] < int(time.time()):
        raise HTTPException(status_code=404, detail="Link not found or expired")
    denied = link_denied(row, request)
    if denied:
        return denied
    key_q = f"?key={quote(row['link_password'])}" if row["link_password"] else ""
    lines = [f"{PUBLIC_BASE_URL}/file/{token}/{quote(m['rel'])}{key_q}" for m in job_manifest(row)]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=links-{token}.txt"})

@app.get("/gate/{token}", response_class=HTMLResponse)
def gate_page(token: str, next: str = ""):
    row = get_job(token)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    body = f"""
    <div class="card" style="max-width:460px;margin:60px auto">
      <h1>Password required</h1>
      <p>This link is password protected.</p>
      <form method="post" action="/gate/{token}">
        <input type="hidden" name="next" value="{html.escape(next)}">
        <input type="password" name="password" placeholder="Link password" autocomplete="off">
        <p><button class="btn" type="submit">Open</button></p>
      </form>
    </div>"""
    return HTMLResponse(html_page("Password required", body))

@app.post("/gate/{token}")
def gate_submit(token: str, password: str = Form(""), next: str = Form("")):
    row = get_job(token)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if row["link_password"] and password == row["link_password"]:
        target = next if next.startswith("/") else f"/files/{token}"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(f"link_{token}", password, max_age=7 * 24 * 3600, httponly=True, secure=True, samesite="lax")
        return resp
    return RedirectResponse(f"/gate/{token}", status_code=303)

@app.post("/link-settings/{token}")
def link_settings(token: str, password: str = Form(""), expiry_hours: str = Form("")):
    row = get_job(token)
    if not row or row["status"] != "complete":
        raise HTTPException(status_code=404, detail="Job not found")
    fields = {"link_password": password.strip() or None}
    if expiry_hours.strip():
        try:
            h = int(expiry_hours)
            fields["expires_at"] = int(time.time()) + max(1, min(h, 8760)) * 3600
        except ValueError:
            pass
    set_job(token, **fields)
    return RedirectResponse(f"/job/{token}", status_code=303)

@app.post("/offload/{token}")
def offload_now(token: str):
    row = get_job(token)
    if not row or row["status"] != "complete":
        raise HTTPException(status_code=404, detail="Job not found")
    if not s3_enabled():
        body = f'<div class="card"><h1>Cloud storage not configured</h1><p>Set S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY and S3_PUBLIC_URL in .env first.</p><p><a class="btn ghost" href="/job/{token}">Back</a></p></div>'
        return HTMLResponse(html_page("Not configured", body), status_code=400)
    if row["offloaded"] or OFFLOAD_JOBS.get(token) == "running":
        return RedirectResponse(f"/job/{token}", status_code=303)
    OFFLOAD_JOBS[token] = "running"
    threading.Thread(target=do_offload, args=(token,), daemon=True).start()
    return RedirectResponse(f"/job/{token}", status_code=303)

@app.post("/oci-events/{secret}")
async def oci_events(secret: str, request: Request):
    if not OCI_HOOK_SECRET or secret != OCI_HOOK_SECRET:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    sub_url = payload.get("SubscribeURL") or payload.get("subscribeURL")
    if sub_url:
        try:
            requests.get(sub_url, timeout=15)
            print("oci subscription confirmed", flush=True)
        except Exception as e:
            print(f"oci confirm error: {e}", flush=True)
        return {"ok": True}
    events = payload if isinstance(payload, list) else [payload]
    sent = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message")
        if isinstance(msg, str):
            try:
                ev = json.loads(msg)
            except Exception:
                continue
        etype = ev.get("eventType", "")
        data = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}
        name = data.get("resourceName", "")
        if "createobject" in etype:
            text = f"☁️ File stored in Oracle bucket: {name}"
        elif "deleteobject" in etype:
            text = f"🗑 File deleted from Oracle bucket: {name}"
        elif etype:
            text = f"☁️ Oracle event: {etype} {name}"
        else:
            continue
        notify(text)
        sent += 1
        if sent >= 10:
            break
    return {"ok": True, "sent": sent}

# ----------------------------- api -----------------------------

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/job/{token}")
def job_api(token: str):
    row = get_job(token)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "token": row["token"],
        "name": row["name"],
        "status": row["status"],
        "progress": row["progress"],
        "total_size": row["total_size"],
        "expires_at": row["expires_at"],
    }

# ----------------------------- monitor -----------------------------

def monitor():
    while True:
        try:
            conn = db()
            rows = conn.execute("SELECT * FROM jobs WHERE status IN ('metadata','waiting_selection','downloading','paused')").fetchall()
            client = qbit()
            try:
                torrents = client.info()
            except Exception:
                torrents = []
            by_path = {str(t.get("save_path", "")): t for t in torrents}
            by_hash = {str(t.get("hash", "")).lower(): t for t in torrents}
            for row in rows:
                t = None
                h = (row["torrent_hash"] or "").lower()
                if h and h in by_hash:
                    t = by_hash[h]
                elif row["save_path"] in by_path:
                    t = by_path[row["save_path"]]
                if not t:
                    continue
                size_gb = (t.get("total_size") or 0) / (1024 ** 3)
                if size_gb > MAX_TORRENT_SIZE_GB:
                    client.delete(t["hash"], True)
                    shutil.rmtree(row["save_path"], ignore_errors=True)
                    conn.execute("UPDATE jobs SET status='error', error=? WHERE id=?", (f"Torrent exceeds the {MAX_TORRENT_SIZE_GB:g} GB limit.", row["id"]))
                    continue
                if row["status"] == "metadata" and t.get("has_metadata"):
                    try:
                        files = client.files(t["hash"])
                    except Exception:
                        files = []
                    if files:
                        usage = shutil.disk_usage(DOWNLOAD_DIR)
                        if (t.get("total_size") or 0) > usage.total - MIN_FREE_BYTES:
                            client.delete(t["hash"], True)
                            shutil.rmtree(row["save_path"], ignore_errors=True)
                            conn.execute("UPDATE jobs SET status='error', error=? WHERE id=?", ("This torrent is larger than the server's total storage.", row["id"]))
                            continue
                        for f in files:
                            client.set_prio(t["hash"], f["index"], 0)
                        if len(files) == 1:
                            client.set_prio(t["hash"], files[0]["index"], 1)
                            active_now = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='downloading'").fetchone()["c"]
                            if active_now >= MAX_ACTIVE_DOWNLOADS:
                                conn.execute("UPDATE jobs SET selected_ids=? WHERE id=?", (str(files[0]["index"]), row["id"]))
                                new_status = "queued"
                            else:
                                client.start(t["hash"])
                                new_status = "downloading"
                        else:
                            new_status = "waiting_selection"
                        conn.execute(
                            "UPDATE jobs SET status=?, torrent_hash=?, name=?, total_size=? WHERE id=?",
                            (new_status, t["hash"], t.get("name"), t.get("total_size") or 0, row["id"]),
                        )
                elif row["status"] in ("downloading", "paused"):
                    if row["status"] == "downloading":
                        try:
                            client.enable_streaming(t)
                        except Exception:
                            pass
                    prog = float(t.get("progress") or 0)
                    new_status = "complete" if prog >= 1 else row["status"]
                    if new_status == "complete":
                        notify(f"✅ Download complete: {t.get('name') or row['name'] or 'torrent'}\n{PUBLIC_BASE_URL}/files/{row['token']}")
                        if S3_AUTO_OFFLOAD and s3_enabled() and OFFLOAD_JOBS.get(row["token"]) is None:
                            OFFLOAD_JOBS[row["token"]] = "running"
                            threading.Thread(target=do_offload, args=(row["token"],), daemon=True).start()
                    conn.execute(
                        "UPDATE jobs SET status=?, progress=?, torrent_hash=?, name=?, total_size=? WHERE id=?",
                        (new_status, prog, t["hash"], t.get("name"), t.get("total_size") or 0, row["id"]),
                    )
            queued = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC").fetchall()
            if queued:
                active = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='downloading'").fetchone()["c"]
                for q in queued:
                    if active >= MAX_ACTIVE_DOWNLOADS:
                        break
                    if not q["torrent_hash"]:
                        continue
                    try:
                        qfiles = client.files(q["torrent_hash"])
                    except Exception:
                        continue
                    idset = {int(x) for x in (q["selected_ids"] or "").split(",") if x.strip()}
                    if not idset:
                        idset = {f.get("index") for f in qfiles}
                    need = sum(f.get("size", 0) for f in qfiles if f.get("index") in idset)
                    free_now = shutil.disk_usage(DOWNLOAD_DIR).free
                    if need > free_now - MIN_FREE_BYTES:
                        continue
                    for f in qfiles:
                        client.set_prio(q["torrent_hash"], f["index"], 0)
                    for i in idset:
                        client.set_prio(q["torrent_hash"], i, 1)
                    client.start(q["torrent_hash"])
                    conn.execute("UPDATE jobs SET status='downloading' WHERE id=?", (q["id"],))
                    active += 1
            now = int(time.time())
            expired = conn.execute("SELECT * FROM jobs WHERE expires_at < ? AND status NOT IN ('expired','deleted')", (now,)).fetchall()
            for row in expired:
                try:
                    if row["torrent_hash"]:
                        client.delete(row["torrent_hash"], True)
                except Exception:
                    pass
                delete_s3_files(row)
                shutil.rmtree(row["save_path"], ignore_errors=True)
                conn.execute("UPDATE jobs SET status='expired' WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"monitor error: {e}", flush=True)
        time.sleep(4)

def main():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    db()
    threading.Thread(target=monitor, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

if __name__ == "__main__":
    main()
