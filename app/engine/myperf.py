"""A user's own results, in the same shape as the bots' public performance: the same statistics, curve, market
comparison and per-pair table, but from the trades really made on the user's portfolios (their API keys).
Scope: all portfolios, one portfolio ("a<id>") or one bot ("b<id>", across the user's portfolios)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..db import Bot, ExchangeAccount, SessionLocal, Trade, get_global, utcnow
from . import performance as perf
from .sizing import effective_capital, pair_budget


@dataclass
class MyTrade:
    """A closed Trade, with the field names the performance functions use."""
    id: int
    pair: str
    side: str
    leverage: float
    open_date: dt.datetime | None
    close_date: dt.datetime | None
    open_rate: float | None
    close_rate: float | None
    profit_ratio: float
    profit_abs: float
    stake_amount: float
    exit_reason: str
    bot_id: int | None
    account_id: int
    fees: float
    adopted: bool


def _mine(t: Trade) -> MyTrade:
    return MyTrade(id=t.id, pair=t.pair, side=t.side, leverage=t.leverage or 1, open_date=t.opened_at,
                   close_date=t.closed_at, open_rate=t.entry_price, close_rate=t.exit_price,
                   profit_ratio=(t.realized_pnl / t.stake) if t.stake else 0.0, profit_abs=t.realized_pnl,
                   stake_amount=t.stake, exit_reason=t.exit_reason or "", bot_id=t.bot_id, account_id=t.account_id,
                   fees=t.fees or 0.0, adopted=bool(t.adopted))


def scope_of(user_id: int, raw: str) -> dict:
    """Portfolios and bots this user can pick, and what `raw` selects."""
    with SessionLocal() as s:
        accs = list(s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user_id).order_by(ExchangeAccount.id)))
        bot_ids = sorted({b for a in accs for b in (a.bots or [])} | {b for a in accs for b in (a.pair_bot or {}).values()})
        bots = list(s.scalars(select(Bot).where(Bot.id.in_(bot_ids or [-1])).order_by(Bot.sort, Bot.id)))
        for x in accs + bots:
            s.expunge(x)
    acc_by, bot_by = {a.id: a for a in accs}, {b.id: b for b in bots}
    acc_sel = bot_sel = None
    if raw[:1] == "a" and raw[1:].isdigit() and int(raw[1:]) in acc_by:
        acc_sel = int(raw[1:])
    elif raw[:1] == "b" and raw[1:].isdigit() and int(raw[1:]) in bot_by:
        bot_sel = int(raw[1:])
    scope = f"a{acc_sel}" if acc_sel else f"b{bot_sel}" if bot_sel else "all"
    label = (acc_name(acc_by[acc_sel]) if acc_sel else bot_by[bot_sel].name if bot_sel else None)
    return {"accounts": accs, "bots": bots, "acc_sel": acc_sel, "bot_sel": bot_sel, "scope": scope,
            "scope_label": label, "acc_ids": [acc_sel] if acc_sel else [a.id for a in accs]}


def acc_name(a: ExchangeAccount) -> str:
    return a.label or (a.exchange.title() if a.exchange else "Signals")


def closed(sc: dict, market: str, days: int | None) -> list[MyTrade]:
    with SessionLocal() as s:
        q = select(Trade).where(Trade.account_id.in_(sc["acc_ids"] or [-1]), Trade.status == "closed",
                                Trade.closed_at.is_not(None))
        if sc["bot_sel"]:
            q = q.where(Trade.bot_id == sc["bot_sel"])
        if market in ("long", "short"):
            q = q.where(Trade.side == market)
        if days:
            q = q.where(Trade.closed_at >= utcnow() - dt.timedelta(days=days))
        return [_mine(t) for t in s.scalars(q.order_by(Trade.closed_at))]


def capital(sc: dict) -> float | None:
    """The money behind the scope, for % figures: the portfolios' capital (a bot: the part given to its pairs)."""
    with SessionLocal() as s:
        gs = get_global(s)
    accs = [a for a in sc["accounts"] if a.id in sc["acc_ids"]]
    if sc["bot_sel"]:
        total = sum(pair_budget(gs, a, p) for a in accs for p, b in (a.pair_bot or {}).items() if b == sc["bot_sel"])
    else:
        total = sum(effective_capital(gs, a) for a in accs)
    return total or None


def report(user_id: int, raw_scope: str, market: str, days: int | None) -> dict[str, Any]:
    sc = scope_of(user_id, raw_scope)
    trades = closed(sc, market, days)
    data = perf.summary_of(trades, capital(sc), days)
    pairs = perf.pair_stats_of(trades)
    # per portfolio and per bot (same filters)
    by_acc: dict[int, list[MyTrade]] = {}
    by_bot: dict[int, list[MyTrade]] = {}
    for t in trades:
        by_acc.setdefault(t.account_id, []).append(t)
        if t.bot_id:
            by_bot.setdefault(t.bot_id, []).append(t)
    with SessionLocal() as s:
        gs = get_global(s)
    acc_cards = {a.id: perf._stats(by_acc.get(a.id, []), effective_capital(gs, a) or None) for a in sc["accounts"]}
    bot_cards = {b.id: perf._stats(by_bot.get(b.id, [])) for b in sc["bots"]}
    return {**sc, "data": data if trades else None, "pairs": pairs, "acc_cards": acc_cards, "bot_cards": bot_cards,
            "acc_names": {a.id: acc_name(a) for a in sc["accounts"]}, "bot_names": {b.id: b.name for b in sc["bots"]}}
