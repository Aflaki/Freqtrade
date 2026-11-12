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


_wb: tuple[float, set[str]] = (0.0, set())


def wanted_bases() -> set[str]:
    """The bots' coin names (cached for a minute)."""
    import time
    global _wb
    if time.monotonic() - _wb[0] > 60:
        _wb = (time.monotonic(), {p.split("/")[0].upper() for p in wanted_pairs()})
    return _wb[1]


SHALLOW_VOLUME = 100_000  # 24h volume (in the quote coin) below which the form says "thin market"


async def load_spot(name: str, pairs: set[str]) -> dict:
    """{"BTC/EUR": {symbol, active, min_cost, min_amount, price, volume}} for the wanted coins, in every quote coin
    the site supports (the bots' BTC/USDT is traded as BTC/<portfolio quote> on spot). Also the quote coins
    themselves (EUR/USDT...) so amounts can be compared across currencies."""
    klass = getattr(exmod.ccxt, name, None)
    if klass is None:
        return {}
    bases = {p.split("/")[0] for p in pairs} | set(exmod.QUOTES)
    ex = klass({"enableRateLimit": True, "timeout": 20000,
                "options": exmod.options_for(name, defaultType="spot", fetchCurrencies=False)})
    from .ratelimit import governor, prio
    governor.attach(ex)
    tok = prio.set("market")
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
    spot = await load_spot(name, pairs)  # then futures, not both at once: gentle on the exchange's limits
    if name in exmod.SPOT_ONLY:
        return {"pairs": {}, "spot": spot, "error": "" if spot else f"{name}: no spot markets"}
    res = await _load_futures(name, pairs)
    res["spot"] = spot
    return res


def base_candidates(base: str) -> list[str]:
    """The bots' coin name and what other exchanges call the same contract: Binance's 1000PEPE (price of 1,000
    PEPE) is KPEPE on Hyperliquid."""
    base = base.upper()
    out = [base]
    if base.startswith("1000") and len(base) > 4 and not base[4:].startswith("0"):
        out.append("K" + base[4:])
    return out


def _alias_map(pairs: set[str]) -> dict[str, str]:
    """exchange coin name -> the bots' coin name, for the wanted coins."""
    out: dict[str, str] = {}
    for p in pairs:
        b = p.split("/")[0].upper()
        for c in base_candidates(b):
            out.setdefault(c, b)
    return out


async def _load_futures(name: str, pairs: set[str]) -> dict:
    klass_id, default_type = exmod.ccxt_target(name, "futures")
    klass = getattr(exmod.ccxt, klass_id, None)
    if klass is None:
        return {"pairs": {}, "error": f"{name} not supported"}
    ex = klass({"enableRateLimit": True, "timeout": 20000, "options": exmod.options_for(klass_id, defaultType=default_type)})
    from .ratelimit import governor, prio
    governor.attach(ex)
    tok = prio.set("market")
    try:
        markets = await ex.load_markets()
        alias = _alias_map(pairs)
        rows: dict[str, dict] = {}  # the bots' own pairs (X/USDT), as before
        fut: dict[str, dict] = {}   # "BOTCOIN/QUOTE" -> market, for every settle coin the site supports
        for m in markets.values():
            if not m.get("swap") or m.get("inverse") or m.get("settle") != m.get("quote"):
                continue
            lim = m.get("limits") or {}
            info = {"symbol": m["symbol"], "active": m.get("active") is not False,
                    "min_cost": _num((lim.get("cost") or {}).get("min")),
                    "min_amount": _num((lim.get("amount") or {}).get("min")),
                    "contract_size": _num(m.get("contractSize")) or 1.0,
                    "max_leverage": _num((lim.get("leverage") or {}).get("max"))}
            pair = f"{m.get('base')}/{m.get('quote')}"
            if pair in pairs:
                rows[pair] = info
            bot_base = alias.get(str(m.get("base") or "").upper())
            if bot_base and m.get("quote") in exmod.QUOTES and info["active"]:
                key = f"{bot_base}/{m['quote']}"
                if key not in fut or m.get("base", "").upper() == bot_base:  # the exact name wins over an alias
                    fut[key] = dict(info)
        if rows or fut:
            syms = sorted({r["symbol"] for r in list(rows.values()) + list(fut.values())})
            try:
                tickers = await ex.fetch_tickers(syms)
            except Exception:  # some exchanges only allow all tickers at once
                tickers = await ex.fetch_tickers()
            for r in list(rows.values()) + list(fut.values()):
                t = tickers.get(r["symbol"]) or {}
                r["price"] = _num(t.get("last") or t.get("close"))
                r["volume"] = _num(t.get("quoteVolume")) or ((_num(t.get("baseVolume")) or 0) * (r["price"] or 0) or None)
        return {"pairs": rows, "fut": fut, "error": ""}
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
        for part in ("spot", "fut"):
            if not res.get(part) and old.get(part):
                res[part] = old[part]
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


def fut_info(pair: str, quote: str, exchange: str, cache: dict | None = None) -> dict | None:
    """The perpetual of this pair's coin settled in `quote` (BTC/USDT, USDC -> BTC/USDC:USDC), None if not listed."""
    if cache is None:
        cache = all_info()
    return ((cache.get(exchange) or {}).get("fut") or {}).get(f"{pair.split('/')[0].upper()}/{quote.upper()}")


def market_info(pair: str, quote: str, exchange: str, mode: str, cache: dict | None = None) -> dict | None:
    if mode == "spot":
        if cache is None:
            return spot_info(pair, quote, exchange)
        return ((cache.get(exchange) or {}).get("spot") or {}).get(f"{pair.split('/')[0].upper()}/{quote.upper()}")
    return fut_info(pair, quote, exchange, cache)


def known(exchange: str, mode: str, cache: dict | None = None) -> bool:
    if cache is None:
        cache = all_info()
    return bool((cache.get(exchange) or {}).get("spot" if mode == "spot" else "fut"))


def spot_known(exchange: str) -> bool:
    with SessionLocal() as s:
        return bool(((get_global(s).market_cache or {}).get(exchange) or {}).get("spot"))


def quotes_on(exchange: str, cache: dict | None = None, mode: str = "spot") -> list[str]:
    """Quote coins with spot markets (or perpetuals settled in them) on this exchange, in the site's order."""
    if cache is None:
        cache = all_info()
    have = {k.split("/")[1] for k in ((cache.get(exchange) or {}).get("spot" if mode == "spot" else "fut") or {})}
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


def missing(cache: dict | None = None) -> list[str]:
    """Allowed exchanges with no market data yet (just allowed by the admin, or the last load failed)."""
    with SessionLocal() as s:
        gs = get_global(s)
        names = list(gs.allowed_exchanges or [])
        cache = cache if cache is not None else dict(gs.market_cache or {})
    return [n for n in names if not ((cache.get(n) or {}).get("spot") or (cache.get(n) or {}).get("pairs"))]


MISSING_RETRY_S = 600  # a newly allowed exchange is loaded within a minute; a failing one is retried every 10 min
_tried: dict[str, float] = {}


async def loop() -> None:
    import time
    await asyncio.sleep(30)  # let the app start first
    last_full = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_full >= REFRESH_HOURS * 3600:
                last_full = now
                await refresh()
            else:
                todo = [n for n in missing() if now - _tried.get(n, -1e9) >= MISSING_RETRY_S]
                if todo:
                    for n in todo:
                        _tried[n] = now
                    log.info("loading market data for %s", ", ".join(todo))
                    await refresh(todo)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("market refresh failed")
        await asyncio.sleep(60)
