from __future__ import annotations

import json
import os
import re
import threading
import time
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

CLIP_INDEX_WORKERS = max(1, int(os.getenv("CLIP_INDEX_WORKERS", "2")))
CLIP_BATCH_SIZE = max(1, int(os.getenv("CLIP_BATCH_SIZE", "12")))
CLIP_DOWNLOAD_WORKERS = max(1, int(os.getenv("CLIP_DOWNLOAD_WORKERS", "8")))
CLIP_POLL_SECONDS = max(0.1, float(os.getenv("CLIP_POLL_SECONDS", "0.5")))

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
CLIP_PROCESSING = 0
CLIP_LAST_ERROR = ""


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


def fetch_avatar_bytes(url: str) -> bytes:
    if not url:
        raise ValueError("missing image_url")
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://www.instagram.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/"):
        raise ValueError("avatar URL did not return an image")
    if len(r.content) > 8_000_000:
        raise ValueError("avatar image too large")
    return r.content


def clip_mark(username: str, **fields):
    supa(
        "PATCH",
        TABLE,
        params={"username": f"eq.{username}"},
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


def load_row_avatar(row):
    try:
        return row, fetch_avatar_bytes(row.get("image_url") or ""), None
    except Exception as exc:
        return row, None, str(exc)[:280]


def clip_worker(worker_id: int):
    global CLIP_PROCESSING, CLIP_LAST_ERROR
    from clip_engine import image_embeddings

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=CLIP_DOWNLOAD_WORKERS)

    while True:
        rows = []
        try:
            with CLAIM_LOCK:
                rows = clip_pending(CLIP_BATCH_SIZE)
                for row in rows:
                    clip_mark(
                        row["username"],
                        clip_index_status="indexing",
                        clip_error=None,
                    )

            if not rows:
                time.sleep(CLIP_POLL_SECONDS)
                continue

            with STATE_LOCK:
                CLIP_PROCESSING += len(rows)

            loaded = list(pool.map(load_row_avatar, rows))
            good_rows = []
            image_bytes = []

            for row, data, error in loaded:
                if error or data is None:
                    clip_mark(
                        row["username"],
                        clip_index_status="failed",
                        clip_error=error or "avatar fetch failed",
                    )
                else:
                    good_rows.append(row)
                    image_bytes.append(data)

            if not good_rows:
                continue

            try:
                vectors = image_embeddings(image_bytes)
            except Exception as exc:
                message = f"CLIP worker {worker_id}: {str(exc)[:240]}"
                CLIP_LAST_ERROR = message
                for row in good_rows:
                    clip_mark(
                        row["username"],
                        clip_index_status="failed",
                        clip_error=message,
                    )
                continue

            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            for row, vector in zip(good_rows, vectors):
                clip_mark(
                    row["username"],
                    clip_embedding=json.dumps(vector, separators=(",", ":")),
                    clip_index_status="indexed",
                    clip_error=None,
                    clip_indexed_at=now,
                )

        except Exception as exc:
            CLIP_LAST_ERROR = f"worker {worker_id}: {str(exc)[:280]}"
            time.sleep(1)
        finally:
            if rows:
                with STATE_LOCK:
                    CLIP_PROCESSING = max(0, CLIP_PROCESSING - len(rows))


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
    for i in range(CLIP_INDEX_WORKERS):
        threading.Thread(
            target=clip_worker,
            args=(i + 1,),
            name=f"clip-worker-{i+1}",
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
        "clip_workers": CLIP_INDEX_WORKERS,
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

    return {
        "total": total,
        "indexed": indexed,
        "queued": queued,
        "failed": failed,
        "processing": processing,
        "workers": CLIP_INDEX_WORKERS,
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
