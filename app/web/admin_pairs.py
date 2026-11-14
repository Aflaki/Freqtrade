"""Admin → Pair catalog: every pair of every bot, with its performance per bot and the exchanges' rules.
The admin decides which pairs are offered to users, from which bots, and the per-pair rules."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from ..db import Bot, BotGroup, PairOffer, SessionLocal, get_global
from ..engine import markets
from ..engine import performance as perf
from ..engine.exchange import EXCHANGES
from ..engine.offers import bot_pairs, sync_allowed_pairs
from ..security import flash, form_with_csrf, require_admin
from .common import render, tr

router = APIRouter(prefix="/admin/pairs")
PERIODS = {"30": 30, "90": 90, "365": 365, "all": None}


def catalog(days: int | None) -> dict:
    with SessionLocal() as s:
        gs = get_global(s)
        bots = list(s.scalars(select(Bot).where(Bot.enabled.is_(True)).order_by(Bot.sort, Bot.id)))
        groups = {g.id: g.name for g in s.scalars(select(BotGroup))}
        offers = {(o.pair, o.bot_id): o for o in s.scalars(select(PairOffer))}
        exchanges = list(gs.allowed_exchanges or [])
        legacy = [p for p in gs.allowed_pairs or [] if not any(k[0] == p for k in offers)]
        cache = gs.market_cache or {}
    traded = {b.id: set(bot_pairs(b)) for b in bots}
    stats = perf.pair_matrix("all", days, bots)
    names = {b.id: b.name for b in bots}
    pairs = sorted(set().union(*traded.values(), {k[0] for k in offers}, legacy) if bots else {k[0] for k in offers})
    rows = []
    for p in pairs:
        cells = []
        nobody = not any(p in traded[b.id] for b in bots)  # e.g. from the old list: let the admin pick any bot
        for b in bots:
            o = offers.get((p, b.id))
            if p not in traded[b.id] and o is None and not nobody:
                continue
            st = (stats.get(p) or {}).get(b.id)
            cells.append({"bot": b, "group": groups.get(b.group_id, ""), "o": o, "live": p in traded[b.id], "st": st})
        cells.sort(key=lambda c: -((c["st"] or {}).get("profit_abs") or 0))
        ex = {}
        for e in exchanges:
            known = (cache.get(e) or {}).get("pairs")
            if not known:  # rules of this exchange not loaded (yet): unknown, not "not listed"
                ex[e] = {"unknown": True}
                continue
            i = known.get(p)
            ex[e] = {"i": i, "min_order": markets.min_order(i)} if i else None
        offered = [c for c in cells if c["o"] and c["o"].enabled]
        rows.append({"pair": p, "cells": cells, "ex": ex, "offered": len(offered),
                     "mandatory": any(c["o"] and c["o"].mandatory for c in cells), "legacy": p in legacy,
                     "best": max((c["st"]["profit_abs"] for c in cells if c["st"]), default=None)})
    return {"rows": rows, "bots": bots, "names": names, "exchanges": exchanges, "cache": cache,
            "groups": sorted({g for g in groups.values()}),
            "n_offered": sum(1 for r in rows if r["offered"]), "EXCHANGES": EXCHANGES}


@router.get("")
async def page(request: Request):
    require_admin(request)
    period = request.query_params.get("days", "90")
    period = period if period in PERIODS else "90"
    return render(request, "admin/pairs.html", tab="pairs", period=period, periods=list(PERIODS),
                  **catalog(PERIODS[period]))


def _get_offer(s, pair: str, bot_id: int) -> PairOffer:
    o = s.scalars(select(PairOffer).where(PairOffer.pair == pair, PairOffer.bot_id == bot_id)).first()
    if o is None:
        if s.get(Bot, bot_id) is None:
            raise HTTPException(404, "bot")
        o = PairOffer(pair=pair, bot_id=bot_id)
        s.add(o)
        s.flush()
    return o


@router.post("/set")
async def set_field(request: Request):
    """Autosave of one field: {csrf, pair, bot_id, field, value}."""
    require_admin(request)
    data = await request.json()
    if not hmac.compare_digest(str(data.get("csrf", "")), request.session.get("csrf", "")):
        raise HTTPException(400, "CSRF")
    pair, field, value = str(data.get("pair", "")).upper()[:40], str(data.get("field", "")), data.get("value")
    if "/" not in pair:
        raise HTTPException(400, "pair")
    with SessionLocal() as s:
        if field == "mandatory":  # pair level: all its offers
            rows = list(s.scalars(select(PairOffer).where(PairOffer.pair == pair)))
            if not rows:
                raise HTTPException(400, "offer the pair from a bot first")
            for o in rows:
                o.mandatory = bool(value)
        else:
            try:
                bot_id = int(data.get("bot_id"))
            except (TypeError, ValueError):
                raise HTTPException(400, "bot")
            o = _get_offer(s, pair, bot_id)
            if field == "enabled":
                o.enabled = bool(value)
                if not o.enabled:
                    o.is_default = False
            elif field == "is_default":
                for x in s.scalars(select(PairOffer).where(PairOffer.pair == pair)):
                    x.is_default = False
                o.is_default, o.enabled = bool(value), o.enabled or bool(value)
            elif field == "min_capital":
                v = _float(value)
                o.min_capital = v if v and v > 0 else None
            elif field == "leverage":
                v = _float(value)
                o.leverage = int(v) if v and 1 <= v <= 125 else None
            else:
                raise HTTPException(400, "field")
        sync_allowed_pairs(s)
        s.commit()
    return JSONResponse({"ok": True})


def _float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


@router.post("/apply")
async def apply_rows(request: Request):
    """Bulk action on the selected rows: {csrf, rows: [[pair, bot_id], ...], action, value}."""
    require_admin(request)
    data = await request.json()
    if not hmac.compare_digest(str(data.get("csrf", "")), request.session.get("csrf", "")):
        raise HTTPException(400, "CSRF")
    act, value = str(data.get("action", "")), data.get("value")
    rows = [(str(p).upper()[:40], int(b)) for p, b in (data.get("rows") or [])[:5000]
            if isinstance(p, str) and "/" in p and str(b).isdigit()]
    if not rows:
        raise HTTPException(400, "no rows")
    with SessionLocal() as s:
        if act == "default":  # one default per pair: the last selected row of each pair wins
            chosen = {p: b for p, b in rows}
            for p, b in chosen.items():
                for x in s.scalars(select(PairOffer).where(PairOffer.pair == p)):
                    x.is_default = False
                o = _get_offer(s, p, b)
                o.is_default, o.enabled = True, True
        else:
            for p, b in rows:
                o = _get_offer(s, p, b)
                if act == "offer":
                    o.enabled = True
                elif act == "withdraw":
                    o.enabled = o.is_default = False
                elif act in ("mandatory", "optional"):  # pair level
                    for x in s.scalars(select(PairOffer).where(PairOffer.pair == p)):
                        x.mandatory = act == "mandatory"
                    o.mandatory = act == "mandatory"
                elif act == "leverage":
                    v = _float(value)
                    o.leverage = int(v) if v and 1 <= v <= 125 else None
                elif act == "min_capital":
                    v = _float(value)
                    o.min_capital = v if v and v > 0 else None
                else:
                    raise HTTPException(400, "action")
        sync_allowed_pairs(s)
        s.commit()
    return JSONResponse({"ok": True, "n": len(rows)})


@router.post("/refresh/{exchange}")
async def refresh_one(exchange: str, request: Request):
    """Reload one exchange's market rules (the page calls this exchange by exchange and shows the progress)."""
    import time
    require_admin(request)
    await form_with_csrf(request)
    if exchange not in EXCHANGES:
        return JSONResponse({"ok": False, "error": "unknown exchange"}, 400)
    t0 = time.monotonic()
    try:
        cache = await markets.refresh([exchange])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
    c = cache.get(exchange) or {}
    return JSONResponse({"ok": not c.get("error"), "error": c.get("error", ""), "at": c.get("at", ""),
                         "futures": len(c.get("pairs") or {}), "perps": len(c.get("fut") or {}),
                         "spot": len(c.get("spot") or {}), "quotes": markets.quotes_on(exchange, cache),
                         "fut_quotes": markets.quotes_on(exchange, cache, mode="futures"),
                         "secs": round(time.monotonic() - t0, 1)})


@router.post("/bulk")
async def bulk(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    act = str(f.get("action", ""))
    period = str(f.get("days", "90"))
    days = PERIODS.get(period, 90)
    if act == "refresh_markets":
        cache = await markets.refresh()
        errs = [f"{k}: {v['error']}" for k, v in cache.items() if v.get("error")]
        flash(request, tr(request, "pc_markets_done") + (" — " + "; ".join(errs) if errs else ""), "warn" if errs else "ok")
        return RedirectResponse("/admin/pairs", 303)
    with SessionLocal() as s:
        bots = list(s.scalars(select(Bot).where(Bot.enabled.is_(True))))
        if act in ("offer_all", "offer_bot"):
            only = int(f.get("bot_id")) if act == "offer_bot" and str(f.get("bot_id", "")).isdigit() else None
            for b in bots:
                if only and b.id != only:
                    continue
                for p in bot_pairs(b):
                    _get_offer(s, p, b.id).enabled = True
        elif act == "withdraw_all":
            for o in s.scalars(select(PairOffer)):
                o.enabled = o.is_default = False
        elif act == "best_default":  # the most profitable offered bot of each pair becomes its default
            stats = perf.pair_matrix("all", days, bots)
            by_pair: dict[str, list[PairOffer]] = {}
            for o in s.scalars(select(PairOffer).where(PairOffer.enabled.is_(True))):
                by_pair.setdefault(o.pair, []).append(o)
            for p, offers in by_pair.items():
                best = max(offers, key=lambda o: ((stats.get(p) or {}).get(o.bot_id) or {}).get("profit_abs", -1e18))
                for o in offers:
                    o.is_default = o is best
        else:
            raise HTTPException(400, "action")
        sync_allowed_pairs(s)
        s.commit()
    flash(request, tr(request, "msg_done"))
    return RedirectResponse(f"/admin/pairs?days={period}", 303)
