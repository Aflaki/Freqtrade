"""Public pages: landing with the bot's verified performance, language switch."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from sqlalchemy import select

from ..db import Bot, SessionLocal, User, get_global
from ..engine import performance as perf
from ..i18n import LANGS
from ..security import current_user
from .common import render

router = APIRouter()
PERIODS = {"30": 30, "90": 90, "365": 365, "all": None}


@router.get("/")
async def landing(request: Request):
    market = {"long": "long", "short": "short", "spot": "long"}.get(request.query_params.get("market", ""), "all")
    period = request.query_params.get("days", "90")
    days = PERIODS.get(period, 90)
    with SessionLocal() as s:
        gs = get_global(s)
        bots = s.scalars(select(Bot).where(Bot.enabled.is_(True), Bot.public.is_(True)).order_by(Bot.sort, Bot.id)).all()
    ids = [b.id for b in bots]
    sel = request.query_params.get("bot", "all")
    chosen = [int(sel)] if sel.isdigit() and int(sel) in ids else ids
    sel = str(chosen[0]) if len(chosen) == 1 and sel.isdigit() else "all"
    show = gs.public_performance or (current_user(request) or User(is_admin=False)).is_admin
    data = perf.summary(market, days, bots=chosen) if show else None
    pairs = perf.pair_stats(market, days, chosen) if show else {}
    cards = perf.bot_summaries(ids, market, days) if show else {}
    return render(request, "landing.html", market=market, period=period, periods=list(PERIODS), data=data,
                  pairs=pairs, show=show, bots=bots, cards=cards, bot_sel=sel)


@router.get("/lang/{code}")
async def set_lang(code: str, request: Request):
    if code not in LANGS:
        code = "en"
    u = current_user(request)
    if u:
        with SessionLocal() as s:
            s.get(User, u.id).lang = code
            s.commit()
    back = request.headers.get("referer") or "/"
    if not back.startswith(str(request.base_url)):
        back = "/"
    resp = RedirectResponse(back, 303)
    resp.set_cookie("lang", code, max_age=365 * 24 * 3600, samesite="lax")
    return resp
