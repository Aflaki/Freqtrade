from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import ExchangeAccount, SessionLocal, SignalLog, Trade, User, get_global
from ..engine.exchange import EXCHANGES
from ..security import flash, form_with_csrf, require_admin
from ..settings import config
from ..telegram import bot, esc
from .common import render

router = APIRouter(prefix="/admin")


@router.get("")
async def admin_users(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        users = s.scalars(select(User).order_by(User.created_at.desc())).all()
        stats = {}
        for u in users:
            accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == u.id)).all()
            stats[u.id] = {
                "accounts": len(accs), "capital": sum(a.capital for a in accs),
                "open": s.scalar(select(func.count(Trade.id)).where(Trade.user_id == u.id, Trade.status == "open")),
                "pnl": s.scalar(select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(Trade.user_id == u.id)),
            }
        gs = get_global(s)
    return render(request, "admin/users.html", users=users, stats=stats, gs=gs)


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
            flash(request, "نمی‌توانید خودتان را مسدود یا از ادمینی خارج کنید.", "error")
            return RedirectResponse("/admin", 303)
        if action == "approve":
            u.status = "active"
            if u.telegram_chat_id:
                await bot.send(u.telegram_chat_id, "✅ حساب شما تأیید شد و معاملات فعال است.")
        elif action == "suspend":
            u.status = "suspended"
        elif action == "promote":
            u.is_admin = True
        elif action == "demote":
            u.is_admin = False
        s.commit()
    flash(request, "انجام شد.")
    return RedirectResponse("/admin", 303)


@router.get("/settings")
async def admin_settings(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        gs = get_global(s)
    webhook_url = f"{config.base_url}/webhook/{config.webhook_secret}"
    return render(request, "admin/settings.html", gs=gs, all_exchanges=EXCHANGES, webhook_url=webhook_url)


@router.post("/settings")
async def admin_settings_save(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)

    def num(name, default):
        try:
            return float(str(f.get(name, default)).replace(",", ""))
        except ValueError:
            return default

    with SessionLocal() as s:
        gs = get_global(s)
        gs.entries_enabled = bool(f.get("entries_enabled"))
        gs.require_approval = bool(f.get("require_approval"))
        gs.sizing_formula = "pair_weights" if f.get("sizing_formula") == "pair_weights" else "equal_split"
        gs.tradable_balance_ratio = min(1.0, max(0.01, num("tradable_pct", 90) / 100))
        gs.default_max_open_trades = max(1, int(num("default_max_open_trades", 5)))
        gs.max_leverage = max(1, min(125, int(num("max_leverage", 5))))
        gs.min_capital = max(0.0, num("min_capital", 50))
        mx = num("max_stake_per_trade", 0)
        gs.max_stake_per_trade = mx if mx > 0 else None
        gs.stake_currency = (str(f.get("stake_currency", "USDT")).strip().upper() or "USDT")[:10]
        gs.allowed_exchanges = [e for e in EXCHANGES if f.get(f"ex__{e}")]
        pairs = []
        for line in str(f.get("allowed_pairs", "")).replace(",", "\n").splitlines():
            p = line.strip().upper()
            if "/" not in p and p.endswith(gs.stake_currency):
                p = p[: -len(gs.stake_currency)] + "/" + gs.stake_currency
            if p.count("/") == 1 and p not in pairs:
                pairs.append(p)
        gs.allowed_pairs = pairs
        s.commit()
    flash(request, "تنظیمات ذخیره شد.")
    return RedirectResponse("/admin/settings", 303)


@router.get("/trades")
async def admin_trades(request: Request):
    require_admin(request)
    with SessionLocal() as s:
        trades = s.scalars(select(Trade).order_by(Trade.opened_at.desc()).limit(300)).all()
        emails = {u.id: u.email for u in s.scalars(select(User))}
        names = {a.id: a.label or a.exchange for a in s.scalars(select(ExchangeAccount))}
        signals = s.scalars(select(SignalLog).order_by(SignalLog.received_at.desc()).limit(50)).all()
        cur = get_global(s).stake_currency
    return render(request, "admin/trades.html", trades=trades, emails=emails, acc_names=names,
                  signals=signals, cur=cur)


@router.post("/broadcast")
async def admin_broadcast(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    text = str(f.get("text", "")).strip()
    sent = 0
    if text:
        with SessionLocal() as s:
            chats = [u.telegram_chat_id for u in s.scalars(
                select(User).where(User.status == "active", User.telegram_chat_id.is_not(None)))]
        for c in chats:
            sent += await bot.send(c, f"📢 {esc(text)}")
    flash(request, f"برای {sent} نفر ارسال شد.")
    return RedirectResponse("/admin", 303)
