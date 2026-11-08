from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..engine.exchange import EXCHANGES, NEEDS_PASSWORD
from ..security import csrf_token, current_user, pop_flashes

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals.update(EXCHANGES=EXCHANGES, NEEDS_PASSWORD=NEEDS_PASSWORD)


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


templates.env.filters.update(money=_money, price=_price)

STATUS_FA = {"pending": "در انتظار تأیید", "active": "فعال", "suspended": "مسدود"}
templates.env.globals["STATUS_FA"] = STATUS_FA


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("user", current_user(request))
    ctx["csrf"] = csrf_token(request)
    ctx["flashes"] = pop_flashes(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
