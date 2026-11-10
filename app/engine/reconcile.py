"""The bots' open trades vs the positions really open on a user's exchange account, pair by pair.

For every pair the portfolio follows, the bot it follows that pair from either is in a trade or not, and the
exchange either holds a position or not:
  matched     both, same direction
  exch_only   a position on the exchange, the bot has no trade   -> close now / keep (wait for the bot's next exit)
  opposite    both, opposite directions                          -> close now / keep
  bot_only    the bot is in a trade, the account isn't            -> open now (catch up) / skip
Positions in pairs the portfolio doesn't follow are listed by the positions page itself (close / keep).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from ..db import BotTrade, ExchangeAccount, SessionLocal, Trade, utcnow
from .positions import bot_for_pair, can_scan


def bot_open_trades(bot_id: int) -> list[dict]:
    """The bot's open trades: the live poll of its API, else the open trades its webhooks reported."""
    from .botmonitor import monitor
    live = monitor.live.get(bot_id)
    if live and live.get("at") is not None:
        return list(live.get("trades") or [])
    with SessionLocal() as s:
        rows = s.scalars(select(BotTrade).where(BotTrade.bot_id == bot_id, BotTrade.is_open.is_(True)))
        return [{"ft_trade_id": r.ft_trade_id, "pair": r.pair, "side": r.side, "leverage": r.leverage,
                 "open_rate": r.open_rate, "open_date": r.open_date, "stake_amount": r.stake_amount} for r in rows]


def key(pair: str, side: str, kind: str) -> str:
    return f"{pair}|{side}|{kind}"


async def compare(acc_id: int) -> dict[str, Any]:
    from . import livepos
    from .botmonitor import monitor
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        if acc is None or not can_scan(acc):
            return {}
        s.expunge(acc)
        ours = {(t.pair, t.side): t for t in s.scalars(select(Trade).where(Trade.account_id == acc_id,
                                                                         Trade.status == "open"))}
        for t in ours.values():
            s.expunge(t)
    st = await livepos.refresh(acc_id)
    on_ex: dict[str, dict] = {}
    for p in st["rows"].values():
        on_ex.setdefault(p["pair"], p)
    followed = set(acc.pairs or {})
    by_bot: dict[int, list[dict]] = {}
    rows: list[dict] = []
    bot_total = 0
    outside_bot = 0
    for b in sorted(set(acc.bots or []) | set((acc.pair_bot or {}).values())):
        by_bot[b] = bot_open_trades(b)
        outside_bot += sum(1 for x in by_bot[b] if x["pair"] not in followed)
    for pair in sorted(followed):
        b = bot_for_pair(acc, pair)
        bt = next((x for x in by_bot.get(b, []) if x["pair"] == pair), None) if b is not None else None
        p = on_ex.get(pair)
        if bt:
            bot_total += 1
        if bt is None and p is None:
            continue
        kind = ("matched" if bt["side"] == p["side"] else "opposite") if bt and p else ("bot_only" if bt else "exch_only")
        side = (p or bt)["side"]
        rows.append({"pair": pair, "kind": kind, "side": side, "bot_id": b, "bot": bt, "pos": p,
                     "trade_id": getattr(ours.get((pair, side)), "id", None), "key": key(pair, side, kind)})
    ack = set(acc.recon_ack or [])
    diffs = [r for r in rows if r["kind"] != "matched"]
    for r in diffs:
        r["ack"] = r["key"] in ack
    out = {"rows": rows, "diffs": diffs, "bot_total": bot_total, "outside_bot": outside_bot,
           "exchange_total": len(st["rows"]), "matched": sum(1 for r in rows if r["kind"] == "matched"),
           "outside_exchange": sum(1 for p in st["rows"].values() if p["pair"] not in followed),
           "error": st.get("error", ""), "bots_stale": [b for b in by_bot if (monitor.live.get(b) or {}).get("error")],
           "open": sum(1 for r in diffs if not r["ack"])}
    with SessionLocal() as s:  # for the badge on the portfolio card, without asking the exchange again
        a = s.get(ExchangeAccount, acc_id)
        a.recon_summary = {"open": out["open"], "bot": bot_total, "exchange": out["exchange_total"],
                           "at": utcnow().isoformat(timespec="seconds")}
        a.recon_ack = [k for k in a.recon_ack or [] if k in {r["key"] for r in diffs}] or None
        s.commit()
    return out


async def act(acc_id: int, user_id: int, decisions: dict[str, str]) -> dict[str, list]:
    """decisions: {row key: "close" | "keep" | "open" | "skip"}. Returns what happened, per row."""
    from ..signal import Signal
    from .trader import force_exit, run_for_account
    res = await compare(acc_id)
    rows = {r["key"]: r for r in res.get("diffs", [])}
    done: dict[str, list] = {"closed": [], "opened": [], "kept": [], "failed": []}
    ack: set[str] = set()
    for k, what in decisions.items():
        r = rows.get(k)
        if r is None:
            continue
        if what in ("keep", "skip"):
            ack.add(k)
            done["kept"].append(r)
        elif what == "close" and r["kind"] in ("exch_only", "opposite") and r["pos"]:
            p = r["pos"]
            if r["trade_id"]:
                out = await force_exit(r["trade_id"], user_id)
                ok = out is not None and out.status in ("closed", "partial")
            else:  # not tracked by us: plain reduce-only close
                from .exchange import pool
                async with pool.lock(acc_id):
                    with SessionLocal() as s:
                        acc = s.get(ExchangeAccount, acc_id)
                        client = await pool.get(acc)
                    try:
                        await client.market_order(p["symbol"], "sell" if p["side"] == "long" else "buy", p["contracts"],
                                                  reduce_only=True, price_hint=p["mark"])
                        ok = True
                    except Exception as e:
                        ok = False
                        r["error"] = f"{type(e).__name__}: {str(e)[:150]}"
            (done["closed"] if ok else done["failed"]).append(r)
        elif what == "open" and r["kind"] == "bot_only":
            bt = r["bot"]
            sig = Signal(action=bt["side"], symbol=r["pair"].replace("/", ""), pair=r["pair"],
                         ft_trade_id=bt.get("ft_trade_id"), stake=bt.get("stake_amount"), leverage=bt.get("leverage"),
                         enter_tag="catch-up", id=f"catchup-{acc_id}-{r['pair']}-{dt.datetime.now().timestamp()}")
            out = await run_for_account(acc_id, sig, r["pair"], None, r["bot_id"])
            r["message"] = out.message
            (done["opened"] if out.status in ("opened", "adopted") else done["failed"]).append(r)
    if ack:
        with SessionLocal() as s:
            a = s.get(ExchangeAccount, acc_id)
            a.recon_ack = sorted(set(a.recon_ack or []) | ack)
            s.commit()
    from . import livepos
    livepos._cache.pop(acc_id, None)
    return done
