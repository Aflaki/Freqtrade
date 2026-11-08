from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select

from ..db import (SIZING_MODES, BotTrade, ExchangeAccount, ExecutionLog, NotificationLog, SessionLocal, Trade, User,
                  WebhookEvent, get_global)
from ..engine import performance as perf
from ..engine.botmonitor import CONTROL_ACTIONS, monitor
from ..engine.exchange import EXCHANGES
from ..engine.sizing import effective_capital
from ..i18n import LANGS
from ..security import decrypt, encrypt, flash, form_with_csrf, require_admin
from ..telegram import bot, esc
from .common import render, tr

router = APIRouter(prefix="/admin")
PAGE = 50


# ------------------------------------------------------------------ bot status & control
@router.get("/bot")
async def admin_bot(request: Request):
    require_admin(request)
    st = monitor.load()
    with SessionLocal() as s:
        gs = get_global(s)
        events = s.scalars(select(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(40)).all()
    return render(request, "admin/bot.html", st=st, gs=gs, events=events, tab="bot",
                  webhook_url=f"{gs.base_url.rstrip('/')}/webhook/{gs.webhook_secret}")


@router.post("/bot")
async def admin_bot_action(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    action = str(f.get("action", ""))
    try:
        if action == "refresh":
            st = await monitor.poll(alert=False)
            flash(request, tr(request, "msg_done") if st.get("api") in ("ok", "not_configured") else st.get("error", ""),
                  "ok" if st.get("api") in ("ok", "not_configured") else "error")
        elif action in CONTROL_ACTIONS:
            flash(request, f"Freqtrade: {await monitor.control(action)}")
    except Exception as e:
        flash(request, f"{type(e).__name__}: {str(e)[:200]}", "error")
    return RedirectResponse("/admin/bot", 303)


# ------------------------------------------------------------------ users
@router.get("")
async def admin_users(request: Request):
    require_admin(request)
    q = request.query_params.get("q", "").strip().lower()
    with SessionLocal() as s:
        gs = get_global(s)
        stmt = select(User).order_by(User.created_at.desc())
        if q:
            stmt = stmt.where(User.email.contains(q) | User.name.contains(q) | User.phone.contains(q))
        users = s.scalars(stmt).all()
        stats = {}
        for u in users:
            accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == u.id)).all()
            stats[u.id] = {
                "exchange": sum(1 for a in accs if not a.is_signal), "signal": sum(1 for a in accs if a.is_signal),
                "capital": sum(effective_capital(gs, a) for a in accs),
                "open": s.scalar(select(func.count(Trade.id)).where(Trade.user_id == u.id, Trade.status == "open")),
                "pnl": s.scalar(select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(Trade.user_id == u.id)),
            }
        counts = {k: s.scalar(select(func.count(User.id)).where(User.status == k)) for k in ("pending", "active", "suspended")}
    return render(request, "admin/users.html", users=users, stats=stats, gs=gs, q=q, counts=counts, tab="users")


@router.post("/users/{uid}")
async def admin_user_action(uid: int, request: Request):
    admin = require_admin(request)
    f = await form_with_csrf(request)
    action = f.get("action")
    with SessionLocal() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404)
        if u.id == admin.id and action in ("suspend", "demote"):
            flash(request, tr(request, "err_self"), "error")
            return RedirectResponse("/admin", 303)
        if action == "approve":
            u.status = "active"
            if u.telegram_chat_id:
                await bot.send(u.telegram_chat_id, tr(request, "tg_approved"), u.id, "account")
        elif action == "suspend":
            u.status = "suspended"
        elif action == "promote":
            u.is_admin = True
        elif action == "demote":
            u.is_admin = False
        elif action == "subscription":
            raw = str(f.get("until", "")).strip()
            u.subscription_until = dt.datetime.fromisoformat(raw).replace(hour=23, minute=59) if raw else None
            u.admin_note = str(f.get("note", "")).strip()[:500]
        s.commit()
    flash(request, tr(request, "msg_done"))
    return RedirectResponse(request.headers.get("referer") or "/admin", 303)


@router.get("/users/{uid}")
async def admin_user_detail(uid: int, request: Request):
    require_admin(request)
    with SessionLocal() as s:
        u = s.get(User, uid)
        if u is None:
            raise HTTPException(404)
        accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == uid)).all()
        trades = s.scalars(select(Trade).where(Trade.user_id == uid).order_by(Trade.id.desc()).limit(100)).all()
        execs = s.scalars(select(ExecutionLog).where(ExecutionLog.user_id == uid).order_by(ExecutionLog.id.desc()).limit(100)).all()
        notes = s.scalars(select(NotificationLog).where(NotificationLog.user_id == uid).order_by(NotificationLog.id.desc()).limit(100)).all()
        gs = get_global(s)
    return render(request, "admin/user_detail.html", u=u, accs=accs, trades=trades, execs=execs, notes=notes,
                  gs=gs, tab="users")


# ------------------------------------------------------------------ settings
@router.get("/settings")
async def admin_settings(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        gs = get_global(s)
    return render(request, "admin/settings.html", gs=gs, all_exchanges=EXCHANGES, sizing_all=SIZING_MODES,
                  webhook_url=f"{gs.base_url.rstrip('/')}/webhook/{gs.webhook_secret}", tab="settings",
                  bot_username=bot.username, bot_error=bot.last_error, has_token=bool(gs.telegram_token_enc),
                  has_ft_pass=bool(gs.ft_api_pass_enc))


def _num(f, name, default):
    try:
        return float(str(f.get(name, default)).replace(",", ""))
    except ValueError:
        return default


@router.post("/settings")
async def admin_settings_save(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    section = f.get("section")
    restart_bot = None
    with SessionLocal() as s:
        gs = get_global(s)
        if section == "general":
            gs.site_name = str(f.get("site_name", gs.site_name)).strip()[:80] or "Signal Relay"
            gs.default_lang = f.get("default_lang") if f.get("default_lang") in LANGS else "fa"
            gs.base_url = str(f.get("base_url", "")).strip().rstrip("/")[:200] or gs.base_url
            gs.public_performance = bool(f.get("public_performance"))
            gs.signal_only_enabled = bool(f.get("signal_only_enabled"))
            gs.require_approval = bool(f.get("require_approval"))
            gs.trial_days = max(0, int(_num(f, "trial_days", 0)))
            gs.daily_report_hour_utc = max(0, min(23, int(_num(f, "daily_report_hour_utc", 20))))
            gs.hero = {code: {"title": str(f.get(f"hero_title__{code}", "")).strip()[:200],
                              "subtitle": str(f.get(f"hero_sub__{code}", "")).strip()[:600]} for code in LANGS}
        elif section == "trading":
            gs.entries_enabled = bool(f.get("entries_enabled"))
            gs.stake_currency = (str(f.get("stake_currency", "USDT")).strip().upper() or "USDT")[:10]
            gs.tradable_balance_ratio = min(1.0, max(0.01, _num(f, "tradable_pct", 90) / 100))
            gs.sizing_modes = [m for m in SIZING_MODES if f.get(f"mode__{m}")] or ["equal_split"]
            gs.default_max_open_trades = max(1, int(_num(f, "default_max_open_trades", 5)))
            gs.max_leverage = max(1, min(125, int(_num(f, "max_leverage", 5))))
            gs.min_capital = max(0.0, _num(f, "min_capital", 50))
            mx = _num(f, "max_stake_per_trade", 0)
            gs.max_stake_per_trade = mx if mx > 0 else None
            sl = _num(f, "default_stoploss_pct", 0)
            gs.default_stoploss_pct = sl if sl > 0 else None
            gs.allowed_exchanges = [e for e in EXCHANGES if f.get(f"ex__{e}")]
            pairs: list[str] = []
            for line in str(f.get("allowed_pairs", "")).replace(",", "\n").splitlines():
                p = line.strip().upper()
                if "/" not in p and p.endswith(gs.stake_currency):
                    p = p[: -len(gs.stake_currency)] + "/" + gs.stake_currency
                if p.count("/") == 1 and p not in pairs:
                    pairs.append(p)
            gs.allowed_pairs = pairs
        elif section == "freqtrade":
            gs.relay_on = "order" if f.get("relay_on") == "order" else "fill"
            gs.signal_source = "generic" if f.get("signal_source") == "generic" else "freqtrade"
            gs.allowed_ips = [x.strip() for x in str(f.get("allowed_ips", "")).replace("\n", ",").split(",") if x.strip()]
            gs.dedupe_seconds = max(0, int(_num(f, "dedupe_seconds", 30)))
            gs.ft_api_url = str(f.get("ft_api_url", "")).strip()[:200]
            gs.ft_api_user = str(f.get("ft_api_user", "")).strip()[:80]
            if str(f.get("ft_api_pass", "")).strip():
                gs.ft_api_pass_enc = encrypt(str(f["ft_api_pass"]).strip())
            gs.ft_db_path = str(f.get("ft_db_path", "")).strip()[:300]
        elif section == "regenerate_secret":
            gs.webhook_secret = secrets.token_urlsafe(24)
        elif section == "telegram":
            token = str(f.get("telegram_token", "")).strip()
            if f.get("remove"):
                gs.telegram_token_enc, restart_bot = None, ""
            elif token:
                gs.telegram_token_enc, restart_bot = encrypt(token), token
        s.commit()
    if restart_bot is not None:
        await bot.configure(restart_bot)
        if restart_bot and not bot.username:
            flash(request, f"Telegram: {bot.last_error}", "error")
    flash(request, tr(request, "msg_saved"))
    return RedirectResponse(f"/admin/settings#{section}", 303)


@router.post("/telegram-test")
async def telegram_test(request: Request):
    admin = require_admin(request)
    await form_with_csrf(request)
    ok = await bot.send(admin.telegram_chat_id, "✅ Test", admin.id, "test") if admin.telegram_chat_id else False
    flash(request, tr(request, "msg_done") if ok else tr(request, "err_telegram_test"), "ok" if ok else "error")
    return RedirectResponse("/admin/settings#telegram", 303)


# ------------------------------------------------------------------ performance data
@router.get("/performance")
async def admin_performance(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        gs = get_global(s)
        n = s.scalar(select(func.count(BotTrade.id))) or 0
        n_open = s.scalar(select(func.count(BotTrade.id)).where(BotTrade.is_open.is_(True))) or 0
        by_source = dict(s.execute(select(BotTrade.source, func.count(BotTrade.id)).group_by(BotTrade.source)).all())
        recent = s.scalars(select(BotTrade).order_by(BotTrade.id.desc()).limit(50)).all()
    return render(request, "admin/performance.html", gs=gs, n=n, n_open=n_open, by_source=by_source, recent=recent,
                  tab="performance")


@router.post("/performance")
async def admin_performance_action(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    action = f.get("action")
    with SessionLocal() as s:
        gs = get_global(s)
        url, user, pw, path = gs.ft_api_url, gs.ft_api_user, decrypt(gs.ft_api_pass_enc) or "", gs.ft_db_path
    try:
        if action == "import_api":
            if not url:
                raise ValueError("Freqtrade API URL is empty (Settings → Freqtrade)")
            n = await perf.import_from_api(url, user, pw)
            flash(request, tr(request, "msg_imported", n=n))
        elif action == "import_sqlite":
            if not path:
                raise ValueError("Freqtrade database path is empty (Settings → Freqtrade)")
            n = perf.import_from_sqlite(path)
            flash(request, tr(request, "msg_imported", n=n))
        elif action == "clear":
            with SessionLocal() as s:
                s.execute(delete(BotTrade))
                s.commit()
            flash(request, tr(request, "msg_done"))
    except Exception as e:
        flash(request, f"{type(e).__name__}: {str(e)[:300]}", "error")
    return RedirectResponse("/admin/performance", 303)


# ------------------------------------------------------------------ logs
@router.get("/logs")
async def admin_logs(request: Request):
    require_admin(request)
    qp = request.query_params
    kind = qp.get("kind", "webhooks")
    page = max(1, int(qp.get("page", "1") or 1))
    uid = int(qp["user"]) if qp.get("user", "").isdigit() else None
    status = qp.get("status", "")
    with SessionLocal() as s:
        if kind == "executions":
            q = select(ExecutionLog).order_by(ExecutionLog.id.desc())
            if uid:
                q = q.where(ExecutionLog.user_id == uid)
            if status:
                q = q.where(ExecutionLog.status == status)
            if qp.get("event", "").isdigit():
                q = q.where(ExecutionLog.webhook_event_id == int(qp["event"]))
        elif kind == "notifications":
            q = select(NotificationLog).order_by(NotificationLog.id.desc())
            if uid:
                q = q.where(NotificationLog.user_id == uid)
            if status in ("ok", "failed"):
                q = q.where(NotificationLog.ok.is_(status == "ok"))
        else:
            kind = "webhooks"
            q = select(WebhookEvent).order_by(WebhookEvent.id.desc())
            if status:
                q = q.where(WebhookEvent.status == status)
        rows = s.scalars(q.offset((page - 1) * PAGE).limit(PAGE + 1)).all()
        emails = {u.id: u.email for u in s.scalars(select(User))}
    return render(request, "admin/logs.html", kind=kind, rows=rows[:PAGE], more=len(rows) > PAGE, page=page,
                  uid=uid, status=status, emails=emails, tab="logs")


@router.get("/trades")
async def admin_trades(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        trades = s.scalars(select(Trade).order_by(Trade.opened_at.desc()).limit(300)).all()
        emails = {u.id: u.email for u in s.scalars(select(User))}
        names = {a.id: a.label or a.exchange or "Signals" for a in s.scalars(select(ExchangeAccount))}
        cur = get_global(s).stake_currency
    return render(request, "admin/trades.html", trades=trades, emails=emails, acc_names=names, cur=cur, tab="trades")


@router.post("/broadcast")
async def admin_broadcast(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    text = str(f.get("text", "")).strip()
    sent = 0
    if text:
        with SessionLocal() as s:
            users = [(u.telegram_chat_id, u.id) for u in s.scalars(
                select(User).where(User.status == "active", User.telegram_chat_id.is_not(None)))]
        for chat, uid in users:
            sent += await bot.send(chat, f"📢 {esc(text)}", uid, "broadcast")
    flash(request, tr(request, "msg_broadcast", n=sent))
    return RedirectResponse("/admin", 303)
