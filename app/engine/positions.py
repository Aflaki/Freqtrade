"""Positions that are already open on a user's exchange account when the portfolio is switched on (opened by hand,
or by an older Freqtrade copy that is being retired).

- In the pairs the user chose: we take them over ("adopt"): a Trade row is created for the bot the pair comes from,
  so that bot's exit signal closes it. Nothing is bought, nothing is closed.
- In other pairs: listed on the portfolio; the user decides per position: close now, or keep (never touched).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import ExchangeAccount, ExecutionLog, Order, SessionLocal, Trade, User, utcnow
from ..i18n import t
from ..telegram import bot as tg, esc, fmt_num

log = logging.getLogger("relay.positions")


def key_of(symbol: str, side: str) -> str:
    return f"{symbol}|{side}"


def bot_pair(symbol: str, quote: str | None = None, stake: str = "USDT") -> str:
    """The exchange's market -> the bots' pair: KPEPE/USDC:USDC in a USDC portfolio -> 1000PEPE/USDT."""
    pair = symbol.split(":")[0].upper()
    if not quote or "/" not in pair:
        return pair
    base, q = pair.split("/", 1)
    if q != quote.upper():
        return pair  # not this portfolio's coin: shown as it is
    from .markets import base_candidates, wanted_bases
    for fresh in (False, True):  # a coin added within the last minute isn't in the cached list yet
        known = wanted_bases(fresh)
        if base in known:
            return f"{base}/{stake}"
        for b in known:
            if base in base_candidates(b):
                return f"{b}/{stake}"
    return f"{base}/{stake}"


def normalize(raw: list[dict], market=None, quote: str | None = None, stake: str = "USDT") -> list[dict]:
    """ccxt positions -> [{symbol, pair, side, contracts, contract_size, amount, entry, mark, leverage, notional,
    upnl, margin}] for open ones only. `quote`: the portfolio's coin when it isn't the bots' (pair = bots' pair)."""
    out = []
    for p in raw or []:
        contracts = abs(float(p.get("contracts") or 0))
        sym = str(p.get("symbol") or "")
        if not contracts or not sym:
            continue
        side = str(p.get("side") or "").lower()
        if side not in ("long", "short"):
            info = p.get("info") or {}
            amt = float(info.get("positionAmt") or info.get("size") or p.get("contracts") or 0)
            side = "short" if amt < 0 else "long"
        cs = float(p.get("contractSize") or 1)
        entry = float(p.get("entryPrice") or 0)
        mark = float(p.get("markPrice") or 0) or entry
        lev = float(p.get("leverage") or 0)
        lev_known = lev > 0  # stream updates don't carry the leverage
        notional = abs(float(p.get("notional") or 0)) or contracts * cs * mark
        margin = float(p.get("initialMargin") or p.get("collateral") or 0) or (notional / lev if lev else 0)
        if not lev and margin:
            lev = round(notional / margin, 2)
        out.append({"symbol": sym, "pair": bot_pair(sym, quote if quote and quote != stake else None, stake),
                    "side": side, "contracts": contracts,
                    "contract_size": cs, "entry": entry, "mark": mark, "leverage": lev or 1.0,
                    "notional": round(notional, 4), "upnl": float(p.get("unrealizedPnl") or 0),
                    "margin": round(margin, 4), "key": key_of(sym, side), "lev_known": lev_known,
                    "liq": float(p.get("liquidationPrice") or 0) or None})
    return out


def tracked_keys(s: Session, acc_id: int) -> set[str]:
    return {key_of(tr.symbol, tr.side) for tr in s.scalars(
        select(Trade).where(Trade.account_id == acc_id, Trade.status == "open"))}


def bot_for_pair(acc: ExchangeAccount, pair: str, side: str | None = None) -> int | None:
    """The bot whose exit signal should close this pair on this portfolio (same rule as the dispatcher)."""
    chosen = (acc.pair_bot or {}).get(pair)
    if chosen is not None:
        return chosen
    followed = list(acc.bots or [])
    from .botmonitor import monitor
    for b in followed:  # a followed bot that is in this very trade right now
        if any(x["pair"] == pair and (side is None or x["side"] == side)
               for x in (monitor.live.get(b) or {}).get("trades") or []):
            return b
    from .offers import offers_by_pair
    offered = [o for o in offers_by_pair().get(pair) or [] if o.bot_id in followed]
    if offered:
        d = next((o.bot_id for o in offered if o.is_default), None)
        return d if d is not None else min(o.bot_id for o in offered)
    return followed[0] if followed else None


def bot_trade(bot_id: int | None, pair: str, side: str) -> dict | None:
    """The bot's own open trade on this pair (from the live poll), to link our position to it."""
    if bot_id is None:
        return None
    from .botmonitor import monitor
    rows = [x for x in (monitor.live.get(bot_id) or {}).get("trades") or [] if x["pair"] == pair]
    return next((x for x in rows if x["side"] == side), None) or (
        {"opposite": True, **rows[0]} if rows else None)


def adopt(s: Session, acc: ExchangeAccount, user: User, p: dict, bot_id: int | None,
          ft_trade_id: int | None = None, ft_stake: float | None = None) -> Trade:
    """Create the Trade row for a position that is already on the exchange (no order is placed)."""
    margin = p["margin"] or (p["notional"] / p["leverage"] if p["leverage"] else p["notional"])
    tr = Trade(user_id=user.id, account_id=acc.id, ft_trade_id=ft_trade_id, bot_id=bot_id, pair=p["pair"],
               symbol=p["symbol"], side=p["side"], amount=p["contracts"], contract_size=p["contract_size"],
               entry_price=p["entry"] or p["mark"], stake=margin, ft_stake=ft_stake,
               scale=(margin / ft_stake) if ft_stake else None, leverage=p["leverage"],
               enter_tag="adopted", dry_run=False, adopted=True)
    s.add(tr)
    s.flush()
    s.add(Order(trade_id=tr.id, exchange_order_id="", kind="adopted", side="buy" if p["side"] == "long" else "sell",
                amount=p["contracts"], filled=p["contracts"], price=tr.entry_price, fee=0.0, status="adopted"))
    return tr


def can_scan(acc: ExchangeAccount) -> bool:
    return not acc.is_signal and not acc.is_spot and not acc.dry_run


async def sync_account(account_id: int, notify: bool = True) -> dict[str, Any]:
    """Read the account's open positions; adopt those in the chosen pairs, list the others for the user.
    Returns {"adopted": [...], "foreign": [...], "undecided": [...], "error": ""}."""
    from .exchange import pool
    res: dict[str, Any] = {"adopted": [], "foreign": [], "undecided": [], "error": ""}
    t0 = time.monotonic()
    async with pool.lock(account_id):
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, account_id)
            if acc is None or not can_scan(acc):
                return res
            user = s.get(User, acc.user_id)
            client = None
            try:
                client = await pool.get(acc)
                pos = normalize(await client.positions(fresh=True))
            except Exception as e:
                res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log.info("[acc %s] position scan failed: %s", account_id, res["error"])
                if client is not None:
                    s.add(ExecutionLog(user_id=acc.user_id, account_id=acc.id, action="scan_positions", pair="",
                                       status="error", message=res["error"], calls=client.drain_calls(),
                                       duration_ms=int((time.monotonic() - t0) * 1000)))
                    s.commit()
                return res
            have = tracked_keys(s, acc.id)
            held_pairs = {tr.pair for tr in s.scalars(select(Trade).where(Trade.account_id == acc.id,
                                                                          Trade.status == "open"))}
            foreign = []
            for p in pos:
                if p["key"] in have:
                    continue
                if p["pair"] in (acc.pairs or {}) and p["pair"] not in held_pairs:
                    b = bot_for_pair(acc, p["pair"], p["side"])
                    bt = bot_trade(b, p["pair"], p["side"])
                    same = bt is not None and not bt.get("opposite")
                    tr = adopt(s, acc, user, p, b, bt.get("ft_trade_id") if same else None,
                               bt.get("stake_amount") if same else None)
                    if acc.stoploss_pct:  # the portfolio's protective stop, as for trades we open
                        from .trader import _refresh_stop
                        await _refresh_stop(s, acc, user, client, tr)
                    held_pairs.add(p["pair"])
                    res["adopted"].append({**p, "trade_id": tr.id, "bot_id": b,
                                           "match": "same" if same else ("opposite" if bt else "none")})
                else:
                    foreign.append(p)
            now_keys = {p["key"] for p in foreign}
            acc.kept_positions = [k for k in (acc.kept_positions or []) if k in now_keys] or None
            acc.foreign_positions = foreign or None
            acc.positions_checked_at = utcnow()
            res["foreign"] = foreign
            res["undecided"] = acc.undecided_positions
            s.add(ExecutionLog(user_id=acc.user_id, account_id=acc.id, action="scan_positions", pair="",
                               status="ok", message=f"adopted {len(res['adopted'])}, other {len(foreign)}"[:1000],
                               calls=client.drain_calls(), duration_ms=int((time.monotonic() - t0) * 1000)))
            s.commit()
            if notify and user is not None:
                _notify(user, acc, res)
    return res


def _notify(user: User, acc: ExchangeAccount, res: dict) -> None:
    L = user.lang
    name = acc.label or acc.exchange
    if res["adopted"]:
        lines = "\n".join(f"• <b>{esc(p['pair'])}</b> {'🟢 LONG' if p['side'] == 'long' else '🔴 SHORT'} "
                          f"@ {fmt_num(p['entry'])}" for p in res["adopted"])
        tg.notify(user, "entry_fill", t(L, "pos_tg_adopted", acc=esc(name), lines=lines))
    if res["undecided"]:
        lines = "\n".join(f"• <b>{esc(p['pair'])}</b> {p['side'].upper()}" for p in res["undecided"])
        tg.notify(user, "errors", t(L, "pos_tg_ask", acc=esc(name), lines=lines))


async def decide(account_id: int, decisions: dict[str, str]) -> dict[str, list]:
    """decisions: {"SYMBOL|side": "close" | "keep"} for positions outside the chosen pairs.
    close = reduce-only market order now; keep = never touch it (to let a bot manage it instead, the user adds the
    pair to the portfolio: the next save takes it over)."""
    from .exchange import pool
    out: dict[str, list] = {"closed": [], "kept": [], "failed": []}
    t0 = time.monotonic()
    async with pool.lock(account_id):
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, account_id)
            if acc is None or not can_scan(acc):
                return out
            listed = {p["key"]: p for p in acc.foreign_positions or []}
            kept = set(acc.kept_positions or [])
            client = await pool.get(acc)
            closed_keys = set()
            for k, what in decisions.items():
                p = listed.get(k)
                if p is None:
                    continue
                if what == "keep":
                    kept.add(k)
                    out["kept"].append(p)
                elif what == "close":
                    try:
                        fill = await client.market_order(p["symbol"], "sell" if p["side"] == "long" else "buy",
                                                         p["contracts"], reduce_only=True, price_hint=p["mark"])
                        closed_keys.add(k)
                        kept.discard(k)
                        out["closed"].append({**p, "price": fill.price})
                    except Exception as e:
                        out["failed"].append({**p, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            acc.kept_positions = sorted(kept) or None
            acc.foreign_positions = [p for p in acc.foreign_positions or [] if p["key"] not in closed_keys] or None
            msg = f"closed {len(out['closed'])}, kept {len(out['kept'])}, failed {len(out['failed'])}"
            s.add(ExecutionLog(user_id=acc.user_id, account_id=acc.id, action="close_foreign", pair="",
                               status="error" if out["failed"] else "ok", message=msg, calls=client.drain_calls(),
                               duration_ms=int((time.monotonic() - t0) * 1000)))
            s.commit()
    return out
