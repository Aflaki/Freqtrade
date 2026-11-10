"""Pair catalog: which pairs the admin offers to users, from which bots, with which rules.

A pair with offer rows may only be traded from its enabled offers, and each portfolio takes it from exactly one bot.
A pair without any rows keeps the old behaviour (allowed list only), so existing setups keep working.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from ..db import Bot, PairOffer, SessionLocal, canon_pair, get_global


def offers_by_pair(enabled_only: bool = True) -> dict[str, list[PairOffer]]:
    with SessionLocal() as s:
        q = select(PairOffer).order_by(PairOffer.pair, PairOffer.id)
        if enabled_only:
            q = q.where(PairOffer.enabled.is_(True))
        out: dict[str, list[PairOffer]] = defaultdict(list)
        for o in s.scalars(q):
            out[o.pair].append(o)
    return dict(out)


def pair_rules(offers: list[PairOffer]) -> dict:
    """Pair-level view of its offers: mandatory, default bot, minimum capital, leverage per bot."""
    default = next((o.bot_id for o in offers if o.is_default), offers[0].bot_id if len(offers) == 1 else None)
    return {"mandatory": any(o.mandatory for o in offers), "default_bot": default,
            "bots": [o.bot_id for o in offers], "min_capital": {o.bot_id: o.min_capital for o in offers},
            "leverage": {o.bot_id: o.leverage for o in offers}}


def offer_for(s, pair: str, bot_id: int) -> PairOffer | None:
    return s.scalars(select(PairOffer).where(PairOffer.pair == pair, PairOffer.bot_id == bot_id)).first()


def pair_is_managed(s, pair: str) -> bool:
    return s.scalar(select(PairOffer.id).where(PairOffer.pair == pair).limit(1)) is not None


def bot_may_send(s, pair: str, bot_id: int) -> bool:
    """Entry signals: a managed pair only from its enabled offers; an unmanaged pair from any bot (old behaviour)."""
    if not pair_is_managed(s, pair):
        return True
    o = offer_for(s, pair, bot_id)
    return bool(o and o.enabled)


def sync_allowed_pairs(s) -> list[str]:
    """allowed_pairs = pairs with an enabled offer + unmanaged pairs the admin listed the old way."""
    gs = get_global(s)
    managed = {p for (p,) in s.execute(select(PairOffer.pair).distinct())}
    enabled = {p for (p,) in s.execute(select(PairOffer.pair).where(PairOffer.enabled.is_(True)).distinct())}
    legacy = {p for p in (gs.allowed_pairs or []) if p not in managed}
    gs.allowed_pairs = sorted(enabled | legacy)
    return gs.allowed_pairs


def bot_pairs(b: Bot) -> list[str]:
    """Pairs a bot trades now (its Freqtrade whitelist), after its own pair filter."""
    return sorted({canon_pair(p) for p in ((b.state or {}).get("whitelist") or []) if b.allows(p)})


def migrate_offers() -> None:
    """Once: turn the old allowed-pairs list into offers (from the bots that actually trade each pair)."""
    with SessionLocal() as s:
        gs = get_global(s)
        if gs.offers_migrated:
            return
        bots = list(s.scalars(select(Bot).where(Bot.enabled.is_(True), Bot.public.is_(True))))
        traded = {b.id: set(bot_pairs(b)) for b in bots}
        for pair in gs.allowed_pairs or []:
            ids = [b.id for b in bots if pair in traded[b.id]]
            if not ids:  # no whitelist known yet: leave it unmanaged (old behaviour) until the admin sets it
                continue
            for i in ids:
                s.add(PairOffer(pair=pair, bot_id=i, enabled=True, is_default=len(ids) == 1))
        gs.offers_migrated = True
        s.commit()


def forced_leverage(pair: str, bot_id: int | None) -> int | None:
    """The admin's fixed leverage for a pair from a bot (or, without a chosen bot, from the pair's only/default offer)."""
    with SessionLocal() as s:
        offers = [o for o in s.scalars(select(PairOffer).where(PairOffer.pair == pair, PairOffer.enabled.is_(True)))]
    if bot_id is not None:
        offers = [o for o in offers if o.bot_id == bot_id]
    elif len(offers) > 1:
        offers = [o for o in offers if o.is_default] or offers[:1]
    return offers[0].leverage if offers and offers[0].leverage else None


def bot_leverage(bot_id: int) -> int | None:
    """The leverage this bot usually trades with (most common among its last 100 trades)."""
    from collections import Counter

    from ..db import BotTrade
    with SessionLocal() as s:
        levs = [round(x or 1) for x in s.scalars(select(BotTrade.leverage).where(BotTrade.bot_id == bot_id)
                                                     .order_by(BotTrade.id.desc()).limit(100))]
    return Counter(levs).most_common(1)[0][0] if levs else None
