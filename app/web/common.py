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
# name -> swatch colours (background, card, accent, accent 2) for the theme picker
THEMES = {
    "aurora": ("#0d0b1f", "#2a2450", "#7c5cff", "#00c2ff"),
    "midnight": ("#0b0e11", "#161a1e", "#f0b90b", "#0ecb81"),
    "terminal": ("#050805", "#0a110a", "#22c55e", "#4ade80"),
    "classic": ("#f6f7f9", "#ffffff", "#2f6fed", "#0f1218"),
    "emerald": ("#f3f7f5", "#ffffff", "#0f9d6b", "#10231a"),
    "ocean": ("#e9f1ff", "#ffffff", "#2563eb", "#06b6d4"),
    "desert": ("#f7f1e6", "#fffaf2", "#0d9488", "#7c2d12"),
}
templates.env.globals.update(EXCHANGES=EXCHANGES, NEEDS_PASSWORD=NEEDS_PASSWORD, LANGS=LANGS, THEMES=THEMES)


def _qs(**params) -> str:
    """Query string for filter links; defaults ('all', empty) are left out so URLs stay short."""
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in params.items() if v not in (None, "", "all")})


templates.env.globals["qs"] = _qs


def _coin_icon(pair, size: int = 18):
    base = str(pair or "").split("/")[0].split(":")[0].upper()
    return Markup('<img class="cicon" src="/icon/coin/%s" width="%d" height="%d" alt="" loading="lazy">') % (base, size, size)


def _ex_icon(ex, size: int = 18):
    return Markup('<img class="cicon" src="/icon/ex/%s" width="%d" height="%d" alt="" loading="lazy">') % (str(ex or ""), size, size)


def _static_versions() -> dict[str, str]:
    """A short hash per static file: its URL changes whenever the file does, so browsers never keep an old copy."""
    import hashlib
    from pathlib import Path
    out = {}
    root = Path(__file__).resolve().parent.parent / "static"
    for f in root.glob("*.*"):
        if f.suffix in (".css", ".js"):
            out[f.name] = hashlib.sha1(f.read_bytes()).hexdigest()[:10]
    return out


STATIC_V = _static_versions()
templates.env.globals.update(coin=_coin_icon, exch=_ex_icon, sv=lambda name: STATIC_V.get(name, "1"))


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


_ZONES: set[str] | None = None


def zones() -> list[str]:
    global _ZONES
    if _ZONES is None:
        from zoneinfo import available_timezones
        _ZONES = {z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/"))}
        _ZONES |= {"UTC"}
    return sorted(_ZONES)


def valid_tz(name) -> str:
    name = str(name or "").strip()
    return name if name and name in set(zones()) else ""


def visitor_tz(request: Request, user=None, site=None) -> str:
    """The user's own choice, else the browser's (cookie set by base.html), else the site default, else UTC."""
    from urllib.parse import unquote
    return (valid_tz(getattr(user, "timezone", "")) or valid_tz(unquote(request.cookies.get("tz") or ""))
            or valid_tz(getattr(site, "default_timezone", "")) or "UTC")


def _local(value, tz_name: str):
    import datetime as _d
    from zoneinfo import ZoneInfo
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = _d.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, _d.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_d.UTC)  # everything is stored in UTC
    return value.astimezone(ZoneInfo(tz_name))


from jinja2 import pass_context  # noqa: E402


@pass_context
def _lt(ctx, value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """{{ some_utc_datetime|lt('%Y-%m-%d %H:%M') }} -> in the visitor's time zone."""
    v = _local(value, ctx.get("tz_name") or "UTC")
    return v.strftime(fmt) if v else ""


templates.env.filters["lt"] = _lt


def render(request: Request, name: str, status_code: int = 200, **ctx):
    user = ctx.setdefault("user", current_user(request))
    lang = get_lang(request, user)
    with SessionLocal() as s:
        ctx.setdefault("site", get_global(s))
    site = ctx["site"]
    theme = (user.theme if user and user.theme in THEMES else None) or request.query_params.get("theme")
    if theme not in THEMES:
        theme = site.theme if site.theme in THEMES else "aurora"
    from .icons import icon_version
    tz_name = visitor_tz(request, user, site)
    from zoneinfo import ZoneInfo
    import datetime as _d
    abbr = _d.datetime.now(ZoneInfo(tz_name)).strftime("%Z")
    tz_label = "UTC" if tz_name == "UTC" else (abbr if abbr and not abbr[0] in "+-" else tz_name.split("/")[-1].replace("_", " "))
    ctx.update(tz_name=tz_name, tz_label=tz_label, tz_auto=not (user and valid_tz(user.timezone)))
    ctx.update(lang=lang, dir=LANGS[lang]["dir"], theme=theme, icon_v=icon_version(), csrf=csrf_token(request), flashes=pop_flashes(request),
               _=lambda key, **kw: t(lang, key, **kw))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def tr(request: Request, key: str, **kw) -> str:
    """Translate a flash/message for the current visitor."""
    return t(get_lang(request), key, **kw)
