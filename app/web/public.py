"""Public pages: landing with the bot's verified performance, language switch."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..db import SessionLocal, User, get_global
from ..engine import performance as perf
from ..i18n import LANGS
from ..security import current_user
from .common import render

router = APIRouter()
PERIODS = {"30": 30, "90": 90, "365": 365, "all": None}


@router.get("/")
async def landing(request: Request):
    market = "spot" if request.query_params.get("market") == "spot" else "futures"
    period = request.query_params.get("days", "90")
    days = PERIODS.get(period, 90)
    with SessionLocal() as s:
        gs = get_global(s)
    show = gs.public_performance or (current_user(request) or User(is_admin=False)).is_admin
    data = perf.summary(market, days) if show else None
    pairs = perf.pair_stats(market, days) if show else {}
    pairs = {p: st for p, st in pairs.items() if p in gs.allowed_pairs} or pairs
    return render(request, "landing.html", market=market, period=period, periods=list(PERIODS), data=data,
                  pairs=pairs, show=show)


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
