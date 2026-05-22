from __future__ import annotations
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="主力雷達")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── TTL Cache (in-memory) ──────────────────────
_cache: dict[str, tuple[Any, float]] = {}

_TTL_MAP: dict[str, int] = {
    "exchangeInfo": 3600,
    "ticker/24hr": 30,
    "interval=1d": 300,
    "interval=4h": 300,
    "interval=1h": 120,
    "openInterestHist": 120,
    "premiumIndex": 300,
    "globalLongShortAccountRatio": 120,
    "alternative.me": 1800,
}

def _ttl(url: str) -> int:
    for k, v in _TTL_MAP.items():
        if k in url:
            return v
    return 60

def _cache_get(url: str) -> Any | None:
    entry = _cache.get(url)
    if entry and time.monotonic() - entry[1] < _ttl(url):
        return entry[0]
    return None

def _cache_set(url: str, data: Any) -> None:
    _cache[url] = (data, time.monotonic())

# ── Proxy fetch ───────────────────────────────
async def _fetch(url: str) -> Any:
    cached = _cache_get(url)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers={"User-Agent": "SmartMoneyRadar/3.0"})
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail="上游 API 錯誤")
    data = r.json()
    _cache_set(url, data)
    return data

# ── Proxy Routes ──────────────────────────────
@app.get("/proxy/fapi/{path:path}")
async def proxy_fapi(path: str, request: Request):
    qs = request.url.query
    return await _fetch(f"https://fapi.binance.com/{path}" + (f"?{qs}" if qs else ""))

@app.get("/proxy/sapi/{path:path}")
async def proxy_sapi(path: str, request: Request):
    qs = request.url.query
    return await _fetch(f"https://api.binance.com/{path}" + (f"?{qs}" if qs else ""))

@app.get("/proxy/fng")
async def proxy_fng(request: Request):
    qs = request.url.query
    return await _fetch("https://api.alternative.me/fng/" + (f"?{qs}" if qs else ""))

@app.get("/health")
async def health():
    return {"status": "ok", "cached_keys": len(_cache)}

# ── Serve frontend (must be last) ─────────────
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
