"""Market benchmarks: daily closes of BTC, ETH, the top coins and the total crypto market cap.

Sources (no API key needed):
* coin prices  -> daily candles from public exchange APIs via ccxt (Binance, then Bybit, OKX, KuCoin)
* top-5 list   -> CoinGecko /coins/markets (stablecoins and wrapped tokens excluded)
* TOTAL        -> CoinGecko: full history if the endpoint is available, otherwise today's /global figure
                  every day + a one-time backfill of 365 days from the top-10 coins' market caps,
                  scaled to today's total (marked as an approximation)
Updated at start-up and every 6 hours.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import defaultdict
from typing import Any

import ccxt.async_support as ccxt
from sqlalchemy import func, select

from ..db import BenchmarkPrice, SessionLocal, get_global, utcnow

log = logging.getLogger("relay.bench")

TOTAL = "TOTAL"
FALLBACK_TOP = ["BTC", "ETH", "XRP", "BNB", "SOL"]
STABLE = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "BUSD", "PYUSD", "USDD", "USDS", "FRAX", "USD1", "RLUSD", "USDTB",
          "BSC-USD", "SUSDE", "USDX", "EURC", "USD0"}
WRAPPED_PREFIX = ("W", "ST", "WST", "WE", "CB", "RETH", "JITO", "BETH")
EXCHANGES = ("binance", "bybit", "okx", "kucoin")
CG = "https://api.coingecko.com/api/v3"
HISTORY_START = dt.datetime(2018, 1, 1, tzinfo=dt.UTC)
NAMES = {"TOTAL": "Crypto market cap", "BTC": "Bitcoin", "ETH": "Ethereum", "XRP": "XRP", "BNB": "BNB", "SOL": "Solana",
         "DOGE": "Dogecoin", "ADA": "Cardano", "TRX": "TRON"}


# ------------------------------------------------------------------ fetching
async def _cg(path: str, params: dict | None = None) -> Any:
    """Paced + retried (see coingecko.py): the free limits are shared with icons and market widgets."""
    from . import coingecko
    return await coingecko.get(path, params)


def _is_real_coin(sym: str, name: str) -> bool:
    if sym in STABLE or "usd" in name.lower() and sym.startswith("USD"):
        return False
    wrapped = any(w in name.lower() for w in ("wrapped", "staked", "bridged", "liquid staking"))
    return not wrapped


async def top_coins(n: int = 5) -> list[dict]:
    """Top coins by market cap (CoinGecko), without stablecoins / wrapped tokens."""
    rows = await _cg("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 40, "page": 1})
    out = [{"symbol": r["symbol"].upper(), "id": r["id"], "name": r["name"], "market_cap": r.get("market_cap") or 0}
           for r in rows if _is_real_coin(r["symbol"].upper(), r.get("name", ""))]
    return out[:n]


def _last_day(symbol: str) -> dt.date | None:
    with SessionLocal() as s:
        return s.scalar(select(func.max(BenchmarkPrice.day)).where(BenchmarkPrice.symbol == symbol))


def _store(symbol: str, rows: list[tuple[dt.date, float]], source: str, overwrite: bool = True) -> int:
    if not rows:
        return 0
    with SessionLocal() as s:
        existing = {b.day: b for b in s.scalars(select(BenchmarkPrice).where(
            BenchmarkPrice.symbol == symbol, BenchmarkPrice.day.in_([d for d, _ in rows])))}
        n = 0
        for day, close in rows:
            if close is None or close <= 0:
                continue
            b = existing.get(day)
            if b is None:
                s.add(BenchmarkPrice(symbol=symbol, day=day, close=float(close), source=source))
                n += 1
            elif overwrite:
                b.close, b.source = float(close), source
        s.commit()
    return n


async def update_coin(symbol: str) -> str:
    """Daily candles SYMBOL/USDT from the first exchange that has them."""
    last = _last_day(symbol)
    since = int(((dt.datetime.combine(last, dt.time(), dt.UTC) - dt.timedelta(days=2)) if last else HISTORY_START)
                .timestamp() * 1000)
    errors = []
    for ex_id in EXCHANGES:
        klass = getattr(ccxt, ex_id, None)
        if klass is None:
            continue
        ex = klass({"enableRateLimit": True, "timeout": 20000})
        from .ratelimit import governor, prio
        governor.attach(ex)
        prio.set("background")  # this loop's own task: benchmarks never compete with trading
        try:
            rows: list[tuple[dt.date, float]] = []
            cursor = since
            for _ in range(20):  # 20 x 1000 days is plenty
                candles = await ex.fetch_ohlcv(f"{symbol}/USDT", "1d", since=cursor, limit=1000)
                if not candles:
                    break
                rows += [(dt.datetime.fromtimestamp(c[0] / 1000, dt.UTC).date(), c[4]) for c in candles]
                if len(candles) < 2 or candles[-1][0] <= cursor:
                    break
                cursor = candles[-1][0] + 86_400_000
                if cursor > utcnow().timestamp() * 1000 + 86_400_000:
                    break
            if rows:
                _store(symbol, rows, ex_id)
                return ex_id
            errors.append(f"{ex_id}: no data")
        except Exception as e:
            errors.append(f"{ex_id}: {type(e).__name__}")
        finally:
            await ex.close()
    raise RuntimeError("; ".join(errors) or "no exchange available")


async def update_total(top10_ids: list[str]) -> str:
    """Whole crypto market cap (USD)."""
    try:  # full history (only on some CoinGecko plans)
        data = await _cg("/global/market_cap_chart", {"vs_currency": "usd", "days": "max"})
        pts = (data.get("market_cap_chart") or {}).get("market_cap") or []
        if pts:
            _store(TOTAL, [(dt.datetime.fromtimestamp(t / 1000, dt.UTC).date(), v) for t, v in pts], "coingecko")
            return "coingecko-history"
    except Exception:
        pass
    g = await _cg("/global")
    today_total = float(((g.get("data") or {}).get("total_market_cap") or {}).get("usd") or 0)
    if today_total:
        _store(TOTAL, [(utcnow().date(), today_total)], "coingecko-global")
    with SessionLocal() as s:
        n = s.scalar(select(func.count(BenchmarkPrice.id)).where(BenchmarkPrice.symbol == TOTAL)) or 0
    if n < 30 and top10_ids and today_total:  # one-time backfill: sum of top-10 market caps, scaled to the real total
        per_day: dict[dt.date, float] = defaultdict(float)
        for cid in top10_ids:
            mc = await _cg(f"/coins/{cid}/market_chart", {"vs_currency": "usd", "days": 365, "interval": "daily"})
            for t, v in mc.get("market_caps") or []:
                per_day[dt.datetime.fromtimestamp(t / 1000, dt.UTC).date()] += v or 0
        if per_day:
            latest = per_day[max(per_day)]
            scale = today_total / latest if latest else 1
            _store(TOTAL, [(d, v * scale) for d, v in per_day.items() if d < utcnow().date()], "top10-scaled", overwrite=False)
        return "global+top10-backfill"
    return "global"


async def update_all() -> dict:
    state: dict[str, Any] = {"started": utcnow().isoformat(timespec="seconds"), "errors": {}, "sources": {}}
    try:
        top = await top_coins(10)
        symbols = [c["symbol"] for c in top[:5]] or FALLBACK_TOP
        top10_ids = [c["id"] for c in top]
    except Exception as e:
        state["errors"]["top"] = f"{type(e).__name__}: {str(e)[:120]}"
        symbols, top10_ids = list(FALLBACK_TOP), []
    for must in ("BTC", "ETH"):
        if must not in symbols:
            symbols.insert(0, must)
    state["symbols"] = symbols[:6]
    for sym in state["symbols"]:
        try:
            state["sources"][sym] = await update_coin(sym)
        except Exception as e:
            state["errors"][sym] = str(e)[:200]
    try:
        state["sources"][TOTAL] = await update_total(top10_ids)
    except Exception as e:
        state["errors"][TOTAL] = f"{type(e).__name__}: {str(e)[:150]}"
    state["finished"] = utcnow().isoformat(timespec="seconds")
    with SessionLocal() as s:
        get_global(s).bench_state = state
        s.commit()
    log.info("benchmarks updated: %s", state)
    return state


async def loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await update_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("benchmark update failed")
        await asyncio.sleep(6 * 3600)


# ------------------------------------------------------------------ reading
def symbols() -> list[str]:
    with SessionLocal() as s:
        st = get_global(s).bench_state or {}
        have = set(s.scalars(select(BenchmarkPrice.symbol).distinct()))
    order = [TOTAL] + (st.get("symbols") or FALLBACK_TOP)
    return [x for x in order if x in have]


def series(symbol: str, start: dt.date, end: dt.date | None = None) -> list[tuple[dt.datetime, float]]:
    """% change since `start` (first available close on/after start = 0%)."""
    with SessionLocal() as s:
        q = select(BenchmarkPrice).where(BenchmarkPrice.symbol == symbol, BenchmarkPrice.day >= start)
        if end:
            q = q.where(BenchmarkPrice.day <= end)
        rows = list(s.scalars(q.order_by(BenchmarkPrice.day)))
    if not rows:
        return []
    base = rows[0].close
    return [(dt.datetime.combine(r.day, dt.time()), (r.close / base - 1) * 100) for r in rows]


def is_approx_total() -> bool:
    with SessionLocal() as s:
        return bool(s.scalar(select(func.count(BenchmarkPrice.id)).where(
            BenchmarkPrice.symbol == TOTAL, BenchmarkPrice.source == "top10-scaled")))
