"""Admin → Portfolios: every user's portfolio with its connection and reconciliation state; the connection test and
the reconciliation run for one portfolio or for many at once (the page calls them one by one and shows each result).
Also: which offered pairs an exchange can trade in a market and coin (e.g. Hyperliquid futures in USDC)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from ..db import ExchangeAccount, SessionLocal, Trade, User, get_global
from ..engine import markets
from ..engine.exchange import EXCHANGES, QUOTES, SPOT_ONLY
from ..security import form_with_csrf, require_admin
from .common import render

router = APIRouter(prefix="/admin/portfolios")


@router.get("")
async def portfolios(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        accs = list(s.scalars(select(ExchangeAccount).where(ExchangeAccount.kind == "exchange")
                              .order_by(ExchangeAccount.user_id, ExchangeAccount.id)))
        users = {u.id: u for u in s.scalars(select(User))}
        opened = dict(s.execute(select(Trade.account_id, func.count(Trade.id)).where(Trade.status == "open")
                                .group_by(Trade.account_id)).all())
        gs = get_global(s)
        for a in accs:
            s.expunge(a)
        for u in users.values():
            s.expunge(u)
    return render(request, "admin/portfolios.html", tab="portfolios", accs=accs, users=users, opened=opened,
                  stake=gs.stake_currency, avail=_availability(request))


@router.post("/{acc_id}/recon")
async def recon_one(acc_id: int, request: Request):
    """Compare one portfolio with its bots now (reads the exchange); the decisions stay on its own page."""
    require_admin(request)
    await form_with_csrf(request)
    from ..engine import reconcile
    from ..engine.positions import can_scan
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        ok = acc is not None and can_scan(acc)
    if not ok:
        return JSONResponse({"skip": True})
    try:
        res = await reconcile.compare(acc_id)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:160]}"})
    return JSONResponse({"open": res.get("open", 0), "diffs": len(res.get("diffs", [])), "bot": res.get("bot_total", 0),
                         "exchange": res.get("exchange_total", 0), "matched": res.get("matched", 0),
                         "error": res.get("error", "")})


def _availability(request: Request) -> dict | None:
    """?ex=hyperliquid&mode=futures&quote=USDC -> each offered pair on that market: tradable or why not."""
    qp = request.query_params
    ex = qp.get("ex", "")
    if ex not in EXCHANGES:
        return None
    mode = "spot" if qp.get("mode") == "spot" or ex in SPOT_ONLY else "futures"
    cache = markets.all_info()
    with SessionLocal() as s:
        gs = get_global(s)
        stake = gs.stake_currency
        allowed = ex in (gs.allowed_exchanges or [])
    have = markets.quotes_on(ex, cache, mode=mode)
    quote = (qp.get("quote") or "").upper() or (stake if stake in have or not have else have[0])
    from ..engine.offers import bot_leverage, offers_by_pair
    rows = []
    known = markets.known(ex, mode, cache)
    for pair, offers in sorted(offers_by_pair().items()):
        offers = [o for o in offers if o.enabled]
        if not offers:
            continue
        i = markets.market_info(pair, quote, ex, mode, cache)
        levs = sorted({int(o.leverage or bot_leverage(o.bot_id) or 1) for o in offers}) if mode == "futures" else [1]
        mx = (i or {}).get("max_leverage")
        need = markets.min_order(i)
        vol = (i or {}).get("volume")
        if not known:
            status, why = "unknown", ("ap_why_unknown", {})
        elif i is None:
            status, why = "no", ("ap_why_not_listed", {"q": quote})
        elif not i.get("active", True):
            status, why = "no", ("ap_why_closed", {})
        elif mx and max(levs) > mx:
            status, why = "no", ("ap_why_lev", {"lev": max(levs), "max": int(mx)})
        elif vol is not None and vol < markets.SHALLOW_VOLUME:
            status, why = "thin", ("ap_st_thin", {})
        else:
            status, why = "ok", ("", {})
        rows.append({"pair": pair, "symbol": (i or {}).get("symbol", ""), "status": status, "why": why, "levs": levs,
                     "max_lev": mx, "min_order": need, "volume": vol, "price": (i or {}).get("price")})
    return {"ex": ex, "mode": mode, "quote": quote, "quotes": have or list(QUOTES), "allowed": allowed, "known": known,
            "rows": rows, "ok": sum(1 for r in rows if r["status"] in ("ok", "thin")),
            "no": sum(1 for r in rows if r["status"] == "no"), "at": (cache.get(ex) or {}).get("at", "")}
