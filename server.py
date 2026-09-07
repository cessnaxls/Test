from __future__ import annotations

import os
import re
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = os.path.dirname(os.path.abspath(__file__))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TABLE = os.getenv("PROFILE_TABLE", "scraped_profiles_v2")

app = FastAPI(title="Instagram Profile Viewer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.instagram.com", "https://instagram.com"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "web", "static")), name="static")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")

class BatchIn(BaseModel):
    source_url: str | None = Field(default=None, max_length=2000)
    records: list[dict[str, Any]] = Field(default_factory=list, max_length=500)

def require_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Supabase is not configured on Render.")

def supa(method: str, path: str, *, params=None, payload=None, prefer=None):
    require_supabase()
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    r = requests.request(method, f"{SUPABASE_URL}/rest/v1/{path}", params=params, json=payload, headers=headers, timeout=30)
    if not r.ok:
        raise HTTPException(r.status_code, f"Supabase error: {r.text[:500]}")
    return r.json() if r.text else []

def clean_username(value: Any) -> str:
    u = str(value or "").strip().lstrip("@").lower()
    return u if USERNAME_RE.fullmatch(u) else ""

@app.get("/", response_class=HTMLResponse)
def home():
    with open(os.path.join(BASE, "web", "index.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.get("/health")
def health():
    return {"ok": True, "version": "1.0.0", "supabase_configured": bool(SUPABASE_URL and SUPABASE_KEY), "table": TABLE}

@app.post("/api/profiles/batch")
def ingest_batch(req: BatchIn):
    unique: dict[str, dict[str, Any]] = {}
    for raw in req.records:
        username = clean_username(raw.get("username"))
        if not username:
            continue
        rec = {
            "username": username,
            "full_name": str(raw.get("full_name") or "")[:300],
            "profile_url": str(raw.get("profile_url") or f"https://www.instagram.com/{username}/")[:1000],
            "image_url": str(raw.get("image_url") or "")[:5000],
            "source_url": str(raw.get("source_url") or req.source_url or "")[:2000],
        }
        old = unique.get(username)
        if old is None or (not old["image_url"] and rec["image_url"]):
            unique[username] = rec
    rows = list(unique.values())
    if not rows:
        return {"received": len(req.records), "accepted": 0}
    for i in range(0, len(rows), 200):
        supa("POST", f"{TABLE}?on_conflict=username", payload=rows[i:i+200], prefer="resolution=merge-duplicates,return=minimal")
    return {"received": len(req.records), "accepted": len(rows)}

@app.get("/api/profiles")
def profiles(limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0)):
    rows = supa("GET", TABLE, params={
        "select": "username,full_name,profile_url,image_url,source_url,first_seen,last_seen,seen_count",
        "order": "last_seen.desc",
        "limit": str(limit),
        "offset": str(offset),
    })
    return {"profiles": rows, "limit": limit, "offset": offset}

@app.get("/api/stats")
def stats():
    total = 0
    offset = 0
    while True:
        rows = supa("GET", TABLE, params={"select": "username", "limit": "1000", "offset": str(offset)})
        total += len(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return {"total": total}

@app.delete("/api/profiles")
def clear_profiles():
    supa("DELETE", TABLE, params={"username": "neq.__never_a_real_instagram_username__"}, prefer="return=minimal")
    return {"ok": True}
