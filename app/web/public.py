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
    """Shared filters of the public pages: scope (all / group / bot), market side, period."""
    qp = request.query_params
    market = {"long": "long", "short": "short", "spot": "long"}.get(qp.get("market", ""), "all")
    period = qp.get("days", "all") if qp.get("days", "all") in PERIODS else "all"
    all_bots, groups = perf.visible_bots()
    group_ids = {g.id for g in groups}
    gname = {g.id: g.name for g in groups}

    def eff_group(b) -> int:
        return b.group_id if b.group_id in group_ids else 0

    raw = qp.get("scope", "")
    if not raw:  # older links: ?group= / ?bot=
        raw = ("b" + qp["bot"]) if qp.get("bot", "").isdigit() else ("g" + qp["group"]) if qp.get("group", "").isdigit() else ""
    by_id = {b.id: b for b in all_bots}
    group = bot = None
    if raw[:1] == "g" and raw[1:].isdigit() and (int(raw[1:]) in group_ids or raw == "g0"):
        group = int(raw[1:])
    elif raw[:1] == "b" and raw[1:].isdigit() and int(raw[1:]) in by_id:
        bot = int(raw[1:])
        group = eff_group(by_id[bot])
    scope_bots = ([by_id[bot]] if bot else [b for b in all_bots if eff_group(b) == group] if group is not None else all_bots)
    scope = f"b{bot}" if bot else f"g{group}" if group is not None else "all"
    tree = [(g, [b for b in all_bots if eff_group(b) == g.id]) for g in groups]
    tree = [(g, bs) for g, bs in tree if bs]
    ungrouped = [b for b in all_bots if eff_group(b) == 0]
    return dict(market=market, period=period, days=PERIODS[period], all_bots=all_bots, groups=groups,
                group_sel=group, bot_sel=bot, scope=scope, scope_qs="" if scope == "all" else f"scope={scope}",
                scope_bots=scope_bots, chosen=[b.id for b in scope_bots], tree=tree, ungrouped_bots=ungrouped,
                ungrouped=bool(ungrouped), periods=list(PERIODS),
                group_label=(gname.get(group) if group else None) if group is not None else None,
                bot_label=by_id[bot].name if bot else None)


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
    # bots tab: the selected bot's siblings (its group), else the bots in scope
    peers = ([b for b in sc["all_bots"] if (b.group_id or 0) == (sc["group_sel"] or 0)] if sc["bot_sel"]
             else sc["scope_bots"])
    cards = perf.bot_summaries([b.id for b in peers], m, d) if show else {}
    caps = perf.capital_breakdown() if show else {}
    by_group: dict[int, list[int]] = {}
    for b in sc["all_bots"]:
        by_group.setdefault(b.group_id or 0, []).append(b.id)
    gcards = perf.group_summaries(sc["groups"], by_group, m, d) if show and sc["scope"] == "all" else {}
    names = {b.id: b.name for b in sc["all_bots"]}
    return render(request, "landing.html", data=data, pairs=pairs, show=show, cards=cards, gcards=gcards,
                  peers=peers, caps=caps, bot_names=names, **sc)


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
