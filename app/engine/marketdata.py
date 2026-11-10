"""Market overview for the home page + coin and exchange icons.

Sources (all free, no key needed):
  Fear & Greed ............ alternative.me (attribution shown next to it); CoinMarketCap keyless API as fallback
  Altcoin Season Index .... CoinMarketCap keyless public API
  Total market cap, dominance, coin & exchange icons ... CoinGecko (paced client, see coingecko.py)
  BTC/ETH price, funding rate, long/short ratio ........ Binance USDⓈ-M futures public market data
Icons are downloaded once and served from this server (fast, private, no hot-linking).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from ..db import SessionLocal, get_global, utcnow
from ..settings import DATA_DIR
from . import coingecko

log = logging.getLogger("relay.marketdata")
ICON_DIR = DATA_DIR / "icons"
MAP_FILE = ICON_DIR / "map.json"
FAPI = "https://fapi.binance.com"
CMC = "https://pro-api.coinmarketcap.com/public-api"
# our exchange ids -> CoinGecko exchange ids
CG_EXCHANGES = {"binance": "binance", "bybit": "bybit_spot", "okx": "okex", "bitget": "bitget", "kucoin": "kucoin",
                "gate": "gate", "mexc": "mxc", "htx": "huobi", "bingx": "bingx"}
WIDGET_MINUTES = 30


# ------------------------------------------------------------------ icons
def _load_map() -> dict:
    try:
        return json.loads(MAP_FILE.read_text())
    except (OSError, ValueError):
        return {"coins": {}, "exchanges": {}}


async def refresh_icon_map(pages: int = 4) -> dict:
    """Symbol -> image URL for the top ~1000 coins (largest market cap wins a symbol), and exchange logos."""
    coins: dict[str, str] = {}
    for page in range(1, pages + 1):
        rows = await coingecko.get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                                      "per_page": 250, "page": page})
        for c in rows or []:
            sym, img = str(c.get("symbol", "")).upper(), c.get("image")
            if sym and isinstance(img, str) and sym not in coins:
                coins[sym] = img.replace("/large/", "/small/")
    exchanges: dict[str, str] = {}
    rows = await coingecko.get("/exchanges", {"per_page": 250})
    by_id = {e.get("id"): e.get("image") for e in rows or [] if isinstance(e, dict)}
    for ours, cg_id in CG_EXCHANGES.items():
        if by_id.get(cg_id):
            exchanges[ours] = by_id[cg_id]
    data = {"coins": coins, "exchanges": exchanges, "at": utcnow().isoformat(timespec="seconds")}
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(json.dumps(data))
    return data


def _safe(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())[:20]


async def icon_file(kind: str, name: str) -> Path | None:
    """Local copy of a coin/exchange icon (downloaded on first use). None = unknown -> letter badge."""
    key = _safe(name) if kind == "coin" else re.sub(r"[^a-z0-9]", "", name.lower())[:20]
    if not key:
        return None
    folder = ICON_DIR / kind
    for ext in ("png", "jpg", "webp", "svg"):
        p = folder / f"{key}.{ext}"
        if p.exists():
            return p
    if (folder / f"{key}.none").exists():
        return None
    url = (_load_map().get("coins" if kind == "coin" else "exchanges") or {}).get(key if kind == "coin" else key)
    folder.mkdir(parents=True, exist_ok=True)
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url)
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) > 300_000:
            raise ValueError(f"{r.status_code} {ctype}")
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/svg+xml": "svg"}.get(ctype.split(";")[0], "png")
        p = folder / f"{key}.{ext}"
        p.write_bytes(r.content)
        return p
    except Exception as e:
        log.info("icon %s/%s not available: %s", kind, key, e)
        (folder / f"{key}.none").write_text("")  # don't retry on every page view (cleared by the daily refresh)
        return None


def letter_badge(name: str) -> str:
    """Fallback icon: the first letters on a colour taken from the name."""
    text = (_safe(name) or "?")[:3]
    hue = sum(ord(ch) for ch in text) * 37 % 360
    size = 15 if len(text) > 2 else 18
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><circle cx="20" cy="20" r="20" '
            f'fill="hsl({hue},55%,45%)"/><text x="20" y="26" text-anchor="middle" font-family="Arial,sans-serif" '
            f'font-size="{size}" font-weight="700" fill="#fff">{text}</text></svg>')


# ------------------------------------------------------------------ widgets
async def _json(url: str, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=15, headers={"accept": "application/json"}) as c:
        r = await c.get(url, params=params or {})
        r.raise_for_status()
        return r.json()


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def fear_greed() -> dict:
    try:
        d = (await _json("https://api.alternative.me/fng/", {"limit": 2})).get("data") or []
        now, prev = d[0], (d[1] if len(d) > 1 else {})
        return {"value": int(now["value"]), "label": now.get("value_classification", ""),
                "prev": int(prev["value"]) if prev.get("value") else None, "source": "alternative.me"}
    except Exception as e:
        first_error = e
    d = (await _json(CMC + "/v3/fear-and-greed/latest")).get("data") or {}
    d = d[0] if isinstance(d, list) and d else d
    v = _num(d.get("value"))
    if v is None:
        raise first_error
    return {"value": int(v), "label": d.get("value_classification", ""), "prev": None, "source": "CoinMarketCap"}


def _find_index(obj: Any) -> float | None:
    """The 0-100 number in CoinMarketCap's altcoin-season answer (field names vary between versions)."""
    if isinstance(obj, dict):
        for k in ("altcoin_index", "altcoin_season_index", "altcoinIndex", "index", "value"):
            v = _num(obj.get(k))
            if v is not None and 0 <= v <= 100:
                return v
        for k in ("data", "quote", "points", "latest"):
            if k in obj:
                v = _find_index(obj[k])
                if v is not None:
                    return v
    if isinstance(obj, list) and obj:
        return _find_index(obj[-1] if isinstance(obj[-1], dict) else obj[0])
    return None


async def altcoin_season() -> dict:
    v = _find_index(await _json(CMC + "/v1/altcoin-season-index/latest"))
    if v is None:
        raise ValueError("no index in the answer")
    return {"value": int(round(v)), "source": "CoinMarketCap"}


async def global_market() -> dict:
    d = (await coingecko.get("/global")).get("data") or {}
    pct = d.get("market_cap_percentage") or {}
    return {"total_usd": _num((d.get("total_market_cap") or {}).get("usd")),
            "change_24h": _num(d.get("market_cap_change_percentage_24h_usd")),
            "volume_usd": _num((d.get("total_volume") or {}).get("usd")),
            "btc_dom": _num(pct.get("btc")), "eth_dom": _num(pct.get("eth")), "source": "CoinGecko"}


async def futures_stats() -> dict:
    """BTC/ETH price + 24h change, funding rate and BTC long/short account ratio (Binance, weight ~1 each)."""
    out: dict[str, Any] = {"source": "Binance"}
    for sym in ("BTCUSDT", "ETHUSDT"):
        t = await _json(FAPI + "/fapi/v1/ticker/24hr", {"symbol": sym})
        p = await _json(FAPI + "/fapi/v1/premiumIndex", {"symbol": sym})
        out[sym[:3]] = {"price": _num(t.get("lastPrice")), "change": _num(t.get("priceChangePercent")),
                        "funding": (_num(p.get("lastFundingRate")) or 0) * 100}
    ls = await _json(FAPI + "/futures/data/globalLongShortAccountRatio", {"symbol": "BTCUSDT", "period": "1h", "limit": 1})
    if ls:
        out["long_pct"] = (_num(ls[-1].get("longAccount")) or 0) * 100
        out["ls_ratio"] = _num(ls[-1].get("longShortRatio"))
    return out


async def refresh_widgets() -> dict:
    with SessionLocal() as s:
        data = dict(get_global(s).market_widgets or {})
    errors = {}
    for name, fn in (("fng", fear_greed), ("alt", altcoin_season), ("global", global_market), ("fut", futures_stats)):
        try:
            data[name] = await fn()
        except Exception as e:  # keep the last good value
            errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
    data["errors"], data["at"] = errors, utcnow().isoformat(timespec="seconds")
    with SessionLocal() as s:
        get_global(s).market_widgets = data
        s.commit()
    return data


def widgets() -> dict:
    with SessionLocal() as s:
        return dict(get_global(s).market_widgets or {})


async def loop() -> None:
    await asyncio.sleep(40)
    last_icons = None
    while True:
        try:
            await refresh_widgets()
            if last_icons is None or (utcnow() - last_icons).total_seconds() > 86400:
                await refresh_icon_map()
                for p in ICON_DIR.glob("*/*.none"):  # give unknown icons another chance once a day
                    p.unlink(missing_ok=True)
                last_icons = utcnow()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("market data refresh failed")
        await asyncio.sleep(WIDGET_MINUTES * 60)


def view() -> dict:
    """What the home page needs: the data, the gauge needle end point and the Fear & Greed band."""
    import math
    mw = widgets()
    fng = (mw.get("fng") or {}).get("value")
    gauge, band = (60, 10), "neutral"
    if fng is not None:
        ang = math.pi * (1 - max(0, min(100, fng)) / 100)
        gauge = (round(60 + 44 * math.cos(ang), 1), round(60 - 44 * math.sin(ang), 1))
        band = ("extreme_fear" if fng < 25 else "fear" if fng < 45 else "neutral" if fng <= 55
                else "greed" if fng <= 75 else "extreme_greed")
    return {"mw": mw, "gauge": gauge, "fng_band": band}
