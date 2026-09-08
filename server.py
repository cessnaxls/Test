from __future__ import annotations

import json
import os
import re
import threading
import time
import concurrent.futures
import queue
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
CLIP_INFER_BATCH = max(8, int(os.getenv("CLIP_INFER_BATCH", "64")))
CLIP_DOWNLOAD_WORKERS = max(4, int(os.getenv("CLIP_DOWNLOAD_WORKERS", "32")))
CLIP_READY_QUEUE = max(CLIP_INFER_BATCH * 2, int(os.getenv("CLIP_READY_QUEUE", "256")))
CLIP_POLL_SECONDS = max(0.05, float(os.getenv("CLIP_POLL_SECONDS", "0.10")))
CLIP_TARGET_RATE = max(1.0, float(os.getenv("CLIP_TARGET_RATE", "10.0")))

app = FastAPI(title="Instagram Profile Gallery + CLIP", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.instagram.com", "https://instagram.com"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "web", "static")), name="static")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")
CLAIM_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
READY_QUEUE = queue.Queue(maxsize=CLIP_READY_QUEUE)
CLIP_PROCESSING = 0
CLIP_LAST_ERROR = ""
CLIP_RATE_EVENTS = []
CLIP_DOWNLOADED = 0
CLIP_DOWNLOAD_FAILED = 0


class ProfileBatch(BaseModel):
    source_url: str | None = Field(default=None, max_length=2000)
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


def require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(
            503,
            "Supabase is not configured on Render. Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.",
        )


def supa(method: str, path: str, *, params=None, payload=None, prefer=None):
    require_supabase()
    headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }
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
            timeout=45,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Could not reach Supabase: {exc}") from exc

    if not r.ok:
        raise HTTPException(r.status_code, f"Supabase {r.status_code}: {r.text[:600]}")

    if not r.text:
        return []
    try:
        return r.json()
    except ValueError as exc:
        raise HTTPException(502, f"Supabase returned invalid JSON: {r.text[:300]}") from exc


def clean_username(value: Any) -> str:
    username = str(value or "").strip().lstrip("@").lower()
    return username if USERNAME_RE.fullmatch(username) else ""


HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://www.instagram.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})
HTTP.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=max(32, CLIP_DOWNLOAD_WORKERS),
    pool_maxsize=max(64, CLIP_DOWNLOAD_WORKERS * 2),
    max_retries=0,
))


def fetch_avatar_bytes(url: str) -> bytes:
    if not url:
        raise ValueError("missing image_url")

    last = None

    for attempt in range(3):
        try:
            r = HTTP.get(
                url,
                timeout=(5, 12),
                allow_redirects=True,
            )

            if r.status_code in (408, 425, 429, 500, 502, 503, 504):
                last = RuntimeError(f"avatar HTTP {r.status_code}")
                if attempt < 2:
                    time.sleep(0.12 * (2 ** attempt))
                    continue

            r.raise_for_status()

            content_type = r.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise ValueError("avatar URL did not return an image")

            if len(r.content) > 4_000_000:
                raise ValueError("avatar image too large")

            return r.content

        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.12 * (2 ** attempt))

    raise last or RuntimeError("avatar download failed")


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


def download_one(row):
    global CLIP_DOWNLOADED, CLIP_DOWNLOAD_FAILED

    try:
        data = fetch_avatar_bytes(row.get("image_url") or "")
        with STATE_LOCK:
            CLIP_DOWNLOADED += 1
        return row, data, None
    except Exception as exc:
        with STATE_LOCK:
            CLIP_DOWNLOAD_FAILED += 1
        return row, None, str(exc)[:260]


def claim_and_download_loop():
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=CLIP_DOWNLOAD_WORKERS,
        thread_name_prefix="avatar-download",
    )

    while True:
        rows = []

        try:
            # Apply backpressure: don't keep claiming if inference is behind.
            if READY_QUEUE.qsize() >= max(CLIP_INFER_BATCH * 2, CLIP_READY_QUEUE - CLIP_INFER_BATCH):
                time.sleep(0.03)
                continue

            with CLAIM_LOCK:
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

            futures = [
                pool.submit(download_one, row)
                for row in rows
            ]

            for future in concurrent.futures.as_completed(futures):
                row, data, error = future.result()

                if error or data is None:
                    clip_mark_many(
                        [row["username"]],
                        clip_index_status="failed",
                        clip_error=error or "avatar download failed",
                    )
                    continue

                # Blocks when inference is behind: natural backpressure.
                READY_QUEUE.put((row, data))

        except Exception as exc:
            global CLIP_LAST_ERROR
            CLIP_LAST_ERROR = f"download stage: {str(exc)[:280]}"
            if rows:
                try:
                    clip_mark_many(
                        [row["username"] for row in rows],
                        clip_index_status="queued",
                        clip_error=CLIP_LAST_ERROR,
                    )
                except Exception:
                    pass
            time.sleep(0.5)


def bulk_write_embeddings(rows, vectors):
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


def infer_loop():
    global CLIP_PROCESSING, CLIP_LAST_ERROR, CLIP_RATE_EVENTS

    from clip_engine import image_embeddings

    while True:
        batch_rows = []
        batch_data = []

        try:
            # Wait for one item, then fill the rest of the batch quickly.
            row, data = READY_QUEUE.get(timeout=1.0)
            batch_rows.append(row)
            batch_data.append(data)

            deadline = time.perf_counter() + 0.025

            while len(batch_rows) < CLIP_INFER_BATCH:
                timeout = max(0.0, deadline - time.perf_counter())

                if timeout <= 0:
                    break

                try:
                    row, data = READY_QUEUE.get(timeout=timeout)
                    batch_rows.append(row)
                    batch_data.append(data)
                except queue.Empty:
                    break

            with STATE_LOCK:
                CLIP_PROCESSING = len(batch_rows)

            started = time.perf_counter()

            vectors = image_embeddings(batch_data)

            bulk_write_embeddings(batch_rows, vectors)

            completed_at = time.time()

            with STATE_LOCK:
                CLIP_RATE_EVENTS.extend([completed_at] * len(batch_rows))
                cutoff = completed_at - 60
                CLIP_RATE_EVENTS = [t for t in CLIP_RATE_EVENTS if t >= cutoff]

            elapsed = max(time.perf_counter() - started, 1e-6)

            # If inference itself is slow, keep full batches flowing;
            # the queue/downloader stages will remain overlapped.
            if len(batch_rows) / elapsed < CLIP_TARGET_RATE and READY_QUEUE.qsize() < CLIP_INFER_BATCH:
                time.sleep(0)

        except queue.Empty:
            continue

        except Exception as exc:
            CLIP_LAST_ERROR = f"inference stage: {str(exc)[:280]}"

            if batch_rows:
                try:
                    clip_mark_many(
                        [row["username"] for row in batch_rows],
                        clip_index_status="queued",
                        clip_error=CLIP_LAST_ERROR,
                    )
                except Exception:
                    pass

            time.sleep(0.25)

        finally:
            with STATE_LOCK:
                CLIP_PROCESSING = 0


def clip_rate():
    with STATE_LOCK:
        now = time.time()
        cutoff = now - 60
        recent = [t for t in CLIP_RATE_EVENTS if t >= cutoff]

    if len(recent) < 2:
        return 0.0

    span = max(now - recent[0], 1.0)
    return len(recent) / span


def recover_indexing_rows():
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


@app.on_event("startup")
def startup():
    recover_indexing_rows()

    threading.Thread(
        target=claim_and_download_loop,
        name="clip-download-pipeline",
        daemon=True,
    ).start()

    threading.Thread(
        target=infer_loop,
        name="clip-inference-pipeline",
        daemon=True,
    ).start()


@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE, "web", "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "2.0.0",
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY),
        "table": TABLE,
        "clip_download_workers": CLIP_DOWNLOAD_WORKERS,
        "clip_infer_batch": CLIP_INFER_BATCH,
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
            "profile_url": str(
                raw.get("profile_url") or f"https://www.instagram.com/{username}/"
            )[:1000],
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
            payload=rows[i:i + 250],
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
            params={
                "select": "username,image_url",
                "limit": "1000",
                "offset": str(offset),
            },
        )

        if not rows:
            break

        total += len(rows)
        with_photos += sum(1 for row in rows if row.get("image_url"))

        if len(rows) < 1000:
            break

        offset += 1000

    return {
        "total": total,
        "with_photos": with_photos,
        "without_photos": max(total - with_photos, 0),
    }


@app.get("/api/avatar")
def avatar(url: str = Query(..., min_length=8, max_length=5000)):
    if not (
        url.startswith("https://")
        and any(host in url.lower() for host in ("cdninstagram", "fbcdn", "scontent"))
    ):
        raise HTTPException(400, "Unsupported avatar URL")

    try:
        data = fetch_avatar_bytes(url)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 502
        raise HTTPException(code, f"Avatar CDN returned {code}") from exc
    except Exception as exc:
        raise HTTPException(502, f"Avatar fetch failed: {exc}") from exc

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


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

    with STATE_LOCK:
        processing = CLIP_PROCESSING
        last_error = CLIP_LAST_ERROR
        ready = READY_QUEUE.qsize()
        downloaded = CLIP_DOWNLOADED
        download_failed = CLIP_DOWNLOAD_FAILED

    return {
        "total": total,
        "indexed": indexed,
        "queued": queued,
        "failed": failed,
        "processing": processing,
        "download_workers": CLIP_DOWNLOAD_WORKERS,
        "infer_batch": CLIP_INFER_BATCH,
        "ready_queue": ready,
        "downloaded": downloaded,
        "download_failed": download_failed,
        "images_per_second": round(clip_rate(), 2),
        "last_error": last_error,
    }


@app.post("/api/clip/reindex")
def clip_reindex():
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


@app.get("/api/clip/search/text")
def clip_search_text(
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(default=10000, ge=1, le=10000),
):
    from clip_engine import text_embedding

    query = np.asarray(text_embedding(q.strip()), dtype=np.float32)
    qnorm = float(np.linalg.norm(query))
    if qnorm <= 0:
        raise HTTPException(500, "CLIP produced an empty text embedding")
    query = query / qnorm

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

            denom = float(np.linalg.norm(vector))
            if denom <= 0:
                continue

            score = float(np.dot(query, vector / denom))
            row["score"] = score
            scored.append(row)

        if len(rows) < page:
            break

        offset += page

    scored.sort(key=lambda x: x["score"], reverse=True)

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
