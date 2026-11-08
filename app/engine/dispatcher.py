"""Fans one signal out to every eligible portfolio and records the result on the webhook event."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

from sqlalchemy import select

from ..db import ExchangeAccount, SessionLocal, Trade, User, WebhookEvent, get_global, utcnow
from ..signal import Signal, canonical_pair
from .botmonitor import monitor
from .trader import Outcome, run_for_account

log = logging.getLogger("relay.dispatch")
_seen: dict[str, float] = {}


def is_duplicate(sig: Signal, ttl: int) -> bool:
    now = time.monotonic()
    for k in [k for k, ts in _seen.items() if now - ts > ttl]:
        _seen.pop(k, None)
    if sig.id in _seen:
        return True
    _seen[sig.id] = now
    return False


def eligible_accounts(sig: Signal, pair: str, bot_id: int) -> list[int]:
    with SessionLocal() as s:
        gs = get_global(s)
        if sig.action in ("long", "short"):
            if not gs.entries_enabled or pair not in gs.allowed_pairs:
                return []
            rows = s.execute(select(ExchangeAccount, User).join(User, User.id == ExchangeAccount.user_id).where(
                User.status == "active", User.trading_enabled.is_(True), ExchangeAccount.enabled.is_(True))).all()
            out = []
            for a, u in rows:
                if not u.subscription_active or pair not in (a.pairs or {}) or bot_id not in (a.bots or []):
                    continue
                if a.is_signal and not gs.signal_only_enabled:
                    continue
                if not a.is_signal and a.exchange not in gs.allowed_exchanges:
                    continue
                out.append(a.id)
            return out
        # exits: every portfolio that holds the pair, whatever its current status
        return list(s.scalars(select(Trade.account_id).where(Trade.pair == pair, Trade.status == "open",
                                                             Trade.bot_id == bot_id).distinct()))


async def dispatch(sig: Signal, event_id: int | None, bot_id: int, bot_name: str = "") -> list[Outcome]:
    with SessionLocal() as s:
        pair = canonical_pair(sig, get_global(s).allowed_pairs)
    results: list[Outcome] = []
    if not pair:
        summary = f"pair {sig.symbol} not recognised"
    else:
        ids = eligible_accounts(sig, pair, bot_id)
        log.info("signal %s %s -> %d account(s)", sig.action, pair, len(ids))
        results = list(await asyncio.gather(*(run_for_account(i, sig, pair, event_id, bot_id) for i in ids)))
        c = Counter(r.status for r in results)
        summary = ", ".join(f"{k}: {v}" for k, v in c.items()) or "no eligible accounts"
    try:
        monitor.report_trade(bot_name, sig, pair, results)
    except Exception:
        log.exception("could not report bot trade to admins")
    if event_id:
        with SessionLocal() as s:
            e = s.get(WebhookEvent, event_id)
            if e:
                e.pair = pair or sig.symbol
                e.accounts = len(results)
                e.summary = summary[:500]
                e.finished_at = utcnow()
                s.commit()
    return results
