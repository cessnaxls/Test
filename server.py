from __future__ import annotations

import os
import re
import json
import time
import queue
import threading
import concurrent.futures
from typing import Any

import numpy as np

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TABLE = os.getenv("PROFILE_TABLE", "scraped_profiles_v2")

CLIP_CLAIM_SIZE = max(16, int(os.getenv("CLIP_CLAIM_SIZE", "128")))
CLIP_INFER_BATCH = max(4, int(os.getenv("CLIP_INFER_BATCH", "32")))
CLIP_DOWNLOAD_WORKERS = max(4, int(os.getenv("CLIP_DOWNLOAD_WORKERS", "32")))
CLIP_READY_QUEUE = max(CLIP_INFER_BATCH * 2, int(os.getenv("CLIP_READY_QUEUE", "256")))
CLIP_POLL_SECONDS = max(0.05, float(os.getenv("CLIP_POLL_SECONDS", "0.10")))

CLIP_LOCK = threading.Lock()
CLIP_STATE_LOCK = threading.Lock()
CLIP_STOP = threading.Event()
CLIP_RUNNING = False
CLIP_PROCESSING = 0
CLIP_LAST_ERROR = ""
CLIP_RATE_EVENTS = []
CLIP_READY = queue.Queue(maxsize=CLIP_READY_QUEUE)

app = FastAPI(title="Instagram Profile Gallery", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.instagram.com", "https://instagram.com"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "web", "static")), name="static")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")

class ProfileBatch(BaseModel):
    source_url: str | None = Field(default=None, max_length=2000)
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)

def require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Supabase is not configured on Render. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")

def supa(method: str, path: str, *, params=None, payload=None, prefer=None):
    require_supabase()

    headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }

    # New sb_secret_* keys are API keys, not JWTs.
    # Legacy service_role keys are JWTs and may be sent as Bearer tokens.
    if not SUPABASE_KEY.startswith("sb_secret_") and SUPABASE_KEY.count(".") == 2:
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"

    if prefer:
        headers["Prefer"] = prefer

    try:
        r = requests.request(
            method,
            f"{SUPABASE_URL}/rest/v1/{path}",
            params=params,
            json=payload,
            headers=headers,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Could not reach Supabase: {exc}") from exc

    if not r.ok:
        raise HTTPException(
            r.status_code,
            f"Supabase {r.status_code}: {r.text[:600]}"
        )

    if not r.text:
        return []

    try:
        return r.json()
    except ValueError as exc:
        raise HTTPException(
            502,
            f"Supabase returned invalid JSON: {r.text[:300]}"
        ) from exc


def clean_username(value: Any) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    return username if USERNAME_RE.fullmatch(username) else ""


CLIP_HTTP = requests.Session()
CLIP_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://www.instagram.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})
CLIP_HTTP.mount(
    "https://",
    requests.adapters.HTTPAdapter(
        pool_connections=max(32, CLIP_DOWNLOAD_WORKERS),
        pool_maxsize=max(64, CLIP_DOWNLOAD_WORKERS * 2),
        max_retries=0,
    ),
)


def clip_mark_many(usernames: list[str], **fields):
    if not usernames:
        return
    for i in range(0, len(usernames), 100):
        chunk = usernames[i:i+100]
        supa(
            "PATCH",
            TABLE,
            params={"username": f"in.({','.join(chunk)})"},
            payload=fields,
            prefer="return=minimal",
        )


def clip_pending(limit: int):
    return supa(
        "GET",
        TABLE,
        params={
            "select": "username,image_url,clip_index_status",
            "or": "(clip_index_status.is.null,clip_index_status.eq.queued)",
            "image_url": "neq.",
            "order": "last_seen.desc",
            "limit": str(limit),
        },
    )


def clip_fetch_avatar(row):
    url = str(row.get("image_url") or "")
    if not url:
        return row, None, "missing image_url"

    last = None
    for attempt in range(3):
        try:
            r = CLIP_HTTP.get(url, timeout=(5, 12), allow_redirects=True)
            if r.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.12 * (2 ** attempt))
                continue
            r.raise_for_status()

            if not r.headers.get("content-type", "").startswith("image/"):
                raise ValueError("avatar did not return an image")
            if len(r.content) > 4_000_000:
                raise ValueError("avatar too large")

            return row, r.content, None
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.12 * (2 ** attempt))

    return row, None, str(last or "avatar download failed")[:260]


def clip_bulk_write(rows, vectors):
    if not rows:
        return

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    items = []

    for row, vector in zip(rows, vectors):
        items.append({
            "username": row["username"],
            "clip_embedding": json.dumps(vector, separators=(",", ":")),
            "clip_index_status": "indexed",
            "clip_error": None,
            "clip_indexed_at": now,
        })

    supa(
        "POST",
        "rpc/bulk_set_clip_embeddings",
        payload={"items": items},
        prefer="return=minimal",
    )


def clip_record_rate(count: int):
    global CLIP_RATE_EVENTS
    now = time.time()
    with CLIP_STATE_LOCK:
        CLIP_RATE_EVENTS.extend([now] * count)
        cutoff = now - 60
        CLIP_RATE_EVENTS = [t for t in CLIP_RATE_EVENTS if t >= cutoff]


def clip_rate():
    with CLIP_STATE_LOCK:
        now = time.time()
        recent = [t for t in CLIP_RATE_EVENTS if t >= now - 60]
    if len(recent) < 2:
        return 0.0
    return len(recent) / max(now - recent[0], 1.0)


def clip_pipeline():
    global CLIP_RUNNING, CLIP_PROCESSING, CLIP_LAST_ERROR

    downloader = concurrent.futures.ThreadPoolExecutor(
        max_workers=CLIP_DOWNLOAD_WORKERS,
        thread_name_prefix="clip-avatar",
    )

    try:
        # Heavy imports happen ONLY after the user explicitly presses Start.
        from clip_engine import image_embeddings

        while not CLIP_STOP.is_set():
            rows = []

            try:
                with CLIP_LOCK:
                    rows = clip_pending(CLIP_CLAIM_SIZE)
                    if rows:
                        clip_mark_many(
                            [row["username"] for row in rows],
                            clip_index_status="indexing",
                            clip_error=None,
                        )

                if not rows:
                    time.sleep(CLIP_POLL_SECONDS)
                    continue

                futures = [downloader.submit(clip_fetch_avatar, row) for row in rows]
                ready_rows = []
                ready_data = []

                for future in concurrent.futures.as_completed(futures):
                    if CLIP_STOP.is_set():
                        break

                    row, data, error = future.result()

                    if error or data is None:
                        clip_mark_many(
                            [row["username"]],
                            clip_index_status="failed",
                            clip_error=error or "avatar download failed",
                        )
                        continue

                    ready_rows.append(row)
                    ready_data.append(data)

                    if len(ready_rows) >= CLIP_INFER_BATCH:
                        with CLIP_STATE_LOCK:
                            CLIP_PROCESSING = len(ready_rows)

                        vectors = image_embeddings(ready_data)
                        clip_bulk_write(ready_rows, vectors)
                        clip_record_rate(len(ready_rows))

                        ready_rows = []
                        ready_data = []

                if ready_rows and not CLIP_STOP.is_set():
                    with CLIP_STATE_LOCK:
                        CLIP_PROCESSING = len(ready_rows)

                    vectors = image_embeddings(ready_data)
                    clip_bulk_write(ready_rows, vectors)
                    clip_record_rate(len(ready_rows))

            except Exception as exc:
                CLIP_LAST_ERROR = str(exc)[:300]
                if rows:
                    try:
                        clip_mark_many(
                            [row["username"] for row in rows],
                            clip_index_status="queued",
                            clip_error=CLIP_LAST_ERROR,
                        )
                    except Exception:
                        pass
                time.sleep(0.4)

            finally:
                with CLIP_STATE_LOCK:
                    CLIP_PROCESSING = 0

    finally:
        downloader.shutdown(wait=False, cancel_futures=True)
        CLIP_RUNNING = False


def recover_clip_rows():
    try:
        supa(
            "PATCH",
            TABLE,
            params={"clip_index_status": "eq.indexing"},
            payload={"clip_index_status": "queued"},
            prefer="return=minimal",
        )
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE, "web", "index.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "2.1.0-stable-clip",
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "table": TABLE,
        "key_type": (
            "secret"
            if SUPABASE_KEY.startswith("sb_secret_")
            else "legacy_jwt"
            if SUPABASE_KEY.count(".") == 2
            else "unknown"
        ),
    }

@app.get("/api/debug/supabase")
def debug_supabase():
    rows = supa(
        "GET",
        TABLE,
        params={"select": "username", "limit": "1"},
    )
    return {
        "ok": True,
        "table": TABLE,
        "sample_rows": len(rows),
    }

@app.post("/api/profiles/batch")
def ingest_profiles(req: ProfileBatch):
    unique = {}
    for raw in req.records:
        username = clean_username(raw.get("username"))
        if not username:
            continue
        row = {
            "username": username,
            "full_name": str(raw.get("full_name") or "")[:300],
            "profile_url": str(raw.get("profile_url") or f"https://www.instagram.com/{username}/")[:1000],
            "image_url": str(raw.get("image_url") or "")[:5000],
            "source_url": str(raw.get("source_url") or req.source_url or "")[:2000],
        }
        old = unique.get(username)
        if old is None or (not old["image_url"] and row["image_url"]):
            unique[username] = row

    rows = list(unique.values())
    if not rows:
        return {"received": len(req.records), "accepted": 0, "with_photos": 0}

    for i in range(0, len(rows), 250):
        supa(
            "POST",
            f"{TABLE}?on_conflict=username",
            payload=rows[i:i+250],
            prefer="resolution=merge-duplicates,return=minimal",
        )

    return {
        "received": len(req.records),
        "accepted": len(rows),
        "with_photos": sum(1 for row in rows if row["image_url"]),
    }

@app.get("/api/profiles")
def get_profiles(
    limit: int = Query(default=250, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    photos_only: bool = Query(default=False),
):
    params = {
        "select": "username,full_name,profile_url,image_url,source_url,first_seen,last_seen,seen_count,clip_index_status",
        "order": "last_seen.desc",
        "limit": str(limit),
        "offset": str(offset),
    }
    if photos_only:
        params["image_url"] = "neq."
    rows = supa("GET", TABLE, params=params)
    return {"profiles": rows, "limit": limit, "offset": offset}

@app.get("/api/stats")
def stats():
    total = 0
    with_photos = 0
    offset = 0
    while True:
        rows = supa(
            "GET",
            TABLE,
            params={"select": "username,image_url", "limit": "1000", "offset": str(offset)},
        )
        if not rows:
            break
        total += len(rows)
        with_photos += sum(1 for row in rows if row.get("image_url"))
        if len(rows) < 1000:
            break
        offset += 1000
    return {"total": total, "with_photos": with_photos, "without_photos": max(total-with_photos, 0)}


@app.get("/api/avatar")
def avatar(url: str = Query(..., min_length=8, max_length=5000)):
    if not (
        url.startswith("https://")
        and any(host in url.lower() for host in ("cdninstagram", "fbcdn", "scontent"))
    ):
        raise HTTPException(400, "Unsupported avatar URL")

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://www.instagram.com/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        raise HTTPException(502, f"Avatar fetch failed: {exc}") from exc

    if not r.ok:
        raise HTTPException(r.status_code, f"Avatar CDN returned {r.status_code}")

    content_type = r.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise HTTPException(502, "Avatar URL did not return an image")

    return Response(
        content=r.content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",
        },
    )


@app.post("/api/clip/start")
def clip_start():
    global CLIP_RUNNING, CLIP_LAST_ERROR

    if CLIP_RUNNING:
        return {"ok": True, "running": True}

    recover_clip_rows()
    CLIP_STOP.clear()
    CLIP_LAST_ERROR = ""
    CLIP_RUNNING = True

    threading.Thread(
        target=clip_pipeline,
        name="clip-pipeline",
        daemon=True,
    ).start()

    return {"ok": True, "running": True}


@app.post("/api/clip/stop")
def clip_stop():
    CLIP_STOP.set()
    return {"ok": True}


@app.post("/api/clip/reindex")
def clip_reindex():
    if CLIP_RUNNING:
        raise HTTPException(409, "Stop CLIP indexing before reindexing.")

    supa(
        "PATCH",
        TABLE,
        params={"username": "neq.__never_real__"},
        payload={
            "clip_embedding": None,
            "clip_index_status": "queued",
            "clip_error": None,
            "clip_indexed_at": None,
        },
        prefer="return=minimal",
    )

    return {"ok": True}


@app.get("/api/clip/stats")
def clip_stats():
    total = indexed = failed = queued = 0
    offset = 0

    while True:
        rows = supa(
            "GET",
            TABLE,
            params={
                "select": "clip_index_status",
                "limit": "1000",
                "offset": str(offset),
            },
        )

        if not rows:
            break

        total += len(rows)
        indexed += sum(1 for r in rows if r.get("clip_index_status") == "indexed")
        failed += sum(1 for r in rows if r.get("clip_index_status") == "failed")
        queued += sum(
            1 for r in rows
            if r.get("clip_index_status") in (None, "queued", "indexing")
        )

        if len(rows) < 1000:
            break

        offset += 1000

    with CLIP_STATE_LOCK:
        processing = CLIP_PROCESSING
        last_error = CLIP_LAST_ERROR
        running = CLIP_RUNNING

    return {
        "total": total,
        "indexed": indexed,
        "failed": failed,
        "queued": queued,
        "processing": processing,
        "running": running,
        "images_per_second": round(clip_rate(), 2),
        "download_workers": CLIP_DOWNLOAD_WORKERS,
        "batch_size": CLIP_INFER_BATCH,
        "last_error": last_error,
    }


@app.get("/api/clip/search/text")
def clip_text_search(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=10000, ge=1, le=10000),
):
    # Heavy CLIP load happens on demand, never during app startup.
    from clip_engine import text_embedding

    query = np.asarray(text_embedding(q.strip()), dtype=np.float32)
    query /= max(float(np.linalg.norm(query)), 1e-12)

    scored = []
    offset = 0
    page = 500

    while True:
        rows = supa(
            "GET",
            TABLE,
            params={
                "select": "username,full_name,profile_url,image_url,source_url,seen_count,clip_embedding",
                "clip_index_status": "eq.indexed",
                "limit": str(page),
                "offset": str(offset),
            },
        )

        if not rows:
            break

        for row in rows:
            raw = row.pop("clip_embedding", None)
            if not raw:
                continue

            try:
                vector = np.asarray(json.loads(raw), dtype=np.float32)
            except Exception:
                continue

            if vector.ndim != 1 or vector.size != query.size:
                continue

            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            row["score"] = float(np.dot(query, vector))
            scored.append(row)

        if len(rows) < page:
            break

        offset += page

    scored.sort(key=lambda r: r["score"], reverse=True)

    return {
        "query": q,
        "indexed_searched": len(scored),
        "profiles": scored[:limit],
    }


@app.delete("/api/profiles")
def clear_profiles():
    supa(
        "DELETE",
        TABLE,
        params={"username": "neq.__never_real__"},
        prefer="return=minimal",
    )
    return {"ok": True}
