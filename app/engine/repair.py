"""Correct recorded trades from the exchange's own fills (exact price, amount and fee of every order).

Hyperliquid answers a market order with its slippage cap as "price" and no fee; until the fix, exits could be
recorded at that cap (5% away) and every fee as zero. This reads each order's fills again and recomputes the
trade: entry = weighted average of the entry fills, exits at their real prices, result net of all fees.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from ..db import ExchangeAccount, Order, SessionLocal, Trade


async def from_fills(acc_id: int, apply: bool = False, days: int = 30) -> list[dict[str, Any]]:
    """Per trade: {"id", "pair", "old": {...}, "new": {...}, "orders": n, "missing": n}. Saved only with apply."""
    from .exchange import ExchangeClient
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        if acc is None:
            return []
        s.expunge(acc)
    client = ExchangeClient(acc)
    out: list[dict[str, Any]] = []
    try:
        await client.ensure_markets()
        since = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(days=days)
        with SessionLocal() as s:
            trades = list(s.scalars(select(Trade).where(Trade.account_id == acc_id, Trade.opened_at >= since,
                                                        Trade.dry_run.is_(False)).order_by(Trade.id)))
            fills_cache: dict[str, list] = {}
            for tr in trades:
                orders = list(s.scalars(select(Order).where(Order.trade_id == tr.id, Order.exchange_order_id != "")
                                        .order_by(Order.id)))
                if not orders:
                    continue
                if tr.symbol not in fills_cache:
                    start = int((tr.opened_at - dt.timedelta(hours=1)).replace(tzinfo=dt.UTC).timestamp() * 1000)
                    fills_cache[tr.symbol] = await _all_fills(client, tr.symbol, start)
                fills = fills_cache[tr.symbol]
                m = client.market(tr.symbol)
                missing = 0
                for o in orders:
                    mine = [f for f in fills if str(f.get("order")) == o.exchange_order_id]
                    if not mine:
                        missing += 1
                        continue
                    qty = sum(float(f["amount"]) for f in mine)
                    px = sum(float(f["amount"]) * float(f["price"]) for f in mine) / qty
                    fee = 0.0
                    for f in mine:
                        fe = f.get("fee") or {}
                        if fe.get("cost") is None:
                            continue
                        fee += float(fe["cost"]) * (px if fe.get("currency") == m.get("base") else 1.0)
                    o._new = (qty, px, fee)
                if missing == len(orders):
                    continue
                entries = [o for o in orders if o.kind in ("entry", "dca") and hasattr(o, "_new")]
                exits = [o for o in orders if o.kind == "exit" and hasattr(o, "_new")]
                if not entries:
                    continue
                e_qty = sum(o._new[0] for o in entries)
                entry = sum(o._new[0] * o._new[1] for o in entries) / e_qty
                entry_fees = sum(o._new[2] for o in entries)
                d = 1 if tr.side == "long" else -1
                cs = tr.contract_size or 1.0
                pnl = 0.0
                exit_value = 0.0
                closed = 0.0
                for o in exits:
                    q, px, fee = o._new
                    pnl += (px - entry) * q * cs * d - fee - entry_fees * q / e_qty
                    exit_value += px * q
                    closed += q
                fees = entry_fees + sum(o._new[2] for o in exits)
                old = {"entry": tr.entry_price, "exit": tr.exit_price, "pnl": round(tr.realized_pnl, 4),
                       "fees": round(tr.fees, 4)}
                new = {"entry": entry, "exit": exit_value / closed if closed else None, "pnl": round(pnl, 4),
                       "fees": round(fees, 4)}
                out.append({"id": tr.id, "pair": tr.pair, "status": tr.status, "old": old, "new": new,
                            "orders": len(orders), "missing": missing})
                if apply and missing == 0:
                    for o in orders:
                        o.filled, o.price, o.fee = o._new
                    tr.entry_price, tr.fees = entry, fees
                    if exits:
                        tr.exit_value, tr.realized_pnl = exit_value, pnl
                    tr.stake = entry * e_qty * cs / max(1.0, tr.leverage or 1)
            if apply:
                s.commit()
    finally:
        await client.close()
    return out


async def _all_fills(client, symbol: str, since_ms: int) -> list[dict]:
    """Every own fill of `symbol` since `since_ms` (paged)."""
    out: list[dict] = []
    for _ in range(20):
        batch = await client.ex.fetch_my_trades(symbol, since_ms, 500)
        if not batch:
            break
        out += batch
        last = max(int(f.get("timestamp") or 0) for f in batch)
        if len(batch) < 500 or last <= since_ms:
            break
        since_ms = last + 1
    seen, uniq = set(), []
    for f in out:
        k = (f.get("id"), f.get("order"), f.get("timestamp"), f.get("amount"))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq
