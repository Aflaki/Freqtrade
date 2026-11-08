from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from ..db import SessionLocal, get_global
from ..engine.exchange import EXCHANGES, NEEDS_PASSWORD
from ..i18n import LANGS, t
from ..security import csrf_token, current_user, pop_flashes

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals.update(EXCHANGES=EXCHANGES, NEEDS_PASSWORD=NEEDS_PASSWORD, LANGS=LANGS)


def _money(v, digits: int = 2) -> str:
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _price(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    return f"{v:,.6g}" if abs(v) < 1 else f"{v:,.4f}" if abs(v) < 100 else f"{v:,.2f}"


def _pct(v, digits: int = 2) -> str:
    try:
        return f"{float(v):+.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _num(fn):
    """Numbers are always rendered left-to-right (signs stay in place inside RTL Persian text)."""
    def wrap(v, *a):
        return Markup('<span class="num">%s</span>') % fn(v, *a)
    return wrap


def _signed(v, digits: int = 2) -> str:
    try:
        return f"{float(v):+,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _plain(v) -> str:
    """Strip Telegram markdown that Freqtrade puts in its messages."""
    import re
    return re.sub(r"```\w*\n?|[*`]", "", "" if v is None else str(v))


templates.env.filters["plain"] = _plain
templates.env.filters.update(money=_num(_money), price=_num(_price), pct=_num(_pct), signed=_num(_signed))


def get_lang(request: Request, user=None) -> str:
    user = user if user is not None else current_user(request)
    if user and user.lang in LANGS:
        return user.lang
    q = request.query_params.get("lang")
    if q in LANGS:
        return q
    c = request.cookies.get("lang")
    if c in LANGS:
        return c
    for part in request.headers.get("accept-language", "").split(","):
        code = part.strip()[:2].lower()
        if code in LANGS:
            return code
    with SessionLocal() as s:
        return get_global(s).default_lang


def render(request: Request, name: str, status_code: int = 200, **ctx):
    user = ctx.setdefault("user", current_user(request))
    lang = get_lang(request, user)
    with SessionLocal() as s:
        ctx.setdefault("site", get_global(s))
    ctx.update(lang=lang, dir=LANGS[lang]["dir"], csrf=csrf_token(request), flashes=pop_flashes(request),
               _=lambda key, **kw: t(lang, key, **kw))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def tr(request: Request, key: str, **kw) -> str:
    """Translate a flash/message for the current visitor."""
    return t(get_lang(request), key, **kw)
