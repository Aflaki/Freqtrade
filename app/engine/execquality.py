"""Execution quality: every trade of a portfolio next to the bot's own trade that signalled it.

The bot (Freqtrade) records the price it entered and exited at and when; we record our own fills. Per trade:

  entry / exit slippage   how much worse (+) or better (-) our fill was than the bot's, in basis points
                          (1 bp = 0.01%); a long that pays more, or a short that sells lower, is worse
  delay                   seconds from the bot's fill to the signal reaching us, and from there to our order
  result                  both results without leverage, net of fees (Freqtrade's own profit ratio for the bot),
                          and the difference: what copying cost or gained on this trade

Prices are compared only when both trade in a dollar coin (USDT/USDC/USD); a EUR market shows results only.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from ..db import (BotTrade, ExchangeAccount, ExecutionLog, Order, SessionLocal, Trade, User, WebhookEvent,
                  utcnow)
from .exchange import EXCHANGES, USD_LIKE

ENTRY_ACTIONS = ("long", "short")


def _secs(a: dt.datetime | None, b: dt.datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def _bps(ours: float | None, bot: float | None, side: str, entry: bool) -> float | None:
    """Positive = our price was worse for us than the bot's."""
    if not ours or not bot:
        return None
    worse_if_higher = (side == "long") == entry  # long entry / short exit: paying more is worse
    d = (ours / bot - 1) * 1e4
    return d if worse_if_higher else -d


def report(acc_ids: list[int], days: int | None = 30, side: str = "all") -> dict[str, Any]:
    with SessionLocal() as s:
        q = select(Trade).where(Trade.account_id.in_(acc_ids or [-1]), Trade.is_signal.is_(False),
                                Trade.bot_id.is_not(None), Trade.ft_trade_id.is_not(None),
                                Trade.status.in_(("open", "closed")))
        if days:
            q = q.where(Trade.opened_at >= utcnow() - dt.timedelta(days=days))
        if side in ("long", "short"):
            q = q.where(Trade.side == side)
        trades = list(s.scalars(q.order_by(Trade.opened_at.desc())))
        ids = [t.id for t in trades]
        accs = {a.id: a for a in s.scalars(select(ExchangeAccount).where(ExchangeAccount.id.in_(acc_ids or [-1])))}
        owners = {u.id: u for u in s.scalars(select(User).where(User.id.in_({a.user_id for a in accs.values()} or {-1})))}
        bots = {(b.bot_id, b.ft_trade_id): b for b in s.scalars(select(BotTrade).where(
            BotTrade.bot_id.in_({t.bot_id for t in trades} or {-1}),
            BotTrade.ft_trade_id.in_({t.ft_trade_id for t in trades} or {-1})))}
        orders: dict[int, list[Order]] = defaultdict(list)
        for o in s.scalars(select(Order).where(Order.trade_id.in_(ids or [-1])).order_by(Order.id)):
            orders[o.trade_id].append(o)
        logs: dict[int, list[ExecutionLog]] = defaultdict(list)
        for lg in s.scalars(select(ExecutionLog).where(ExecutionLog.trade_id.in_(ids or [-1])).order_by(ExecutionLog.id)):
            logs[lg.trade_id].append(lg)
        hooks = {w.id: w.received_at for w in s.scalars(select(WebhookEvent).where(WebhookEvent.id.in_(
            {lg.webhook_event_id for ls in logs.values() for lg in ls if lg.webhook_event_id} or {-1})))}
        for x in [*trades, *accs.values(), *owners.values(), *bots.values(), *[o for v in orders.values() for o in v],
                  *[lg for v in logs.values() for lg in v]]:
            s.expunge(x)

    rows = []
    for t in trades:
        b = bots.get((t.bot_id, t.ft_trade_id))
        acc = accs.get(t.account_id)
        if acc is None:
            continue
        ods = orders.get(t.id, [])
        entry_o = next((o for o in ods if o.kind in ("entry", "adopted")), None)
        exit_os = [o for o in ods if o.kind == "exit"]
        ls = logs.get(t.id, [])
        entry_log = next((lg for lg in ls if lg.action in ENTRY_ACTIONS), None)
        exit_log = next((lg for lg in reversed(ls) if lg.action.startswith("close")), None)
        same_money = t.currency in USD_LIKE  # the bots trade USDT: prices comparable in a dollar coin only
        lev = max(1.0, t.leverage or 1)
        closed = t.status == "closed"
        our_exit = t.exit_price if closed else None
        r: dict[str, Any] = {
            "id": t.id, "pair": t.pair, "symbol": t.symbol, "side": t.side, "status": t.status, "paper": bool(t.dry_run),
            "adopted": bool(t.adopted), "account": acc.label or EXCHANGES.get(acc.exchange, acc.exchange),
            "account_id": acc.id, "owner": (owners.get(acc.user_id).email if owners.get(acc.user_id) else ""),
            "exchange": EXCHANGES.get(acc.exchange, acc.exchange), "cur": t.currency, "lev": lev,
            "bot_id": t.bot_id, "found": b is not None, "comparable": same_money,
            "opened": t.opened_at, "closed": t.closed_at,
            "our_entry": t.entry_price, "our_exit": our_exit, "fees": t.fees or 0.0, "pnl": t.realized_pnl if closed else None,
            "bot_entry": b.open_rate if b else None, "bot_exit": (b.close_rate if b and not b.is_open else None),
            "bot_opened": b.open_date if b else None, "bot_closed": (b.close_date if b and not b.is_open else None),
            **{k: None for k in ("slip_entry", "slip_exit", "delay_entry", "delay_exit", "signal_entry", "signal_exit",
                                 "exec_entry", "exec_exit", "ours_pct", "fees_pct", "bot_pct", "diff_pct")},
        }
        if b is not None and same_money and not t.adopted:
            r["slip_entry"] = _bps(t.entry_price, b.open_rate, t.side, True)
        if b is not None and same_money and closed and not b.is_open:
            r["slip_exit"] = _bps(our_exit, b.close_rate, t.side, False)
        if b is not None and not t.adopted:
            ours_at = entry_o.created_at if entry_o else t.opened_at
            got = hooks.get(entry_log.webhook_event_id) if entry_log and entry_log.webhook_event_id else None
            r["delay_entry"] = _secs(ours_at, b.open_date)
            r["signal_entry"] = _secs(got, b.open_date)      # bot fill -> signal received
            r["exec_entry"] = (entry_log.duration_ms / 1000) if entry_log else None  # our processing incl. the order
        if b is not None and closed and not b.is_open:
            ours_at = exit_os[-1].created_at if exit_os else t.closed_at
            got = hooks.get(exit_log.webhook_event_id) if exit_log and exit_log.webhook_event_id else None
            r["delay_exit"] = _secs(ours_at, b.close_date)
            r["signal_exit"] = _secs(got, b.close_date)
            r["exec_exit"] = (exit_log.duration_ms / 1000) if exit_log else None
        if closed and t.stake:
            r["ours_pct"] = t.realized_pnl / t.stake / lev * 100  # unlevered, net of fees
            r["fees_pct"] = (t.fees or 0) / (t.stake * lev) * 100
        if b is not None and not b.is_open and b.profit_ratio is not None:
            r["bot_pct"] = b.ratio_1x * 100  # Freqtrade's own ratio, net of its fees
        if r.get("ours_pct") is not None and r.get("bot_pct") is not None:
            r["diff_pct"] = r["ours_pct"] - r["bot_pct"]
        rows.append(r)

    def agg(rs: list[dict]) -> dict[str, Any]:
        def avg(k):
            v = [r[k] for r in rs if r.get(k) is not None]
            return sum(v) / len(v) if v else None

        def med(k):
            v = [r[k] for r in rs if r.get(k) is not None]
            return statistics.median(v) if v else None
        closed = [r for r in rs if r["status"] == "closed"]
        return {"trades": len(rs), "closed": len(closed), "matched": sum(1 for r in rs if r["found"]),
                "slip_entry": avg("slip_entry"), "slip_exit": avg("slip_exit"),
                "delay_entry": med("delay_entry"), "delay_exit": med("delay_exit"),
                "signal_entry": med("signal_entry"), "exec_entry": med("exec_entry"),
                "ours_pct": avg("ours_pct"), "bot_pct": avg("bot_pct"), "diff_pct": avg("diff_pct"),
                "fees_pct": avg("fees_pct"), "better": sum(1 for r in rs if (r.get("diff_pct") or 0) > 0.001),
                "worse": sum(1 for r in rs if (r.get("diff_pct") or 0) < -0.001),
                "pnl": sum(r["pnl"] or 0 for r in closed), "fees": sum(r["fees"] for r in rs),
                "cur": rs[0]["cur"] if rs and len({r["cur"] for r in rs}) == 1 else ""}

    by_acc: dict[int, list] = defaultdict(list)
    by_pair: dict[str, list] = defaultdict(list)
    for r in rows:
        by_acc[r["account_id"]].append(r)
        by_pair[r["pair"]].append(r)
    return {"rows": rows, "total": agg(rows),
            "accounts": sorted(({"account": rs[0]["account"], "owner": rs[0]["owner"], "exchange": rs[0]["exchange"],
                                 "id": a, **agg(rs)} for a, rs in by_acc.items()), key=lambda x: -x["trades"]),
            "pairs": sorted(({"pair": p, **agg(rs)} for p, rs in by_pair.items()), key=lambda x: -x["trades"])}
