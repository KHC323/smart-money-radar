from __future__ import annotations
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

app = FastAPI()
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


# ── Daily News ────────────────────────────────
_TW_TZ = timezone(timedelta(hours=8))
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


async def _get_safe(client: httpx.AsyncClient, url: str, ua: str = "SmartMoneyRadar/3.0") -> httpx.Response | None:
    try:
        r = await client.get(url, timeout=12, headers={"User-Agent": ua})
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


async def _section_crypto(client: httpx.AsyncClient) -> str:
    lines = ["📈 *加密貨幣*"]
    syms = '["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"]'
    r = await _get_safe(client, f"https://api.binance.com/api/v3/ticker/24hr?symbols={syms}")
    if r:
        for item in r.json():
            sym = item["symbol"].replace("USDT", "")
            price = float(item["lastPrice"])
            chg = float(item["priceChangePercent"])
            arrow = "▲" if chg >= 0 else "▼"
            price_str = f"{price:,.0f}" if price >= 100 else f"{price:,.4f}"
            lines.append(f"  {arrow} {sym}: ${price_str} ({chg:+.2f}%)")
    else:
        lines.append("  資料暫時無法取得")

    fng_r = await _get_safe(client, "https://api.alternative.me/fng/?limit=1")
    if fng_r:
        fng = fng_r.json()["data"][0]
        label_map = {
            "Extreme Fear": "極度恐懼 😱",
            "Fear": "恐懼 😰",
            "Neutral": "中性 😐",
            "Greed": "貪婪 😄",
            "Extreme Greed": "極度貪婪 🤑",
        }
        label_zh = label_map.get(fng["value_classification"], fng["value_classification"])
        lines.append(f"  恐懼貪婪指數: {fng['value']} | {label_zh}")

    return "\n".join(lines)


async def _yf_quote(client: httpx.AsyncClient, symbol: str) -> tuple[float, float] | None:
    encoded = symbol.replace("^", "%5E")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=2d"
    r = await _get_safe(client, url, ua=_BROWSER_UA)
    if not r:
        return None
    try:
        meta = r.json()["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or meta.get("previousClose") or 0)
        prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
        if price and prev:
            return price, (price - prev) / prev * 100
    except Exception:
        pass
    return None


async def _section_us_stocks(client: httpx.AsyncClient) -> str:
    indices = [("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ"), ("^DJI", "道瓊")]
    lines = ["🇺🇸 *美股指數*"]
    for sym, name in indices:
        result = await _yf_quote(client, sym)
        if result:
            price, chg_pct = result
            arrow = "▲" if chg_pct >= 0 else "▼"
            lines.append(f"  {arrow} {name}: {price:,.2f} ({chg_pct:+.2f}%)")
        else:
            lines.append(f"  ❌ {name}: 無法取得")
    return "\n".join(lines)


async def _section_tw_stocks(client: httpx.AsyncClient) -> str:
    lines = ["🇹🇼 *台股大盤*"]
    result = await _yf_quote(client, "^TWII")
    if result:
        price, chg_pct = result
        arrow = "▲" if chg_pct >= 0 else "▼"
        lines.append(f"  {arrow} 加權指數: {price:,.2f} ({chg_pct:+.2f}%)")
    else:
        lines.append("  資料暫時無法取得")
    return "\n".join(lines)


async def _section_news(client: httpx.AsyncClient) -> str:
    lines = ["📰 *重要財經新聞*"]
    items: list[str] = []
    feeds = [
        "https://news.cnyes.com/rss/id/wd_tse",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    ]
    for url in feeds:
        if len(items) >= 5:
            break
        r = await _get_safe(client, url, ua=_BROWSER_UA)
        if not r:
            continue
        try:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item")[:4]:
                el = item.find("title")
                if el is not None and el.text:
                    title = el.text.strip().replace("<![CDATA[", "").replace("]]>", "").strip()
                    if len(title) > 8:
                        items.append(f"• {title[:90]}")
        except Exception:
            continue

    lines.extend(items[:5] if items else ["• 今日新聞暫時無法取得"])
    return "\n".join(lines)


@app.get("/api/daily-news")
async def send_daily_news(request: Request):
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {cron_secret}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未設定")

    now = datetime.now(_TW_TZ)
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    date_str = f"{now.strftime('%Y/%m/%d')} ({weekdays[now.weekday()]})"

    async with httpx.AsyncClient(timeout=20) as client:
        results = await asyncio.gather(
            _section_crypto(client),
            _section_us_stocks(client),
            _section_tw_stocks(client),
            _section_news(client),
            return_exceptions=True,
        )

    fallbacks = [
        "📈 *加密貨幣*\n  資料獲取失敗",
        "🇺🇸 *美股指數*\n  資料獲取失敗",
        "🇹🇼 *台股大盤*\n  資料獲取失敗",
        "📰 *重要財經新聞*\n• 新聞獲取失敗",
    ]
    crypto, us_stocks, tw_stocks, news = [
        str(r) if not isinstance(r, Exception) else fallbacks[i]
        for i, r in enumerate(results)
    ]

    message = (
        f"📊 *每日市場摘要* | {date_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{crypto}\n\n"
        f"{us_stocks}\n\n"
        f"{tw_stocks}\n\n"
        f"{news}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_由主力雷達自動發送_"
    )

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Telegram API 錯誤: {r.text}")

    return {"status": "sent", "timestamp": now.isoformat()}
