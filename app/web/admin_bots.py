"""Admin: many Freqtrade bots — list, add (single or bulk), status, control, history import."""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import Bot, BotGroup, BotTrade, ExchangeAccount, SessionLocal, Trade, WebhookEvent, canon_pair, get_global
from ..engine import performance as perf
from ..engine.botmonitor import CONTROL_ACTIONS, _error_state, diagnose, fetch_info, monitor, normalize_api_url
from ..security import decrypt, encrypt, flash, form_with_csrf, require_admin
from .common import render, tr

router = APIRouter(prefix="/admin")
TEMPLATE_FILE = __import__("pathlib").Path(__file__).resolve().parents[2] / "freqtrade" / "webhook-config.json"


def _webhook_template() -> dict:
    import json
    return json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))["webhook"]


def _all_bots() -> tuple[list[Bot], str]:
    with SessionLocal() as s:
        return list(s.scalars(select(Bot).order_by(Bot.sort, Bot.id))), get_global(s).base_url


@router.get("/bots/export/freqtrade_webhooks.py")
async def export_script(request: Request):
    from fastapi.responses import Response

    from .export_script import build_script
    require_admin(request)
    bots, base = _all_bots()
    with SessionLocal() as s:
        site = get_global(s).site_name
    body = build_script(site, base, bots, _webhook_template())
    return Response(body, media_type="text/x-python",
                    headers={"Content-Disposition": 'attachment; filename="freqtrade_webhooks.py"'})


@router.get("/bots/export/webhooks.json")
async def export_json(request: Request):
    from fastapi.responses import JSONResponse
    require_admin(request)
    bots, base = _all_bots()
    tpl = _webhook_template()
    out = [{"id": b.id, "name": b.name, "api_url": b.api_url, "webhook_url": webhook_url(base, b),
            "webhook": {**tpl, "enabled": True, "url": webhook_url(base, b)}} for b in bots]
    return JSONResponse(out, headers={"Content-Disposition": 'attachment; filename="webhooks.json"'})


def webhook_url(base: str, bot: Bot) -> str:
    return f"{base.rstrip('/')}/webhook/{bot.webhook_secret}"


async def _create_bot(name: str, url: str, user: str, pw: str) -> tuple[Bot, str]:
    """Create a bot; if an API is given, read strategy/whitelist from it right away."""
    state, err = {}, ""
    url = normalize_api_url(url)
    if url:
        try:
            state = await fetch_info(url, user, pw)
        except Exception as e:
            state, err = _error_state(e), _error_state(e)["error"]
    with SessionLocal() as s:
        n = (s.scalar(select(func.count(Bot.id))) or 0) + 1
        name = (name or state.get("bot_name") or state.get("strategy") or f"Bot {n}").strip()[:80]
        b = Bot(name=name, api_url=url, api_user=user.strip()[:80], api_pass_enc=encrypt(pw) if pw else None,
                state=state or None, sort=n)
        s.add(b)
        s.commit()
        return b, err


@router.get("/bot")
async def bots_list(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        gs = get_global(s)
        bots = s.scalars(select(Bot).order_by(Bot.sort, Bot.id)).all()
        followers = {b.id: 0 for b in bots}
        for acc in s.scalars(select(ExchangeAccount)):
            for bid in acc.bots or []:
                if bid in followers:
                    followers[bid] += 1
        open_user = dict(s.execute(select(Trade.bot_id, func.count(Trade.id)).where(Trade.status == "open")
                                   .group_by(Trade.bot_id)).all())
        groups = s.scalars(select(BotGroup).order_by(BotGroup.sort, BotGroup.id)).all()
    caps = perf.capital_breakdown()
    return render(request, "admin/bots.html", gs=gs, bots=bots, followers=followers, open_user=open_user, tab="bot", caps=caps,
                  base=gs.base_url, groups=groups, group_names={g.id: g.name for g in groups}, wd=_watchdog_view(request, bots))


def _watchdog_view(request: Request, bots) -> dict:
    """Watchdog card: install command, servers with their last report, which bots are watched, recent events."""
    import datetime as dt

    from ..db import WatchdogEvent, WatchdogHost, utcnow
    from .watchdog import SILENT_MINUTES, bots_on_host, site_url, token
    with SessionLocal() as s:
        hosts = list(s.scalars(select(WatchdogHost).order_by(WatchdogHost.hostname)))
        events = list(s.scalars(select(WatchdogEvent).order_by(WatchdogEvent.id.desc()).limit(30)))
        watched: dict[int, dict] = {}
        rows = []
        for h in hosts:
            by_port = bots_on_host(h.ips or [], s)
            seen = {b.get("port"): b for b in (h.report or {}).get("bots", [])}
            for port, bot in by_port.items():
                if port in seen:
                    watched[bot.id] = {**seen[port], "host": h.hostname}
            rows.append({"h": h, "online": h.last_seen and utcnow() - h.last_seen < dt.timedelta(minutes=SILENT_MINUTES),
                         "n": len(seen), "mem": (h.report or {}).get("mem") or {}, "load": (h.report or {}).get("load") or []})
    names = {b.id: b.name for b in bots}
    unwatched = [b for b in bots if b.enabled and b.api_url and b.id not in watched]
    return {"cmd": f'curl -fsSL "{site_url(request)}/watchdog/install.sh?t={token()}" | sudo sh', "hosts": rows,
            "events": events, "watched": watched, "unwatched": unwatched if hosts else [], "names": names}


@router.post("/bot")
async def bots_action(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    action = f.get("action")
    if action == "add":
        b, err = await _create_bot(str(f.get("name", "")), str(f.get("api_url", "")), str(f.get("api_user", "")),
                                   str(f.get("api_pass", "")))
        flash(request, tr(request, "msg_bot_added", name=b.name) + (f" — API: {err}" if err else ""), "error" if err else "ok")
        return RedirectResponse(f"/admin/bots/{b.id}", 303)
    if action == "bulk":
        added, failed = 0, []
        for line in str(f.get("lines", "")).splitlines():
            parts = [p.strip() for p in re.split(r"[|,\t]", line)]
            if not line.strip() or line.strip().startswith("#"):
                continue
            parts += [""] * (4 - len(parts))
            name, url, user, pw = parts[:4]
            b, err = await _create_bot(name, url, user, pw)
            added += 1
            if err:
                failed.append(f"{b.name}: {err}")
        flash(request, tr(request, "msg_bots_bulk", n=added))
        for x in failed[:10]:
            flash(request, x, "error")
    elif action == "group_add":
        name = str(f.get("group_name", "")).strip()[:80]
        if name:
            with SessionLocal() as s:
                n = s.scalar(select(func.count(BotGroup.id))) or 0
                try:
                    cap = float(str(f.get("group_capital", "")).replace(",", "") or 0)
                except ValueError:
                    cap = 0
                s.add(BotGroup(name=name, description=str(f.get("group_desc", "")).strip()[:500], sort=n + 1,
                               stats_capital=cap if cap > 0 else None))
                s.commit()
            flash(request, tr(request, "msg_saved"))
    elif action == "group_save":
        with SessionLocal() as s:
            g = s.get(BotGroup, int(f.get("group_id") or 0))
            if g:
                g.name = str(f.get("group_name", g.name)).strip()[:80] or g.name
                g.description = str(f.get("group_desc", "")).strip()[:500]
                g.public = bool(f.get("group_public"))
                try:
                    cap = float(str(f.get("group_capital", "")).replace(",", "") or 0)
                except ValueError:
                    cap = 0
                g.stats_capital = cap if cap > 0 else None
                g.sort = int(f.get("group_sort") or g.sort or 0)
                s.commit()
        flash(request, tr(request, "msg_saved"))
    elif action == "group_delete":
        with SessionLocal() as s:
            g = s.get(BotGroup, int(f.get("group_id") or 0))
            if g:
                for b in s.scalars(select(Bot).where(Bot.group_id == g.id)):
                    b.group_id = None
                s.delete(g)
                s.commit()
        flash(request, tr(request, "msg_deleted"))
    elif action == "move":  # bulk: selected bots -> group
        target = str(f.get("target_group", ""))
        gid = int(target) if target.isdigit() and int(target) > 0 else None
        ids = [int(k.split("__", 1)[1]) for k in f.keys() if k.startswith("sel__") and k.split("__", 1)[1].isdigit()]
        with SessionLocal() as s:
            for b in s.scalars(select(Bot).where(Bot.id.in_(ids or [-1]))):
                b.group_id = gid
            s.commit()
        flash(request, tr(request, "msg_moved", n=len(ids)))
    elif action == "reload_all":
        with SessionLocal() as s:
            ids = [b.id for b in s.scalars(select(Bot).where(Bot.enabled.is_(True), Bot.api_url != ""))]
        ok, errs = 0, []
        for i in ids:
            try:
                await monitor.control(i, "reload_config")
                ok += 1
            except Exception as e:
                errs.append(f"#{i}: {type(e).__name__}: {str(e)[:120]}")
        flash(request, tr(request, "msg_reloaded", n=ok, total=len(ids)), "ok" if not errs else "error")
        for e in errs[:10]:
            flash(request, e, "error")
    elif action == "refresh_all":
        await monitor.poll_all(alert=False)
        flash(request, tr(request, "msg_done"))
    return RedirectResponse("/admin/bot", 303)


def _get(s, bot_id: int) -> Bot:
    b = s.get(Bot, bot_id)
    if b is None:
        raise HTTPException(404)
    return b


@router.get("/bots/{bot_id}")
async def bot_detail(bot_id: int, request: Request):
    require_admin(request)
    with SessionLocal() as s:
        gs = get_global(s)
        b = _get(s, bot_id)
        events = s.scalars(select(WebhookEvent).where(WebhookEvent.bot_id == bot_id)
                           .order_by(WebhookEvent.id.desc()).limit(40)).all()
        n_trades = s.scalar(select(func.count(BotTrade.id)).where(BotTrade.bot_id == bot_id)) or 0
        groups = s.scalars(select(BotGroup).order_by(BotGroup.sort, BotGroup.id)).all()
        traded = sorted(set(s.scalars(select(BotTrade.pair).where(BotTrade.bot_id == bot_id).distinct())))
    diag = request.session.pop("diag", None)
    diag = diag["steps"] if diag and diag.get("bot") == bot_id else None
    return render(request, "admin/bot.html", b=b, st=b.st, gs=gs, events=events, tab="bot", n_trades=n_trades, diag=diag,
                  groups=groups, traded=traded,
                  webhook_url=webhook_url(gs.base_url, b), has_pass=bool(b.api_pass_enc))


@router.post("/bots/{bot_id}")
async def bot_action(bot_id: int, request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    action = str(f.get("action", ""))
    back = RedirectResponse(f"/admin/bots/{bot_id}", 303)
    try:
        if action == "save":
            with SessionLocal() as s:
                b = _get(s, bot_id)
                b.name = str(f.get("name", b.name)).strip()[:80] or b.name
                b.description = str(f.get("description", "")).strip()[:500]
                b.api_url = normalize_api_url(str(f.get("api_url", "")))
                b.api_user = str(f.get("api_user", "")).strip()[:80]
                if str(f.get("api_pass", "")).strip():
                    b.api_pass_enc = encrypt(str(f["api_pass"]).strip())
                b.enabled, b.copy_enabled, b.public = bool(f.get("enabled")), bool(f.get("copy_enabled")), bool(f.get("public"))
                b.auto_restart = bool(f.get("auto_restart"))
                b.sort = int(f.get("sort") or b.sort or 0)
                try:
                    cap = float(str(f.get("stats_capital", "")).replace(",", "") or 0)
                except ValueError:
                    cap = 0
                b.stats_capital = cap if cap > 0 else None
                g = str(f.get("group_id", ""))
                b.group_id = int(g) if g.isdigit() and int(g) > 0 and s.get(BotGroup, int(g)) else None
                mode = str(f.get("pair_filter_mode", "none"))
                b.pair_filter_mode = mode if mode in ("whitelist", "blacklist") else "none"
                raw = str(f.get("pair_filter", "")).replace(",", "\n").replace(" ", "\n")
                b.pair_filter = sorted({canon_pair(x) for x in raw.splitlines() if x.strip()})
                s.commit()
            st = await monitor.poll_bot(bot_id, alert=False)
            flash(request, tr(request, "msg_saved") + (f" — API: {st.get('error')}" if st.get("error") else ""),
                  "error" if st.get("error") else "ok")
        elif action == "diagnose":
            with SessionLocal() as s:
                b = _get(s, bot_id)
                url, user, pw = b.api_url, b.api_user, decrypt(b.api_pass_enc) or ""
            request.session["diag"] = {"bot": bot_id, "steps": await diagnose(url, user, pw)}
            await monitor.poll_bot(bot_id, alert=False)
        elif action == "refresh":
            st = await monitor.poll_bot(bot_id, alert=False)
            flash(request, st.get("error") or tr(request, "msg_done"), "error" if st.get("error") else "ok")
        elif action == "regenerate":
            with SessionLocal() as s:
                _get(s, bot_id).webhook_secret = secrets.token_urlsafe(24)
                s.commit()
            flash(request, tr(request, "msg_done"))
        elif action == "import_api":
            with SessionLocal() as s:
                b = _get(s, bot_id)
                url, user, pw = b.api_url, b.api_user, decrypt(b.api_pass_enc) or ""
            if not url:
                raise ValueError("no Freqtrade API configured for this bot")
            flash(request, tr(request, "msg_imported", n=await perf.import_from_api(bot_id, url, user, pw)))
        elif action == "import_sqlite":
            path = str(f.get("db_path", "")).strip()
            flash(request, tr(request, "msg_imported", n=perf.import_from_sqlite(bot_id, path)))
        elif action == "delete":
            with SessionLocal() as s:
                if s.scalar(select(func.count(Trade.id)).where(Trade.bot_id == bot_id, Trade.status == "open")):
                    flash(request, tr(request, "err_bot_open_trades"), "error")
                    return back
                b = _get(s, bot_id)
                for acc in s.scalars(select(ExchangeAccount)):
                    if bot_id in (acc.bots or []):
                        acc.bots = [x for x in acc.bots if x != bot_id]
                s.delete(b)
                s.commit()
            flash(request, tr(request, "msg_deleted"))
            return RedirectResponse("/admin/bot", 303)
        elif action in CONTROL_ACTIONS:
            flash(request, f"Freqtrade: {await monitor.control(bot_id, action)}")
    except Exception as e:
        flash(request, f"{type(e).__name__}: {str(e)[:250]}", "error")
    return back
