"""Exchange rules per pair (public data, no API key): is it listed, minimum order, max leverage, price.

The minimum margin a user needs for one trade on a pair = max(min notional, min quantity x price) / leverage.
Refreshed every 6 hours for the pairs that matter (bots' whitelists + allowed pairs); kept in GlobalSettings.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from ..db import Bot, SessionLocal, canon_pair, get_global, utcnow
from . import exchange as exmod

log = logging.getLogger("relay.markets")
REFRESH_HOURS = 6


def _num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def wanted_pairs() -> set[str]:
    with SessionLocal() as s:
        gs = get_global(s)
        out = set(gs.allowed_pairs or [])
        for b in s.scalars(select(Bot).where(Bot.enabled.is_(True))):
            out |= {canon_pair(p) for p in ((b.state or {}).get("whitelist") or [])}
    return out


SHALLOW_VOLUME = 100_000  # 24h volume (in the quote coin) below which the form says "thin market"


async def load_spot(name: str, pairs: set[str]) -> dict:
    """{"BTC/EUR": {symbol, active, min_cost, min_amount, price, volume}} for the wanted coins, in every quote coin
    the site supports (the bots' BTC/USDT is traded as BTC/<portfolio quote> on spot). Also the quote coins
    themselves (EUR/USDT...) so amounts can be compared across currencies."""
    klass = getattr(exmod.ccxt, name, None)
    if klass is None:
        return {}
    bases = {p.split("/")[0] for p in pairs} | set(exmod.QUOTES)
    ex = klass({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": "spot", "fetchCurrencies": False}})
    from .ratelimit import governor, prio
    governor.attach(ex)
    tok = prio.set("background")
    try:
        markets = await ex.load_markets()
        rows: dict[str, dict] = {}
        for m in markets.values():
            if not m.get("spot") or m.get("base") not in bases or m.get("quote") not in exmod.QUOTES:
                continue
            lim = m.get("limits") or {}
            rows[f"{m['base']}/{m['quote']}"] = {
                "symbol": m["symbol"], "active": m.get("active") is not False,
                "min_cost": _num((lim.get("cost") or {}).get("min")), "min_amount": _num((lim.get("amount") or {}).get("min"))}
        if rows:
            try:
                tickers = await ex.fetch_tickers([r["symbol"] for r in rows.values()])
            except Exception:
                try:
                    tickers = await ex.fetch_tickers()
                except Exception:
                    tickers = {}
            for r in rows.values():
                t = tickers.get(r["symbol"]) or {}
                r["price"] = _num(t.get("last") or t.get("close"))
                r["volume"] = _num(t.get("quoteVolume")) or ((_num(t.get("baseVolume")) or 0) * (r["price"] or 0) or None)
        return rows
    except Exception as e:
        log.info("%s spot markets: %s", name, e)
        return {}
    finally:
        try:
            prio.reset(tok)
            await ex.close()
        except Exception:
            pass


async def load_exchange(name: str, pairs: set[str]) -> dict:
    """{"pairs": {pair: info}, "spot": {...}, "error": ""}: one exchange's futures (linear perpetual) markets
    and its spot markets in every quote coin."""
    spot = await load_spot(name, pairs)
    if name in exmod.SPOT_ONLY:
        return {"pairs": {}, "spot": spot, "error": "" if spot else f"{name}: no spot markets"}
    res = await _load_futures(name, pairs)
    res["spot"] = spot
    return res


async def _load_futures(name: str, pairs: set[str]) -> dict:
    klass_id, default_type = exmod.ccxt_target(name, "futures")
    klass = getattr(exmod.ccxt, klass_id, None)
    if klass is None:
        return {"pairs": {}, "error": f"{name} not supported"}
    ex = klass({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": default_type}})
    from .ratelimit import governor, prio
    governor.attach(ex)
    tok = prio.set("background")
    try:
        markets = await ex.load_markets()
        rows: dict[str, dict] = {}
        for m in markets.values():
            if not m.get("swap") or m.get("inverse") or m.get("settle") != m.get("quote"):
                continue
            pair = f"{m.get('base')}/{m.get('quote')}"
            if pair not in pairs:
                continue
            lim = m.get("limits") or {}
            rows[pair] = {"symbol": m["symbol"], "active": m.get("active") is not False,
                          "min_cost": _num((lim.get("cost") or {}).get("min")),
                          "min_amount": _num((lim.get("amount") or {}).get("min")),
                          "contract_size": _num(m.get("contractSize")) or 1.0,
                          "max_leverage": _num((lim.get("leverage") or {}).get("max"))}
        if rows:
            try:
                tickers = await ex.fetch_tickers([r["symbol"] for r in rows.values()])
            except Exception:  # some exchanges only allow all tickers at once
                tickers = await ex.fetch_tickers()
            for r in rows.values():
                t = tickers.get(r["symbol"]) or {}
                r["price"] = _num(t.get("last") or t.get("close"))
        return {"pairs": rows, "error": ""}
    except Exception as e:
        return {"pairs": {}, "error": f"{type(e).__name__}: {str(e)[:150]}"}
    finally:
        try:
            prio.reset(tok)
            await ex.close()
        except Exception:
            pass


async def refresh(exchanges: list[str] | None = None) -> dict:
    with SessionLocal() as s:
        gs = get_global(s)
        names = exchanges or list(gs.allowed_exchanges or [])
        cache = dict(gs.market_cache or {})
    pairs = wanted_pairs()
    results = await asyncio.gather(*(load_exchange(n, pairs) for n in names))
    now = utcnow().isoformat(timespec="seconds")
    for name, res in zip(names, results):
        old = cache.get(name) or {}
        if not res.get("spot") and old.get("spot"):
            res["spot"] = old["spot"]
        if res["error"] and (old.get("pairs") or old.get("spot")):  # keep the last good data, note the error
            cache[name] = {**old, "error": res["error"]}
        else:
            cache[name] = {**res, "at": now}
    with SessionLocal() as s:
        get_global(s).market_cache = cache
        s.commit()
    return cache


def info(pair: str, exchange: str) -> dict | None:
    with SessionLocal() as s:
        cache = get_global(s).market_cache or {}
    return ((cache.get(exchange) or {}).get("pairs") or {}).get(pair)


def spot_info(pair: str, quote: str, exchange: str) -> dict | None:
    """The spot market of this pair's coin in `quote` on `exchange` (BTC/USDT, EUR -> BTC/EUR), None if not listed."""
    with SessionLocal() as s:
        cache = get_global(s).market_cache or {}
    return ((cache.get(exchange) or {}).get("spot") or {}).get(f"{pair.split('/')[0].upper()}/{quote.upper()}")


def spot_known(exchange: str) -> bool:
    with SessionLocal() as s:
        return bool(((get_global(s).market_cache or {}).get(exchange) or {}).get("spot"))


def quotes_on(exchange: str, cache: dict | None = None) -> list[str]:
    """Quote coins with spot markets on this exchange (in the site's order: USDT, USDC, USD, EUR, ...)."""
    if cache is None:
        cache = all_info()
    have = {k.split("/")[1] for k in ((cache.get(exchange) or {}).get("spot") or {})}
    return [q for q in exmod.QUOTES if q in have]


def usd_rate(quote: str, cache: dict | None = None) -> float | None:
    """How many USD one unit of `quote` is worth (EUR -> 1.08), from any exchange's spot prices; 1 for dollars."""
    q = quote.upper()
    if q in exmod.USD_LIKE:
        return 1.0
    if cache is None:
        cache = all_info()
    for ex in cache.values():
        spot = (ex or {}).get("spot") or {}
        for usd in ("USDT", "USDC", "USD"):
            direct, inverse = spot.get(f"{q}/{usd}") or {}, spot.get(f"{usd}/{q}") or {}
            if direct.get("price"):
                return direct["price"]
            if inverse.get("price"):
                return 1 / inverse["price"]
    return None


def convert(amount: float | None, src: str, dst: str, cache: dict | None = None) -> float | None:
    """`amount` in `src` expressed in `dst` (None when no rate is known)."""
    if amount is None or src.upper() == dst.upper():
        return amount
    a, b = usd_rate(src, cache), usd_rate(dst, cache)
    return amount * a / b if a and b else None


def all_info() -> dict[str, dict]:
    with SessionLocal() as s:
        return dict(get_global(s).market_cache or {})


def min_order(i: dict | None) -> float | None:
    """Smallest order value (notional, in the quote coin) the exchange accepts for the pair."""
    if not i:
        return None
    by_amount = (i.get("min_amount") or 0) * (i.get("price") or 0) * (i.get("contract_size") or 1)
    need = max(i.get("min_cost") or 0, by_amount)
    return round(need, 4) if need else None


def min_margin(i: dict | None, leverage: int | float) -> float | None:
    """Smallest margin for one trade at this leverage."""
    need = min_order(i)
    return round(need / max(1.0, float(leverage or 1)), 4) if need else None


async def loop() -> None:
    await asyncio.sleep(30)  # let the app start first
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("market refresh failed")
        await asyncio.sleep(REFRESH_HOURS * 3600)
