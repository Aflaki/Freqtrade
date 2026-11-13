"""Positions closed on the exchange without us: liquidation, auto-deleveraging (ADL), or by hand.

The site closes a position when the bot exits. If the exchange closes it first (a liquidation, ADL) or the user
does it in the exchange's app, our record would stay "open" with nothing behind it. Every few minutes each real
futures portfolio's open records are compared with the exchange's positions; a record missing twice in a row is
looked up in the exchange's own fills (since our last order on it): it is closed with the real exit price, fees
and result, the reason (liquidation / adl / closed outside), and the user is told.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import func, select

from ..db import ExchangeAccount, ExecutionLog, Order, SessionLocal, Trade, User, utcnow

log = logging.getLogger("relay.external")
CHECK_EVERY = 300        # seconds between checks of an account
CONFIRM_AFTER = 60       # a record must be missing in two reads at least this far apart
WEEK_MS = 7 * 86400_000 - 60_000
_missing: dict[int, float] = {}  # trade id -> when it was first seen missing


def classify(client_id: str, info: dict | None = None) -> str:
    """liquidation | adl | outside, from the closing order's client id (Binance) or the fill (Hyperliquid)."""
    cid = (client_id or "").lower()
    info = info or {}
    if cid.startswith("adl_autoclose") or "adl" in str(info.get("liquidation") or "").lower():
        return "adl"
    if cid.startswith("autoclose") or info.get("liquidation"):
        return "liquidation"
    return "outside"


async def _history(ex, symbol: str, since_ms: int) -> tuple[list[dict], dict[str, str]]:
    """Own fills of `symbol` since `since_ms` and {order id: client order id} (windows under 7 days on Binance)."""
    fills: list[dict] = []
    cids: dict[str, str] = {}
    now = int(time.time() * 1000)
    binance = getattr(ex, "id", "") in ("binanceusdm", "binance")
    start = since_ms
    while start < now:
        end = min(start + WEEK_MS, now) if binance else now
        params = {"endTime": end} if binance else {}
        fills += await ex.fetch_my_trades(symbol, start, 500, params) or []
        if getattr(ex, "has", {}).get("fetchOrders"):
            try:
                for o in await ex.fetch_orders(symbol, start, 500, params) or []:
                    cids[str(o.get("id"))] = str(o.get("clientOrderId") or "")
            except Exception:
                pass
        start = end + 1
    return fills, cids


async def check_account(acc_id: int) -> list[dict[str, Any]]:
    """Close the records of positions that are gone from the exchange. Returns what was closed."""
    from ..i18n import t
    from ..telegram import bot, esc, fmt_num
    from .exchange import pool
    from .positions import can_scan, normalize
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        if acc is None or not acc.enabled or not can_scan(acc):
            return []
        s.expunge(acc)
        open_trades = list(s.scalars(select(Trade).where(Trade.account_id == acc_id, Trade.status == "open",
                                                         Trade.dry_run.is_(False))))
    if not open_trades:
        return []
    async with pool.lock(acc_id):  # never while the bot's own exit for it is being placed
        client = await pool.get(acc)
        held = {(p["symbol"], p["side"]) for p in normalize(await client.positions(fresh=True))}
        now = time.monotonic()
        gone = []
        for tr in open_trades:
            if (tr.symbol, tr.side) in held:
                _missing.pop(tr.id, None)
                continue
            if tr.id not in _missing:  # first read without it: confirm on the next round
                _missing[tr.id] = now
            elif now - _missing[tr.id] >= CONFIRM_AFTER:
                gone.append(tr)
        out = []
        for tr in gone:
            try:
                res = await _close_from_fills(client, acc, tr)
            except Exception as e:
                log.info("[acc %s] fills for %s unavailable: %s", acc_id, tr.symbol, e)
                continue
            _missing.pop(tr.id, None)
            if res is None:
                continue
            out.append(res)
            with SessionLocal() as s:
                user = s.get(User, acc.user_id)
                s.add(ExecutionLog(user_id=acc.user_id, account_id=acc.id, trade_id=tr.id, action="exchange_close",
                                   pair=tr.pair, status="closed", message=res["message"]))
                s.commit()
                if user is not None:
                    L = user.lang
                    cur = tr.currency
                    text = (f"{'🔻' if res['pnl'] < 0 else '💰'} {t(L, 'tg_ext_' + res['reason'])}\n"
                            f"<b>{esc(tr.pair)}</b> {tr.side.upper()} — {esc(acc.label or acc.exchange)}\n"
                            f"{t(L, 'tg_entry')}: {fmt_num(tr.entry_price)} → {t(L, 'tg_exit')}: <b>{fmt_num(res['price'])}</b>\n"
                            f"{t(L, 'tg_pnl')}: <b>{res['pnl']:+,.2f} {cur}</b>")
                    bot.notify(user, "exit_fill", text)
        return out


async def _close_from_fills(client, acc: ExchangeAccount, tr: Trade) -> dict | None:
    with SessionLocal() as s:
        last = s.scalar(select(func.max(Order.created_at)).where(Order.trade_id == tr.id)) or tr.opened_at
        ours = {o for o in s.scalars(select(Order.exchange_order_id).where(Order.exchange_order_id != "")) if o}
    since = int(last.replace(tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000) - 5_000
    fills, cids = await _history(client.ex, tr.symbol, since)
    closing_side = "buy" if tr.side == "short" else "sell"
    need = tr.amount
    picked, got = [], 0.0
    for f in sorted(fills, key=lambda f: f.get("timestamp") or 0):
        if f.get("side") != closing_side or str(f.get("order")) in ours:
            continue
        picked.append(f)
        got += float(f["amount"])
        if got >= need * 0.999:
            break
    if not picked:
        reason, price, qty, fee = "outside", None, need, 0.0
    else:
        qty = min(got, need) if need else got
        price = sum(float(f["amount"]) * float(f["price"]) for f in picked) / got
        fee = 0.0
        for f in picked:
            fe = f.get("fee") or {}
            if fe.get("cost") is not None:
                fee += float(fe["cost"]) * (price if fe.get("currency") == f.get("symbol", "").split("/")[0] else 1.0)
        reason = classify(cids.get(str(picked[-1].get("order")), ""), picked[-1].get("info"))
    with SessionLocal() as s:
        t = s.get(Trade, tr.id)
        if t is None or t.status != "open":
            return None
        d = 1 if t.side == "long" else -1
        entry_fees = s.scalar(select(func.coalesce(func.sum(Order.fee), 0)).where(
            Order.trade_id == t.id, Order.kind.in_(("entry", "dca")))) or 0.0
        total = t.amount + t.closed_amount
        pnl = 0.0
        if price:
            pnl = (price - t.entry_price) * qty * t.contract_size * d - fee - (entry_fees * qty / total if total else 0)
            t.exit_value += price * qty
            t.closed_amount += qty
            s.add(Order(trade_id=t.id, exchange_order_id=str(picked[-1].get("order") or ""), kind="exit",
                        side=closing_side, amount=qty, filled=qty, price=price, fee=fee, status=reason))
        t.realized_pnl += pnl
        t.fees += fee
        t.amount = 0.0
        t.status, t.closed_at = ("closed" if price else "void"), utcnow()  # void: no result known, left out of stats
        t.exit_reason = {"liquidation": "liquidation", "adl": "adl"}.get(reason, "closed_on_exchange" if price else "not_on_exchange")
        s.commit()
    msg = f"{tr.symbol} closed on the exchange ({reason})" + (f" @ {price:.8g}, pnl={pnl:.4f}" if price else ", fills not found")
    log.warning("[acc %s] %s", acc.id, msg)
    return {"trade_id": tr.id, "pair": tr.pair, "reason": reason, "price": price, "pnl": pnl, "message": msg}


async def loop() -> None:
    from .positions import can_scan
    from .ratelimit import prio
    await asyncio.sleep(90)
    prio.set("background")
    while True:
        try:
            with SessionLocal() as s:
                ids = [a.id for a in s.scalars(select(ExchangeAccount).where(ExchangeAccount.enabled.is_(True)))
                       if can_scan(a)]
            for acc_id in ids:
                try:
                    await check_account(acc_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.info("[acc %s] external-close check skipped: %s", acc_id, e)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("external-close loop failed")
        await asyncio.sleep(CHECK_EVERY / 2)  # each record is confirmed on the next round
