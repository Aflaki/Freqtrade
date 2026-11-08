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


def _scope(request: Request) -> dict:
    """Shared filters of the public pages: market, period, group, bot."""
    qp = request.query_params
    market = {"long": "long", "short": "short", "spot": "long"}.get(qp.get("market", ""), "all")
    period = qp.get("days", "all") if qp.get("days", "all") in PERIODS else "all"
    all_bots, groups = perf.visible_bots()
    group_ids = {g.id for g in groups}
    group = int(qp["group"]) if qp.get("group", "").isdigit() and (int(qp["group"]) in group_ids or qp["group"] == "0") else None
    scope_bots = [b for b in all_bots if group is None or (b.group_id or 0) == group
                  or (group == 0 and b.group_id not in group_ids)]
    bot = int(qp["bot"]) if qp.get("bot", "").isdigit() and int(qp["bot"]) in {b.id for b in scope_bots} else None
    chosen = [bot] if bot else [b.id for b in scope_bots]
    qs = "&".join(x for x in (f"group={group}" if group is not None else "", f"bot={bot}" if bot else "") if x)
    return dict(market=market, period=period, days=PERIODS[period], all_bots=all_bots, groups=groups,
                group_sel=group, scope_bots=scope_bots, bot_sel=bot, chosen=chosen, scope_qs=qs,
                ungrouped=any(b.group_id not in group_ids for b in all_bots), periods=list(PERIODS))


def _can_see(request: Request) -> bool:
    with SessionLocal() as s:
        gs = get_global(s)
    return gs.public_performance or (current_user(request) or User(is_admin=False)).is_admin


@router.get("/")
async def landing(request: Request):
    sc = _scope(request)
    show = _can_see(request)
    m, d = sc["market"], sc["days"]
    data = perf.summary(m, d, bots=sc["chosen"]) if show else None
    pairs = perf.pair_stats(m, d, sc["chosen"]) if show else {}
    cards = perf.bot_summaries([b.id for b in sc["all_bots"]], m, d) if show else {}
    by_group: dict[int, list[int]] = {}
    for b in sc["all_bots"]:
        by_group.setdefault(b.group_id or 0, []).append(b.id)
    gcards = perf.group_summaries(sc["groups"], by_group, m, d) if show else {}
    ung = by_group.get(0, []) + [b.id for b in sc["all_bots"] if b.group_id and b.group_id not in {g.id for g in sc["groups"]}]
    gcards[0] = perf.group_summaries([type("G", (), {"id": 0})()], {0: ung}, m, d)[0] if show and ung else {}
    return render(request, "landing.html", data=data, pairs=pairs, show=show, cards=cards, gcards=gcards, **sc)


@router.get("/compare")
async def compare(request: Request):
    sc = _scope(request)
    show = _can_see(request)
    pair = request.query_params.get("pair", "").upper()
    m, d = sc["market"], sc["days"]
    matrix = perf.pair_matrix(m, d, sc["scope_bots"]) if show else {}
    cmp = perf.pair_compare(pair, m, d, sc["scope_bots"], sc["groups"]) if show and pair else None
    maxabs = max([abs(st["profit_abs"]) for row in matrix.values() for st in row.values()] or [1]) or 1
    return render(request, "compare.html", show=show, pair=pair, matrix=matrix, cmp=cmp, maxabs=maxabs,
                  group_names={g.id: g.name for g in sc["groups"]}, **sc)


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
