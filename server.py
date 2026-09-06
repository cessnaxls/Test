from __future__ import annotations

import json, os, sqlite3, threading, time, uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "jobs.sqlite3"
DB_LOCK = threading.RLock()
MODES = {"likers", "commenters", "profile_followers", "profile_following", "post_stream_likers"}

app = FastAPI(title="Instagram Safari Collector", version="3.0")
app.mount("/static", StaticFiles(directory=BASE / "web" / "static"), name="static")


def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _column_exists(c: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in c.execute(f"PRAGMA table_info({table})"))


def init_db():
    with DB_LOCK, conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, device_id TEXT NOT NULL, url TEXT NOT NULL, source_mode TEXT NOT NULL,
          browser_windows INTEGER NOT NULL, profile_limit INTEGER NOT NULL, comment_limit INTEGER NOT NULL,
          exact_like_count INTEGER, time_limit_seconds INTEGER, status TEXT NOT NULL,
          message TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
          stop_requested INTEGER NOT NULL DEFAULT 0, workers_done INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS jobs_device_idx ON jobs(device_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS records(
          job_id TEXT NOT NULL, username TEXT NOT NULL, full_name TEXT, profile_url TEXT,
          image_url TEXT, source TEXT, post_url TEXT, worker INTEGER, payload TEXT,
          PRIMARY KEY(job_id, username)
        );

        CREATE TABLE IF NOT EXISTS live_profiles(
          device_id TEXT NOT NULL, username TEXT NOT NULL, full_name TEXT, profile_url TEXT,
          image_url TEXT, source TEXT, post_url TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
          seen_count INTEGER NOT NULL DEFAULT 1, payload TEXT,
          PRIMARY KEY(device_id, username)
        );
        CREATE INDEX IF NOT EXISTS live_profiles_device_seen_idx ON live_profiles(device_id, last_seen DESC);
        CREATE TABLE IF NOT EXISTS live_devices(
          device_id TEXT PRIMARY KEY, scanner_enabled INTEGER NOT NULL DEFAULT 0,
          last_seen REAL, last_page TEXT, last_message TEXT, updated_at REAL NOT NULL
        );
        """)
init_db()


class StartRequest(BaseModel):
    device_id: str = Field(min_length=4, max_length=100)
    url: str = Field(min_length=3, max_length=1000)
    source_mode: str
    browser_windows: int = Field(default=1, ge=1, le=10)
    profile_limit: int = Field(default=1000, ge=1, le=1000000)
    comment_limit: int = Field(default=2000, ge=1, le=100000)
    exact_like_count: int | None = Field(default=None, ge=0)
    time_limit_seconds: int | None = Field(default=None, ge=1, le=86400)


class RecordsRequest(BaseModel):
    device_id: str
    worker: int = Field(ge=0, le=9)
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    message: str | None = None


class ProgressRequest(BaseModel):
    device_id: str
    worker: int = Field(ge=0, le=9)
    message: str


class FinishRequest(BaseModel):
    device_id: str
    worker: int = Field(ge=0, le=9)


class LiveIngestRequest(BaseModel):
    device_id: str = Field(min_length=4, max_length=100)
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    page_url: str | None = Field(default=None, max_length=2000)
    message: str | None = Field(default=None, max_length=1000)


class LiveStateRequest(BaseModel):
    device_id: str = Field(min_length=4, max_length=100)
    enabled: bool
    page_url: str | None = Field(default=None, max_length=2000)


def job_row(job_id: str):
    with conn() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def require_job(job_id: str, device_id: str | None = None):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    if device_id is not None and row["device_id"] != device_id:
        raise HTTPException(403, "Device mismatch")
    return row


def snapshot(row):
    with conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM records WHERE job_id=?", (row["id"],)).fetchone()["n"]
    return {**dict(row), "profiles_collected": n, "stop_requested": bool(row["stop_requested"])}


def live_stats(device_id: str) -> dict[str, Any]:
    with conn() as c:
        d = c.execute("SELECT * FROM live_devices WHERE device_id=?", (device_id,)).fetchone()
        total = c.execute("SELECT COUNT(*) n FROM live_profiles WHERE device_id=?", (device_id,)).fetchone()["n"]
    return {
        "device_id": device_id,
        "scanner_enabled": bool(d["scanner_enabled"]) if d else False,
        "profiles_collected": total,
        "last_seen": d["last_seen"] if d else None,
        "last_page": d["last_page"] if d else None,
        "message": d["last_message"] if d else "Scanner has not connected yet",
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "web" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"ok": True, "version": "3.0"}


# ---------------------------- Live Safari scanner ----------------------------
@app.post("/api/live/state")
def set_live_state(req: LiveStateRequest):
    now = time.time()
    with DB_LOCK, conn() as c:
        c.execute("""INSERT INTO live_devices(device_id,scanner_enabled,last_seen,last_page,last_message,updated_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET
        scanner_enabled=excluded.scanner_enabled,last_seen=excluded.last_seen,
        last_page=COALESCE(excluded.last_page,live_devices.last_page),
        last_message=excluded.last_message,updated_at=excluded.updated_at""",
        (req.device_id, 1 if req.enabled else 0, now, req.page_url,
         "Scanner ON" if req.enabled else "Scanner OFF", now))
    return live_stats(req.device_id)


@app.post("/api/live/ingest")
def live_ingest(req: LiveIngestRequest):
    now = time.time()
    inserted = 0
    touched = 0
    with DB_LOCK, conn() as c:
        for r in req.records:
            u = str(r.get("username", "")).strip().lstrip("@").lower()
            if not u or len(u) > 64:
                continue
            full = str(r.get("full_name") or r.get("name") or "")[:500]
            profile = str(r.get("profile_url") or f"https://www.instagram.com/{u}/")[:1000]
            image = str(r.get("image_url") or "")[:4000]
            source = str(r.get("source") or "live_scroll")[:100]
            post = str(r.get("post_url") or req.page_url or "")[:1000]
            existed = c.execute("SELECT 1 FROM live_profiles WHERE device_id=? AND username=?", (req.device_id, u)).fetchone()
            c.execute("""INSERT INTO live_profiles(device_id,username,full_name,profile_url,image_url,source,post_url,first_seen,last_seen,seen_count,payload)
            VALUES(?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(device_id,username) DO UPDATE SET
            full_name=CASE WHEN length(excluded.full_name)>length(live_profiles.full_name) THEN excluded.full_name ELSE live_profiles.full_name END,
            profile_url=COALESCE(NULLIF(excluded.profile_url,''),live_profiles.profile_url),
            image_url=COALESCE(NULLIF(excluded.image_url,''),live_profiles.image_url),
            source=COALESCE(NULLIF(excluded.source,''),live_profiles.source),
            post_url=COALESCE(NULLIF(excluded.post_url,''),live_profiles.post_url),
            last_seen=excluded.last_seen, seen_count=live_profiles.seen_count+1,
            payload=excluded.payload""",
            (req.device_id, u, full, profile, image, source, post, now, now, json.dumps(r, ensure_ascii=False)))
            touched += 1
            if not existed:
                inserted += 1
        total = c.execute("SELECT COUNT(*) n FROM live_profiles WHERE device_id=?", (req.device_id,)).fetchone()["n"]
        message = req.message or f"Received {touched} profiles ({inserted} new); {total:,} unique total"
        c.execute("""INSERT INTO live_devices(device_id,scanner_enabled,last_seen,last_page,last_message,updated_at)
        VALUES(?,1,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET
        scanner_enabled=1,last_seen=excluded.last_seen,last_page=excluded.last_page,
        last_message=excluded.last_message,updated_at=excluded.updated_at""",
        (req.device_id, now, req.page_url, message[:1000], now))
    return {"accepted": touched, "new_profiles": inserted, "profiles_collected": total}


@app.get("/api/live/stats")
def get_live_stats(device_id: str):
    return live_stats(device_id)


@app.get("/api/live/profiles")
def get_live_profiles(device_id: str, limit: int = 200, offset: int = 0):
    with conn() as c:
        rows = c.execute("""SELECT username,full_name,profile_url,image_url,source,post_url,first_seen,last_seen,seen_count
        FROM live_profiles WHERE device_id=? ORDER BY last_seen DESC LIMIT ? OFFSET ?""",
        (device_id, min(max(limit, 1), 1000), max(offset, 0))).fetchall()
    return {"profiles": [dict(r) for r in rows]}


@app.get("/api/live/search")
def search_live(device_id: str, q: str = "", limit: int = 100):
    q = q.strip().lower()
    with conn() as c:
        if q:
            rows = c.execute("""SELECT username,full_name,profile_url,image_url,source,post_url,first_seen,last_seen,seen_count
            FROM live_profiles WHERE device_id=? AND (lower(username) LIKE ? OR lower(full_name) LIKE ?)
            ORDER BY last_seen DESC LIMIT ?""", (device_id, f"%{q}%", f"%{q}%", min(limit, 1000))).fetchall()
        else:
            rows = c.execute("""SELECT username,full_name,profile_url,image_url,source,post_url,first_seen,last_seen,seen_count
            FROM live_profiles WHERE device_id=? ORDER BY last_seen DESC LIMIT ?""",
            (device_id, min(limit, 1000))).fetchall()
    return {"profiles": [dict(r) for r in rows], "semantic": False}


@app.delete("/api/live/profiles")
def clear_live(device_id: str):
    with DB_LOCK, conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM live_profiles WHERE device_id=?", (device_id,)).fetchone()["n"]
        c.execute("DELETE FROM live_profiles WHERE device_id=?", (device_id,))
        c.execute("UPDATE live_devices SET last_message='Library cleared', updated_at=? WHERE device_id=?", (time.time(), device_id))
    return {"deleted": n}


# ---------------------------- Existing automated modes ----------------------------
@app.post("/api/jobs")
def start(req: StartRequest):
    if req.source_mode not in MODES:
        raise HTTPException(400, "Unknown source mode")
    now = time.time(); jid = uuid.uuid4().hex
    with DB_LOCK, conn() as c:
        c.execute("UPDATE jobs SET status='superseded', updated_at=? WHERE device_id=? AND status IN ('queued','scraping')", (now, req.device_id))
        c.execute("""INSERT INTO jobs(id,device_id,url,source_mode,browser_windows,profile_limit,comment_limit,exact_like_count,time_limit_seconds,status,message,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,'queued','Waiting for Safari extension',?,?)""",
        (jid, req.device_id, req.url, req.source_mode, req.browser_windows, req.profile_limit, req.comment_limit, req.exact_like_count, req.time_limit_seconds, now, now))
    return snapshot(job_row(jid))


@app.get("/api/extension/active")
def extension_active(device_id: str):
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE device_id=? AND status IN ('queued','scraping') ORDER BY created_at DESC LIMIT 1", (device_id,)).fetchone()
    return {"job": snapshot(row) if row else None}


@app.post("/api/jobs/{job_id}/claim")
def claim(job_id: str, body: dict[str, Any]):
    device_id = str(body.get("device_id", "")); require_job(job_id, device_id)
    with DB_LOCK, conn() as c:
        c.execute("UPDATE jobs SET status='scraping', message='Safari workers running', updated_at=? WHERE id=?", (time.time(), job_id))
    return snapshot(job_row(job_id))


@app.post("/api/jobs/{job_id}/records")
def add_records(job_id: str, req: RecordsRequest):
    row = require_job(job_id, req.device_id)
    if row["stop_requested"]:
        return {"accepted": 0, "stop": True}
    accepted = 0
    with DB_LOCK, conn() as c:
        for r in req.records:
            u = str(r.get("username", "")).strip().lstrip("@").lower()
            if not u:
                continue
            full = str(r.get("full_name") or r.get("name") or "")[:500]
            profile = str(r.get("profile_url") or f"https://www.instagram.com/{u}/")[:1000]
            image = str(r.get("image_url") or "")[:4000]
            source = str(r.get("source") or row["source_mode"])[:100]
            post = str(r.get("post_url") or "")[:1000]
            c.execute("""INSERT INTO records(job_id,username,full_name,profile_url,image_url,source,post_url,worker,payload)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,username) DO UPDATE SET
            full_name=CASE WHEN length(excluded.full_name)>length(records.full_name) THEN excluded.full_name ELSE records.full_name END,
            profile_url=COALESCE(NULLIF(excluded.profile_url,''),records.profile_url),
            image_url=COALESCE(NULLIF(excluded.image_url,''),records.image_url),
            post_url=COALESCE(NULLIF(excluded.post_url,''),records.post_url), worker=excluded.worker, payload=excluded.payload""",
            (job_id, u, full, profile, image, source, post, req.worker, json.dumps(r, ensure_ascii=False)))
            accepted += 1
        count = c.execute("SELECT COUNT(*) n FROM records WHERE job_id=?", (job_id,)).fetchone()["n"]
        msg = req.message or f"Collected {count:,} unique profiles"
        stop = count >= row["profile_limit"]
        c.execute("UPDATE jobs SET message=?, stop_requested=CASE WHEN ? THEN 1 ELSE stop_requested END, updated_at=? WHERE id=?", (msg, 1 if stop else 0, time.time(), job_id))
    return {"accepted": accepted, "profiles_collected": count, "stop": stop}


@app.post("/api/jobs/{job_id}/progress")
def progress(job_id: str, req: ProgressRequest):
    row = require_job(job_id, req.device_id)
    with DB_LOCK, conn() as c:
        c.execute("UPDATE jobs SET message=?, updated_at=? WHERE id=?", (req.message[:1000], time.time(), job_id))
    return {"ok": True, "stop": bool(row["stop_requested"])}


@app.post("/api/jobs/{job_id}/finish")
def finish(job_id: str, req: FinishRequest):
    require_job(job_id, req.device_id)
    with DB_LOCK, conn() as c:
        c.execute("UPDATE jobs SET workers_done=workers_done+1, updated_at=? WHERE id=?", (time.time(), job_id))
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        done = r["workers_done"] >= r["browser_windows"]
        if done:
            c.execute("UPDATE jobs SET status='complete', message='Collection complete', updated_at=? WHERE id=?", (time.time(), job_id))
    return snapshot(job_row(job_id))


@app.post("/api/jobs/{job_id}/stop")
def stop(job_id: str, body: dict[str, Any]):
    device_id = str(body.get("device_id", "")); require_job(job_id, device_id)
    with DB_LOCK, conn() as c:
        c.execute("UPDATE jobs SET stop_requested=1, message='Stop requested; preserving collected profiles', updated_at=? WHERE id=?", (time.time(), job_id))
    return snapshot(job_row(job_id))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return snapshot(require_job(job_id))


@app.get("/api/jobs/{job_id}/profiles")
def profiles(job_id: str, limit: int = 200, offset: int = 0):
    require_job(job_id)
    with conn() as c:
        rows = c.execute("SELECT username,full_name,profile_url,image_url,source,post_url,worker FROM records WHERE job_id=? ORDER BY username LIMIT ? OFFSET ?", (job_id, min(limit, 1000), max(offset, 0))).fetchall()
    return {"profiles": [dict(r) for r in rows]}


@app.get("/api/jobs/{job_id}/search")
def search(job_id: str, q: str, limit: int = 100):
    require_job(job_id); q = q.strip().lower()
    with conn() as c:
        rows = c.execute("""SELECT username,full_name,profile_url,image_url,source,post_url,worker FROM records
        WHERE job_id=? AND (lower(username) LIKE ? OR lower(full_name) LIKE ?) ORDER BY username LIMIT ?""",
        (job_id, f"%{q}%", f"%{q}%", min(limit, 1000))).fetchall()
    return {"profiles": [dict(r) for r in rows], "semantic": False,
            "note": "Text search is enabled. Add a separate embedding service for CLIP semantic image search."}


@app.middleware("http")
async def cors(request: Request, call_next):
    if request.method == "OPTIONS":
        resp = JSONResponse({"ok": True})
    else:
        resp = await call_next(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "content-type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    return resp
