"""Admin: many Freqtrade bots — list, add (single or bulk), status, control, history import."""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import Bot, BotTrade, ExchangeAccount, SessionLocal, Trade, WebhookEvent, get_global
from ..engine import performance as perf
from ..engine.botmonitor import CONTROL_ACTIONS, _error_state, fetch_info, monitor
from ..security import decrypt, encrypt, flash, form_with_csrf, require_admin
from .common import render, tr

router = APIRouter(prefix="/admin")


def webhook_url(base: str, bot: Bot) -> str:
    return f"{base.rstrip('/')}/webhook/{bot.webhook_secret}"


async def _create_bot(name: str, url: str, user: str, pw: str) -> tuple[Bot, str]:
    """Create a bot; if an API is given, read strategy/whitelist from it right away."""
    state, err = {}, ""
    url = url.strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        url = "http://" + url
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
    return render(request, "admin/bots.html", gs=gs, bots=bots, followers=followers, open_user=open_user, tab="bot",
                  base=gs.base_url)


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
    return render(request, "admin/bot.html", b=b, st=b.st, gs=gs, events=events, tab="bot", n_trades=n_trades,
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
                url = str(f.get("api_url", "")).strip().rstrip("/")
                b.api_url = ("http://" + url) if url and not re.match(r"^https?://", url) else url
                b.api_user = str(f.get("api_user", "")).strip()[:80]
                if str(f.get("api_pass", "")).strip():
                    b.api_pass_enc = encrypt(str(f["api_pass"]).strip())
                b.enabled, b.copy_enabled, b.public = bool(f.get("enabled")), bool(f.get("copy_enabled")), bool(f.get("public"))
                b.sort = int(f.get("sort") or b.sort or 0)
                s.commit()
            st = await monitor.poll_bot(bot_id, alert=False)
            flash(request, tr(request, "msg_saved") + (f" — API: {st.get('error')}" if st.get("error") else ""),
                  "error" if st.get("error") else "ok")
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
