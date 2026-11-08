"""Fans one signal out to every eligible user account."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

from sqlalchemy import select

from ..db import ExchangeAccount, SessionLocal, SignalLog, Trade, User, get_global
from ..signal import Signal, canonical_pair
from .trader import Outcome, run_for_account

log = logging.getLogger("relay.dispatch")

_seen: dict[str, float] = {}


def is_duplicate(sig: Signal, ttl: int) -> bool:
    now = time.monotonic()
    for k in [k for k, t in _seen.items() if now - t > ttl]:
        _seen.pop(k, None)
    if sig.id in _seen:
        return True
    _seen[sig.id] = now
    return False


def eligible_accounts(sig: Signal, pair: str) -> list[int]:
    with SessionLocal() as s:
        gs = get_global(s)
        if sig.action in ("long", "short"):
            # NEW entries: global switch, active user, user not paused, account enabled, pair selected
            if not gs.entries_enabled:
                return []
            rows = s.execute(
                select(ExchangeAccount, User).join(User, User.id == ExchangeAccount.user_id).where(
                    User.status == "active", User.trading_enabled.is_(True), ExchangeAccount.enabled.is_(True),
                    ExchangeAccount.capital > 0)).all()
            return [a.id for a, _ in rows if pair in (a.pairs or {}) and pair in gs.allowed_pairs
                    and a.exchange in gs.allowed_exchanges]
        # EXITS always run for every account that holds this pair, even if paused/disabled,
        # so nobody is left with an orphaned position.
        return list(s.scalars(select(Trade.account_id).where(Trade.pair == pair, Trade.status == "open").distinct()))


async def dispatch(sig: Signal) -> list[Outcome]:
    with SessionLocal() as s:
        gs = get_global(s)
        pair = canonical_pair(sig, gs.allowed_pairs)
        entry = SignalLog(action=sig.action, pair=pair or sig.symbol, payload=sig.raw)
        s.add(entry)
        s.commit()
        log_id = entry.id
    if not pair:
        summary = f"pair {sig.symbol} not recognised"
        results: list[Outcome] = []
    else:
        ids = eligible_accounts(sig, pair)
        log.info("signal %s %s -> %d account(s)", sig.action, pair, len(ids))
        results = list(await asyncio.gather(*(run_for_account(i, sig, pair) for i in ids)))
        c = Counter(r.status for r in results)
        summary = ", ".join(f"{k}: {v}" for k, v in c.items()) or "no eligible accounts"
    with SessionLocal() as s:
        e = s.get(SignalLog, log_id)
        e.summary = summary[:500]
        s.commit()
    return results
