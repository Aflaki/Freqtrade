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
import time
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
CMC_DATA = "https://api.coinmarketcap.com/data-api"  # what coinmarketcap.com's own pages read (no key)
HIST_EVERY = 6 * 3600   # daily history for the charts: a few times a day is plenty
HIST_DAYS = 365
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


async def _cmc_chart(path: str, days: int) -> dict:
    now = int(time.time())
    return (await _json(CMC_DATA + path, {"start": now - days * 86400, "end": now})).get("data") or {}


def _score(x: Any, key: str) -> int | None:
    v = _num((x or {}).get(key))
    return int(round(v)) if v is not None else None


async def fear_greed() -> dict:
    """CoinMarketCap's own index first (the one on coinmarketcap.com), then alternative.me's (a different index)."""
    try:
        d = await _cmc_chart("/v3/fear-greed/chart", 8)
        hv = d.get("historicalValues") or {}
        now = hv.get("now") or (d.get("dataList") or [{}])[-1]
        v = _score(now, "score")
        if v is not None:
            return {"value": v, "label": now.get("name", ""), "prev": _score(hv.get("yesterday"), "score"),
                    "week": _score(hv.get("lastWeek"), "score"), "month": _score(hv.get("lastMonth"), "score"),
                    "source": "CoinMarketCap"}
    except Exception as e:
        log.info("CoinMarketCap fear & greed: %s", e)
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
    try:
        d = await _cmc_chart("/v3/altcoin-season/chart", 8)
        hv = d.get("historicalValues") or {}
        v = _score(hv.get("now") or (d.get("points") or [{}])[-1], "altcoinIndex")
        if v is not None:
            return {"value": v, "prev": _score(hv.get("yesterday"), "altcoinIndex"),
                    "month": _score(hv.get("lastMonth"), "altcoinIndex"), "source": "CoinMarketCap"}
    except Exception as e:
        log.info("CoinMarketCap altcoin season: %s", e)
    v = _find_index(await _json(CMC + "/v1/altcoin-season-index/latest"))
    if v is None:
        raise ValueError("no index in the answer")
    return {"value": int(round(v)), "source": "CoinMarketCap"}


async def global_market() -> dict:
    """CoinMarketCap's totals first (they count a different set of coins than CoinGecko), CoinGecko as backup."""
    try:
        d = (await _json(CMC_DATA + "/v3/global-metrics/quotes/latest")).get("data") or {}
        q = (d.get("quotes") or [{}])[0]
        total = _num(q.get("totalMarketCap"))
        if total:
            return {"total_usd": total, "change_24h": _num(q.get("totalMarketCapYesterdayPercentageChange")),
                    "volume_usd": _num(q.get("totalVolume24H")), "btc_dom": _num(d.get("btcDominance")),
                    "eth_dom": _num(d.get("ethDominance")), "source": "CoinMarketCap"}
    except Exception as e:
        log.info("CoinMarketCap global metrics: %s", e)
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


def _ts(v: Any) -> int | None:
    if v is None:
        return None
    if str(v).isdigit():
        return int(v)
    try:
        import datetime as _dt
        return int(_dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


async def market_history(days: int = HIST_DAYS) -> dict[str, list]:
    """Daily series for the charts, from CoinMarketCap: total market cap, volume, BTC dominance, Fear & Greed,
    Altcoin season. {"mcap": [[unix, value], ...], ...}; a series that fails is left out."""
    out: dict[str, list] = {}
    try:
        now = int(time.time())
        d = (await _json(CMC_DATA + "/v3/global-metrics/quotes/historical",
                         {"format": "chart_crypto_details", "interval": "1d", "timeStart": now - days * 86400,
                          "timeEnd": now})).get("data") or {}
        mcap, vol, btc = [], [], []
        for q in d.get("quotes") or []:
            ts = _ts(q.get("timestamp"))
            usd = (q.get("quote") or [{}])[0]
            if ts is None:
                continue
            if _num(usd.get("totalMarketCap")):
                mcap.append([ts, _num(usd["totalMarketCap"])])
            if _num(usd.get("totalVolume24H")):
                vol.append([ts, _num(usd["totalVolume24H"])])
            if _num(q.get("btcDominance")) is not None:
                btc.append([ts, _num(q["btcDominance"])])
        out.update({k: v for k, v in (("mcap", mcap), ("vol", vol), ("btc_dom", btc)) if len(v) > 1})
    except Exception as e:
        log.info("CoinMarketCap market history: %s", e)
    for key, path, field, items in (("fng", "/v3/fear-greed/chart", "score", "dataList"),
                                    ("alt", "/v3/altcoin-season/chart", "altcoinIndex", "points")):
        try:
            rows = (await _cmc_chart(path, days)).get(items) or []
            pts = [[_ts(r.get("timestamp")), _num(r.get(field))] for r in rows]
            pts = [p for p in pts if p[0] is not None and p[1] is not None]
            if len(pts) > 1:
                out[key] = pts
        except Exception as e:
            log.info("CoinMarketCap %s history: %s", key, e)
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
    if time.time() - float(data.get("hist_at") or 0) > HIST_EVERY:
        hist = await market_history()
        if hist:
            data["hist"] = {**(data.get("hist") or {}), **hist}
            data["hist_at"] = time.time()
        else:
            errors["hist"] = "CoinMarketCap history unavailable"
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
    return {"mw": mw, "gauge": gauge, "fng_band": band, "mcharts": charts(mw.get("hist") or {}, mw)}


CHART_RANGES = (30, 90, 365)
# key, series in the stored history, divisor, tick format, fixed 0..100 scale, colour
CHART_METRICS = (("mcap", "mcap", 1e9, "${:,.0f}B", False, "var(--chart)"),
                 ("vol", "vol", 1e9, "${:,.0f}B", False, "#a78bfa"),
                 ("btc_dom", "btc_dom", 1, "{:.0f}%", False, "#f7931a"),
                 ("fng", "fng", 1, "{:.0f}", True, "#f59e0b"),
                 ("alt", "alt", 1, "{:.0f}", True, "#22c55e"))


def _with_now(hist: dict, mw: dict) -> dict:
    """The daily history ends at yesterday's close: add today's live value so the chart ends where the cards are."""
    g, now = mw.get("global") or {}, time.time()
    live = {"mcap": g.get("total_usd"), "vol": g.get("volume_usd"), "btc_dom": g.get("btc_dom"),
            "fng": (mw.get("fng") or {}).get("value"), "alt": (mw.get("alt") or {}).get("value")}
    if (mw.get("global") or {}).get("source") != "CoinMarketCap":
        live.update(mcap=None, vol=None, btc_dom=None)  # other sources count differently: don't mix them in
    out = dict(hist)
    for k, v in live.items():
        pts = list(out.get(k) or [])
        if v is not None and pts and now - pts[-1][0] > 600:
            out[k] = pts + [[int(now), float(v)]]
    return out


def charts(hist: dict, mw: dict | None = None) -> list[dict]:
    """[{key, range, chart, last, first, change}] for every metric and range that has data."""
    import datetime as _dt

    from .performance import build_chart
    hist = _with_now(hist, mw or {})
    out = []
    now = time.time()
    for key, src, div, fmt, fixed, color in CHART_METRICS:
        pts = [(p[0], p[1] / div) for p in hist.get(src) or [] if p and p[1] is not None]
        if len(pts) < 2:
            continue
        for days in CHART_RANGES:
            sel = [p for p in pts if p[0] >= now - days * 86400 - 3600]
            if len(sel) > 200:  # a line on a page: about 200 points is plenty
                step = len(sel) / 200
                sel = [sel[int(i * step)] for i in range(200)] + [sel[-1]]
            if len(sel) < 2:
                continue
            series = [{"key": key, "name": key, "color": color, "dash": False, "on": True,
                       "points": [(_dt.datetime.fromtimestamp(t), v) for t, v in sel]}]
            c = build_chart(series, unit="", include_zero=fixed, yfmt=fmt, fixed=(0, 100) if fixed else None)
            if c:
                s0 = c["series"][0]
                bottom = c["h"] - c["B"]
                s0["area"] = f'{s0["path"]} L{c["w"] - c["R"]},{bottom} L{c["L"]},{bottom} Z'
                first, last = sel[0][1], sel[-1][1]
                out.append({"key": key, "days": days, "chart": c, "last": last, "first": first, "fmt": fmt,
                            "change": (last / first - 1) * 100 if first and not fixed else last - first})
    return out
