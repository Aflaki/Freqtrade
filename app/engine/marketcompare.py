"""The market test: does a bot's signal work as well when it is traded on another market?

The bots trade Binance USDT pairs. A user's portfolio may trade the same signal on another exchange and/or in another
quote coin (BTC/EUR on Kraken, spot instead of futures...). For every closed trade of a portfolio we take the bot's own
trade (same bot, same Freqtrade trade id) and compare the two results without leverage (a spot portfolio buys without
leverage, the bot trades futures): the difference is what moving the signal to that market cost or gained
(price differences between markets, fees, slippage, timing).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from ..db import BotTrade, ExchangeAccount, SessionLocal, Trade, utcnow
from .exchange import EXCHANGES


def _market(acc: ExchangeAccount, t: Trade) -> str:
    return f"{EXCHANGES.get(acc.exchange, acc.exchange)} · {'spot' if acc.is_spot else 'futures'} · {t.currency}"


def compare(days: int | None = None) -> dict[str, Any]:
    with SessionLocal() as s:
        q = select(Trade).where(Trade.status == "closed", Trade.is_signal.is_(False), Trade.bot_id.is_not(None),
                                Trade.ft_trade_id.is_not(None), Trade.stake > 0)
        if days:
            q = q.where(Trade.closed_at >= utcnow() - dt.timedelta(days=days))
        trades = list(s.scalars(q))
        accs = {a.id: a for a in s.scalars(select(ExchangeAccount).where(
            ExchangeAccount.id.in_({t.account_id for t in trades} or {-1})))}
        keys = {(t.bot_id, t.ft_trade_id) for t in trades}
        bots = {(b.bot_id, b.ft_trade_id): b for b in s.scalars(select(BotTrade).where(
            BotTrade.bot_id.in_({k[0] for k in keys} or {-1}), BotTrade.is_open.is_(False),
            BotTrade.profit_ratio.is_not(None)))}
        for x in list(accs.values()) + trades + list(bots.values()):
            s.expunge(x)
    rows = []
    for t in trades:
        b, acc = bots.get((t.bot_id, t.ft_trade_id)), accs.get(t.account_id)
        if b is None or acc is None:
            continue
        ours = t.realized_pnl / t.stake / max(1.0, t.leverage or 1) * 100  # % of the position value
        bot = (b.profit_ratio or 0) / max(1.0, b.leverage or 1) * 100
        rows.append({"market": _market(acc, t), "pair": t.pair, "symbol": t.symbol, "ours": ours, "bot": bot,
                     "diff": ours - bot, "pnl": t.realized_pnl, "cur": t.currency, "paper": bool(t.dry_run),
                     "closed": t.closed_at})

    def agg(rs: list[dict]) -> dict:
        n = len(rs)
        return {"trades": n, "paper": sum(1 for r in rs if r["paper"]),
                "ours": sum(r["ours"] for r in rs) / n, "bot": sum(r["bot"] for r in rs) / n,
                "diff": sum(r["diff"] for r in rs) / n, "better": sum(1 for r in rs if r["diff"] > 0.001),
                "worse": sum(1 for r in rs if r["diff"] < -0.001), "wins": sum(1 for r in rs if r["pnl"] > 0),
                "pnl": sum(r["pnl"] for r in rs), "cur": rs[0]["cur"]}
    by_market: dict[str, list] = defaultdict(list)
    by_pair: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_market[r["market"]].append(r)
        by_pair[(r["market"], r["pair"])].append(r)
    markets = sorted(({"market": m, **agg(rs)} for m, rs in by_market.items()), key=lambda x: -x["trades"])
    pairs = sorted(({"market": m, "pair": p, **agg(rs)} for (m, p), rs in by_pair.items()),
                   key=lambda x: (x["market"], -x["trades"]))
    return {"markets": markets, "pairs": pairs, "matched": len(rows), "unmatched": len(trades) - len(rows)}
