"""Open positions as the exchange sees them, for the user's pages: live price, live profit/loss, liquidation price,
the exchange's own entry price and size, and, for positions taken over from before, when they were really opened.

Reads are on demand (when someone looks), cached for a few seconds per portfolio, and not written to the execution
log. What the exchange says is copied into our open trades (entry price, size, leverage), so exits and statistics
work with the real numbers.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from sqlalchemy import select

from ..db import ExchangeAccount, SessionLocal, Trade
from .positions import can_scan, key_of, normalize

log = logging.getLogger("relay.livepos")
MAX_AGE = 15.0
HISTORY_WEEKS = 12  # how far back to look for when a taken-over position was opened
_cache: dict[int, dict[str, Any]] = {}  # account id -> {"at": monotonic, "rows": {key: position}, "error": str}
_prices: dict[str, tuple[float, float]] = {}  # symbol -> (monotonic, price), for paper/signal trades
_bg: set[asyncio.Task] = set()  # open-time lookups running in the background
_date_lock: asyncio.Lock | None = None
_dated: set[tuple] = set()  # (trade id, symbol, side, taken-over time): open time looked up (found or not)


def opened_at_from_fills(fills: list[dict], side: str, contracts: float) -> dt.datetime | None:
    """Walk the account's fills in this symbol backwards from the current position until it was flat:
    the fill that opened it (or flipped it to this side) gives the open time."""
    pos = contracts if side == "long" else -contracts
    eps = max(abs(contracts) * 1e-6, 1e-12)
    for f in sorted(fills, key=lambda x: x.get("timestamp") or 0, reverse=True):
        amt = float(f.get("amount") or 0)
        signed = amt if f.get("side") == "buy" else -amt
        before = pos - signed
        if abs(before) <= eps or (before > 0) != (pos > 0):
            ts = f.get("timestamp")
            return dt.datetime.fromtimestamp(ts / 1000, dt.UTC).replace(tzinfo=None) if ts else None
        pos = before
    return None


async def _open_time(ex, symbol: str, side: str, contracts: float) -> dt.datetime | None:
    """Binance returns at most 7 days of fills per call: go back week by week."""
    if not getattr(ex, "has", {}).get("fetchMyTrades", True):
        return None
    fills: list[dict] = []
    end = int(time.time() * 1000)
    week = 7 * 24 * 3600 * 1000
    for _ in range(HISTORY_WEEKS):
        try:
            batch = await ex.fetch_my_trades(symbol, end - week, 1000, {"endTime": end})
        except Exception as e:
            from .ratelimit import ApiBanned, ApiBusy
            if isinstance(e, (ApiBusy, ApiBanned)):
                raise
            log.info("fills of %s unavailable: %s", symbol, e)
            return None
        fills += batch or []
        found = opened_at_from_fills(fills, side, contracts)
        if found is not None:
            return found
        end -= week
    return None


async def _read_account(acc: ExchangeAccount, rest: bool = False) -> dict[str, Any]:
    from .exchange import pool
    from .ratelimit import prio
    from .streams import manager as streams
    client = await pool.get(acc)
    live = None if rest else streams.positions(acc.id)
    if live is not None:  # pushed by the exchange: no request at all
        return {"rows": {p["key"]: p for p in normalize(live, quote=acc.quote or None)}, "ex": client.ex}
    tok = prio.set("view")  # skipped (cache shown) when the request budget is busy with trading
    try:
        raw = await client.ex.fetch_positions()  # not via client._call: a page view is not a trading action
    finally:
        prio.reset(tok)
    return {"rows": {p["key"]: p for p in normalize(raw, quote=acc.quote or None)}, "ex": client.ex}


async def refresh(acc_id: int, max_age: float = MAX_AGE, rest: bool = False) -> dict[str, Any]:
    """The account's positions on the exchange (cached), with our open trades brought in line with them."""
    hit = _cache.get(acc_id)
    if hit and time.monotonic() - hit["at"] < max_age:
        return hit
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        if acc is None or not can_scan(acc):
            return {"rows": {}, "error": "", "at": time.monotonic()}
        s.expunge(acc)
    try:
        got = await asyncio.wait_for(_read_account(acc, rest), 20)
    except Exception as e:
        from .ratelimit import ApiBusy
        if isinstance(e, ApiBusy) and hit:  # trading needs the budget now: show the last numbers a bit longer
            return hit
        out = {**(hit or {"rows": {}}), "error": f"{type(e).__name__}: {str(e)[:160]}", "at": time.monotonic()}
        _cache[acc_id] = out
        return out
    rows = got["rows"]
    undated = []
    with SessionLocal() as s:
        for tr in s.scalars(select(Trade).where(Trade.account_id == acc_id, Trade.status == "open")):
            p = rows.get(key_of(tr.symbol, tr.side))
            if p is None:
                continue
            if p["entry"] and abs(p["entry"] - tr.entry_price) > 1e-12:
                tr.entry_price = p["entry"]
            if p["contracts"] and abs(p["contracts"] - tr.amount) > 1e-12:
                tr.amount = p["contracts"]  # closed/added by hand on the exchange: follow the real size
            if p.get("lev_known") and abs(p["leverage"] - tr.leverage) > 1e-9:
                tr.leverage = p["leverage"]
            if tr.adopted and (tr.id, tr.symbol, tr.side, tr.opened_at) not in _dated:
                undated.append((tr.id, tr.symbol, tr.side, p["contracts"], tr.opened_at))
        s.commit()
    if undated:  # once per trade, in the background: when was it really opened?
        for u in undated:
            _dated.add(u[:3] + (u[4],))
        task = asyncio.get_running_loop().create_task(_date_trades(got["ex"], undated))
        _bg.add(task)
        task.add_done_callback(_bg.discard)
    out = {"rows": rows, "error": "", "at": time.monotonic()}
    _cache[acc_id] = out
    return out


async def _date_trades(ex, undated: list[tuple]) -> None:
    global _date_lock
    from .ratelimit import ApiBanned, ApiBusy, prio
    _date_lock = _date_lock or asyncio.Lock()
    prio.set("background")  # this task only: the lowest share of the request budget
    async with _date_lock:  # one account's history at a time: gentle on the exchange's rate limits
        for tid, symbol, side, contracts, was in undated:
            try:
                when = await _open_time(ex, symbol, side, contracts)
            except (ApiBusy, ApiBanned):
                _dated.discard((tid, symbol, side, was))  # try again on a later view
                return
            except Exception as e:
                log.info("open time of trade %s unknown: %s", tid, e)
                continue
            if when is None:
                continue
            _dated.add((tid, symbol, side, when))
            with SessionLocal() as s:
                tr = s.get(Trade, tid)
                if tr is not None:
                    tr.opened_at = when
                    s.commit()


async def drain() -> None:
    """Wait for the background lookups (tests)."""
    while _bg:
        await asyncio.gather(*list(_bg), return_exceptions=True)


async def _price(acc: ExchangeAccount, symbol: str) -> float | None:
    hit = _prices.get(symbol)
    if hit and time.monotonic() - hit[0] < MAX_AGE:
        return hit[1]
    from .exchange import pool
    try:
        client = await pool.get(acc)
        px = await asyncio.wait_for(client.price(symbol), 10)
    except Exception:
        return hit[1] if hit else None
    _prices[symbol] = (time.monotonic(), px)
    return px


def cached_for_trades(trades: list[Trade]) -> dict[int, dict[str, Any]]:
    """Like for_trades, but only from what was read before (no exchange call): pages open at once and the
    browser asks /trades/live for fresh numbers right after."""
    out: dict[int, dict[str, Any]] = {}
    for t in trades:
        st = _cache.get(t.account_id)
        p = st["rows"].get(key_of(t.symbol, t.side)) if st else None
        if p is not None:
            margin = p["margin"] or t.stake
            out[t.id] = {"mark": p["mark"], "upnl": p["upnl"], "pct": p["upnl"] / margin * 100 if margin else None,
                         "liq": p.get("liq"), "entry": p["entry"], "live": True, "missing": False}
        elif t.symbol in _prices:
            px = _prices[t.symbol][1]
            upnl = (px - t.entry_price) * t.amount * t.contract_size * (1 if t.side == "long" else -1)
            out[t.id] = {"mark": px, "upnl": upnl, "pct": upnl / t.stake * 100 if t.stake else None, "liq": None,
                         "entry": t.entry_price, "live": False, "missing": False}
    return out


async def for_trades(trades: list[Trade]) -> dict[int, dict[str, Any]]:
    """{trade id: {mark, upnl, pct, liq, live: from the exchange?, missing: not on the exchange}}."""
    if not trades:
        return {}
    with SessionLocal() as s:
        accs = {a.id: a for a in s.scalars(select(ExchangeAccount).where(
            ExchangeAccount.id.in_({t.account_id for t in trades})))}
        for a in accs.values():
            s.expunge(a)
    real = [a for a in accs.values() if can_scan(a)]
    state = dict(zip([a.id for a in real], await asyncio.gather(*(refresh(a.id) for a in real))))
    with SessionLocal() as s:  # numbers may have just been brought in line with the exchange
        fresh = {t.id: t for t in s.scalars(select(Trade).where(Trade.id.in_([t.id for t in trades])))}
        for t in fresh.values():
            s.expunge(t)
    out: dict[int, dict[str, Any]] = {}
    for t in fresh.values():
        acc = accs.get(t.account_id)
        if acc is None:
            continue
        if acc.id in state:
            st = state[acc.id]
            p = st["rows"].get(key_of(t.symbol, t.side))
            if p is not None:
                margin = p["margin"] or t.stake
                out[t.id] = {"mark": p["mark"], "upnl": p["upnl"], "pct": p["upnl"] / margin * 100 if margin else None,
                             "liq": p.get("liq"), "entry": p["entry"], "live": True, "missing": False,
                             "opened": t.opened_at}
            elif not st["error"]:
                out[t.id] = {"live": True, "missing": True, "opened": t.opened_at}
            continue
        px = await _price(acc, t.symbol)  # paper / signals: our own numbers at the market price
        if px:
            d = 1 if t.side == "long" else -1
            upnl = (px - t.entry_price) * t.amount * t.contract_size * d
            out[t.id] = {"mark": px, "upnl": upnl, "pct": upnl / t.stake * 100 if t.stake else None, "liq": None,
                         "entry": t.entry_price, "live": False, "missing": False, "opened": t.opened_at}
    return out


# ---------------------------------------------------------------- wallet balances (dashboard)
_bal: dict[int, tuple[float, dict]] = {}  # account id -> (monotonic, {"wallet", "equity", "free", "live"})
BAL_MAX_AGE = 60.0


def _real(acc: ExchangeAccount) -> bool:
    return acc.kind == "exchange" and not acc.dry_run and bool(acc.api_key_enc)


def _from_stream(acc: ExchangeAccount, cur: str) -> dict | None:
    from .streams import manager as streams
    wallet = streams.wallet(acc.id, cur)
    pos = streams.positions(acc.id)
    if wallet is None or pos is None:
        return None
    upnl = sum(float(p.get("unrealizedPnl") or 0) for p in pos)
    return {"wallet": wallet, "equity": wallet + upnl, "free": streams.free_estimate(acc.id, cur), "live": True,
            "cur": cur}


def cached_balances(accs: list[ExchangeAccount], cur: str) -> dict[int, dict]:
    """What is known without asking the exchange (pages render at once)."""
    out = {}
    for a in accs:
        if not _real(a):
            continue
        b = _from_stream(a, a.currency(cur)) or (_bal.get(a.id) or (0, None))[1]
        if b is None and a.wallet_balance is not None:
            b = {"wallet": a.wallet_balance, "equity": None, "free": None, "live": False, "cur": a.currency(cur)}
        if b is not None:
            out[a.id] = b
    return out


async def balances(accs: list[ExchangeAccount], cur: str) -> dict[int, dict]:
    """Wallet (closed P/L included), equity (with open P/L) and free margin of each real portfolio: from the live
    connection when there is one, else one balance read per minute while someone looks (skipped when busy)."""
    from .exchange import pool, wallet_of
    from .ratelimit import prio
    out = {}

    async def one(a: ExchangeAccount) -> None:
        c = a.currency(cur)  # spot portfolios: their quote coin (EUR, USDC...)
        live = _from_stream(a, c)
        if live:
            out[a.id] = live
            return
        hit = _bal.get(a.id)
        if hit and time.monotonic() - hit[0] < BAL_MAX_AGE:
            out[a.id] = hit[1]
            return
        tok = prio.set("view")
        try:
            client = await pool.get(a)
            raw = await asyncio.wait_for(client.ex.fetch_balance(), 15)
            b = {"wallet": wallet_of(raw, c), "equity": float((raw.get("total") or {}).get(c) or 0),
                 "free": float((raw.get("free") or {}).get(c) or 0), "live": False, "cur": c}
            _bal[a.id] = (time.monotonic(), b)
            out[a.id] = b
        except Exception as e:  # busy budget, network: the last numbers
            log.info("[acc %s] balance not read: %s", a.id, e)
            if hit:
                out[a.id] = hit[1]
        finally:
            prio.reset(tok)
    await asyncio.gather(*(one(a) for a in accs if _real(a)))
    return out
