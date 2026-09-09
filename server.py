from __future__ import annotations

import os
import re
import io
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np

import requests
from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TABLE = os.getenv("PROFILE_TABLE", "scraped_profiles_v2")

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


def _download_avatar_bytes(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://www.instagram.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/"):
        raise RuntimeError("avatar URL did not return an image")
    if len(r.content) > 8_000_000:
        raise RuntimeError("avatar is too large")
    return r.content


def index_pending_profiles(limit: int = 100, workers: int = 16):
    limit = max(1, min(int(limit), 250))
    workers = max(1, min(int(workers), 64))
    rows = supa(
        "GET",
        TABLE,
        params={
            "select": "username,image_url",
            "image_url": "neq.",
            "clip_embedding": "is.null",
            "limit": str(limit),
            "order": "last_seen.desc",
        },
    )
    if not rows:
        return {"requested": limit, "pending": 0, "downloaded": 0, "indexed": 0, "failed": 0}

    downloaded = {}
    failures = {}
    active_workers = min(workers, max(1, len(rows)))
    with ThreadPoolExecutor(max_workers=active_workers) as pool:
        jobs = {pool.submit(_download_avatar_bytes, row["image_url"]): row["username"] for row in rows}
        for future in as_completed(jobs):
            username = jobs[future]
            try:
                downloaded[username] = future.result()
            except Exception as exc:
                failures[username] = str(exc)[:240]

    indexed_rows = []
    if downloaded:
        try:
            from clip_engine import image_embeddings
            usernames = list(downloaded)
            vectors = image_embeddings([downloaded[u] for u in usernames])
            indexed_rows = [
                {
                    "username": username,
                    "clip_embedding": json.dumps(vector, separators=(",", ":")),
                }
                for username, vector in zip(usernames, vectors)
            ]
        except Exception as exc:
            for username in downloaded:
                failures[username] = f"embedding failed: {str(exc)[:200]}"
            indexed_rows = []

    # Upsert only the index columns; existing profile metadata remains intact.
    for start in range(0, len(indexed_rows), 100):
        supa(
            "POST",
            f"{TABLE}?on_conflict=username",
            payload=indexed_rows[start:start + 100],
            prefer="resolution=merge-duplicates,return=minimal",
        )

    # clip_index_status/error are optional upgrade columns. If they exist, record
    # failed downloads; if they do not, indexing still works via clip_embedding.
    if failures:
        failed_rows = [
            {"username": u, "clip_index_status": "error", "clip_error": e}
            for u, e in failures.items()
        ]
        try:
            supa(
                "POST",
                f"{TABLE}?on_conflict=username",
                payload=failed_rows,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        except HTTPException:
            pass

    return {
        "requested": limit,
        "pending": len(rows),
        "downloaded": len(downloaded),
        "indexed": len(indexed_rows),
        "failed": len(failures),
        "workers": workers,
    }

@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE, "web", "index.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "1.1.0",
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
        "select": "username,full_name,profile_url,image_url,source_url,first_seen,last_seen,seen_count",
        "order": "last_seen.desc",
        "limit": str(limit),
        "offset": str(offset),
    }
    if photos_only:
        params["image_url"] = "neq."
    rows = supa("GET", TABLE, params=params)
    return {"profiles": rows, "limit": limit, "offset": offset}

@app.get("/api/profiles/all")
def get_all_profiles(photos_only: bool = Query(default=False)):
    rows_all = []
    offset = 0
    page = 1000
    while True:
        params = {
            "select": "username,full_name,profile_url,image_url,source_url,first_seen,last_seen,seen_count",
            "order": "last_seen.desc",
            "limit": str(page),
            "offset": str(offset),
        }
        if photos_only:
            params["image_url"] = "neq."
        rows = supa("GET", TABLE, params=params)
        rows_all.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return {"profiles": rows_all, "count": len(rows_all)}


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
            "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
        },
    )


@app.post("/api/search/image")
async def search_by_image(
    image: UploadFile = File(...),
    limit: int = Query(default=10000, ge=1, le=10000),
):
    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(400, "Upload an image file.")

    data = await image.read()
    if not data:
        raise HTTPException(400, "Image is empty.")
    if len(data) > 12_000_000:
        raise HTTPException(413, "Image is too large.")

    # Heavy CLIP import happens only when an image search is actually requested.
    try:
        from clip_engine import image_embedding
        query = np.asarray(image_embedding(data), dtype=np.float32)
    except Exception as exc:
        raise HTTPException(503, f"CLIP image search is unavailable: {str(exc)[:300]}") from exc

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
                "clip_embedding": "not.is.null",
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

    scored.sort(key=lambda row: row["score"], reverse=True)

    return {
        "indexed_searched": len(scored),
        "profiles": scored[:limit],
    }


@app.post("/api/index/pending")
def index_pending(
    limit: int = Query(default=100, ge=1, le=250),
    workers: int = Query(default=16, ge=1, le=64),
):
    return index_pending_profiles(limit, workers)


@app.get("/api/search/status")
def image_search_status():
    indexed = 0
    offset = 0

    while True:
        rows = supa(
            "GET",
            TABLE,
            params={
                "select": "username",
                "clip_embedding": "not.is.null",
                "limit": "1000",
                "offset": str(offset),
            },
        )

        indexed += len(rows)

        if len(rows) < 1000:
            break

        offset += 1000

    return {"indexed": indexed}


@app.delete("/api/profiles")
def clear_profiles():
    supa(
        "DELETE",
        TABLE,
        params={"username": "neq.__never_real__"},
        prefer="return=minimal",
    )
    return {"ok": True}
