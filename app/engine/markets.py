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


async def load_exchange(name: str, pairs: set[str]) -> dict:
    """{"pairs": {pair: info}, "error": ""} for one exchange's futures (linear perpetual) markets."""
    klass_id, default_type = exmod.ccxt_target(name, "futures")
    klass = getattr(exmod.ccxt, klass_id, None)
    if klass is None:
        return {"pairs": {}, "error": f"{name} not supported"}
    ex = klass({"enableRateLimit": True, "timeout": 20000, "options": {"defaultType": default_type}})
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
        if res["error"] and old.get("pairs"):  # keep the last good data, note the error
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
