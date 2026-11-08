from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import DEFAULT_NOTIFY, ExchangeAccount, SessionLocal, Trade, User, get_global
from ..engine.exchange import NEEDS_PASSWORD, pool, test_connection
from ..engine.sizing import allocation_table, max_open_trades, tradable_capital
from ..security import encrypt, flash, form_with_csrf, hash_password, mask, require_user, verify_password
from ..telegram import bot
from .common import render

router = APIRouter()


def _num(v, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


@router.get("/")
async def dashboard(request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
        accounts = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)).all()
        open_trades = s.scalars(select(Trade).where(Trade.user_id == user.id, Trade.status == "open")
                                .order_by(Trade.opened_at.desc())).all()
        closed = s.scalars(select(Trade).where(Trade.user_id == user.id, Trade.status == "closed")
                           .order_by(Trade.closed_at.desc()).limit(10)).all()
        pnl = s.scalar(select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(Trade.user_id == user.id)) or 0
        n_closed = s.scalar(select(func.count(Trade.id)).where(Trade.user_id == user.id, Trade.status == "closed")) or 0
        wins = s.scalar(select(func.count(Trade.id)).where(
            Trade.user_id == user.id, Trade.status == "closed", Trade.realized_pnl > 0)) or 0
        acc_names = {a.id: a.label or a.exchange for a in accounts}
    return render(request, "dashboard.html", gs=gs, accounts=accounts, open_trades=open_trades, closed=closed,
                  pnl=pnl, n_closed=n_closed, wins=wins, acc_names=acc_names,
                  total_capital=sum(a.capital for a in accounts), bot_username=bot.username)


# ------------------------------------------------------------------ exchange accounts
def _own(s, user: User, acc_id: int) -> ExchangeAccount:
    acc = s.get(ExchangeAccount, acc_id)
    if acc is None or acc.user_id != user.id:
        raise HTTPException(404)
    return acc


@router.get("/accounts")
async def accounts(request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
        accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)).all()
        rows = [{"acc": a, "key": mask(a.api_key_enc), "alloc": allocation_table(gs, a),
                 "tradable": tradable_capital(gs, a), "mot": max_open_trades(gs, a),
                 "open": s.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id, Trade.status == "open"))}
                for a in accs]
    return render(request, "accounts.html", gs=gs, rows=rows)


@router.get("/accounts/new")
async def account_new(request: Request):
    require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
    return render(request, "account_form.html", gs=gs, acc=None, key_mask="")


@router.get("/accounts/{acc_id}/edit")
async def account_edit(acc_id: int, request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id)
        key_mask = mask(acc.api_key_enc)
    return render(request, "account_form.html", gs=gs, acc=acc, key_mask=key_mask)


async def _save_account(request: Request, acc_id: int | None):
    user = require_user(request)
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id) if acc_id else None
        errors = []
        exchange = str(f.get("exchange", ""))
        if exchange not in gs.allowed_exchanges:
            errors.append("صرافی انتخاب‌شده مجاز نیست.")
        mode = "spot" if f.get("trading_mode") == "spot" else "futures"
        margin_mode = "cross" if f.get("margin_mode") == "cross" else "isolated"
        leverage = int(_num(f.get("leverage"), 1)) if mode == "futures" else 1
        if not 1 <= leverage <= gs.max_leverage:
            errors.append(f"لوریج باید بین ۱ و {gs.max_leverage} باشد.")
        capital = _num(f.get("capital"))
        if capital < gs.min_capital:
            errors.append(f"حداقل سرمایه {gs.min_capital:,.0f} {gs.stake_currency} است.")
        mot = int(_num(f.get("max_open_trades"), 0)) or None
        if mot is not None and not 1 <= mot <= 50:
            errors.append("تعداد معاملات هم‌زمان باید بین ۱ و ۵۰ باشد.")
        pairs = {}
        for p in gs.allowed_pairs:
            if f.get(f"pair__{p}"):
                w = _num(f.get(f"weight__{p}"), 1.0)
                pairs[p] = w if w > 0 else 1.0
        if not pairs:
            errors.append("حداقل یک جفت‌ارز انتخاب کنید.")
        api_key = str(f.get("api_key", "")).strip()
        secret = str(f.get("secret", "")).strip()
        password = str(f.get("password", "")).strip()
        if acc is None and (not api_key or not secret):
            errors.append("API Key و Secret لازم است.")
        if acc is None and exchange in NEEDS_PASSWORD and not password:
            errors.append("این صرافی Passphrase هم لازم دارد.")
        if not f.get("no_withdraw"):
            errors.append("تأیید کنید که کلید API دسترسی برداشت ندارد.")
        if errors:
            for e in errors:
                flash(request, e, "error")
            return RedirectResponse(f"/accounts/{acc_id}/edit" if acc_id else "/accounts/new", 303)

        if acc is None:
            acc = ExchangeAccount(user_id=user.id, exchange=exchange)
            s.add(acc)
        elif acc.exchange != exchange or acc.trading_mode != mode:
            has_open = s.scalar(select(func.count(Trade.id)).where(Trade.account_id == acc.id, Trade.status == "open"))
            if has_open:
                flash(request, "تا وقتی پوزیشن باز دارید نمی‌توانید صرافی یا نوع بازار را عوض کنید.", "error")
                return RedirectResponse(f"/accounts/{acc_id}/edit", 303)
        acc.label = str(f.get("label", "")).strip()[:80]
        acc.exchange = exchange
        acc.trading_mode = mode
        acc.margin_mode = margin_mode
        acc.leverage = leverage
        acc.capital = capital
        acc.max_open_trades = mot
        acc.pairs = pairs
        acc.sandbox = bool(f.get("sandbox"))
        acc.dry_run = bool(f.get("dry_run"))
        if api_key:
            acc.api_key_enc = encrypt(api_key)
        if secret:
            acc.secret_enc = encrypt(secret)
        if password:
            acc.password_enc = encrypt(password)
        acc.last_check_ok = None
        acc.last_check_msg = ""
        s.commit()
        new_id = acc.id
    await pool.drop(new_id)
    flash(request, "ذخیره شد. «تست اتصال» را بزنید تا کلید بررسی شود.")
    return RedirectResponse("/accounts", 303)


@router.post("/accounts/new")
async def account_create(request: Request):
    return await _save_account(request, None)


@router.post("/accounts/{acc_id}/edit")
async def account_update(acc_id: int, request: Request):
    return await _save_account(request, acc_id)


@router.post("/accounts/{acc_id}/test")
async def account_test(acc_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id)
        ok, msg = await test_connection(acc, gs.stake_currency)
        acc.last_check_ok, acc.last_check_msg = ok, msg[:500]
        s.commit()
    flash(request, msg, "ok" if ok else "error")
    return RedirectResponse("/accounts", 303)


@router.post("/accounts/{acc_id}/toggle")
async def account_toggle(acc_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        acc = _own(s, user, acc_id)
        acc.enabled = not acc.enabled
        s.commit()
        flash(request, "فعال شد." if acc.enabled else "غیرفعال شد (ورود جدید انجام نمی‌شود؛ خروج پوزیشن‌های باز ادامه دارد).")
    return RedirectResponse("/accounts", 303)


@router.post("/accounts/{acc_id}/delete")
async def account_delete(acc_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        acc = _own(s, user, acc_id)
        if s.scalar(select(func.count(Trade.id)).where(Trade.account_id == acc.id, Trade.status == "open")):
            flash(request, "این صرافی پوزیشن باز دارد. اول آن را غیرفعال کنید تا پوزیشن‌ها بسته شوند.", "error")
            return RedirectResponse("/accounts", 303)
        s.delete(acc)
        s.commit()
    await pool.drop(acc_id)
    flash(request, "حذف شد و کلیدها پاک شدند.")
    return RedirectResponse("/accounts", 303)


# ------------------------------------------------------------------ trades
@router.get("/trades")
async def trades(request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        rows = s.scalars(select(Trade).where(Trade.user_id == user.id).order_by(Trade.opened_at.desc()).limit(300)).all()
        names = {a.id: a.label or a.exchange for a in s.scalars(
            select(ExchangeAccount).where(ExchangeAccount.user_id == user.id))}
        cur = get_global(s).stake_currency
    return render(request, "trades.html", trades=rows, acc_names=names, cur=cur)


# ------------------------------------------------------------------ settings / telegram
NOTIFY_LABELS = {
    "entry_fill": "باز شدن پوزیشن", "exit_fill": "بسته شدن پوزیشن", "partial_exit": "خروج بخشی",
    "skipped": "سیگنال‌های انجام‌نشده (موجودی ناکافی و ...)", "errors": "خطاهای صرافی",
}


@router.get("/settings")
async def settings_page(request: Request):
    user = require_user(request)
    return render(request, "settings.html", notify={**DEFAULT_NOTIFY, **(user.notify or {})},
                  labels=NOTIFY_LABELS, bot_username=bot.username, bot_enabled=bot.enabled)


@router.post("/settings")
async def settings_save(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        action = f.get("action")
        if action == "notify":
            u.notify = {k: bool(f.get(f"n__{k}")) for k in DEFAULT_NOTIFY}
            u.trading_enabled = bool(f.get("trading_enabled"))
            u.name = str(f.get("name", u.name)).strip()[:120]
            u.phone = str(f.get("phone", u.phone)).strip()[:32]
            flash(request, "ذخیره شد.")
        elif action == "password":
            if not verify_password(str(f.get("old", "")), u.password_hash):
                flash(request, "رمز فعلی اشتباه است.", "error")
            elif len(str(f.get("new", ""))) < 8:
                flash(request, "رمز جدید حداقل ۸ کاراکتر.", "error")
            else:
                u.password_hash = hash_password(str(f["new"]))
                flash(request, "رمز عوض شد.")
        elif action == "telegram_unlink":
            u.telegram_chat_id = None
            flash(request, "تلگرام جدا شد.")
        s.commit()
    return RedirectResponse("/settings", 303)


@router.post("/telegram/link")
async def telegram_link(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    if not bot.username:
        flash(request, "ربات تلگرام هنوز تنظیم نشده است.", "error")
        return RedirectResponse("/settings", 303)
    token = secrets.token_urlsafe(16)
    with SessionLocal() as s:
        s.get(User, user.id).telegram_link_token = token
        s.commit()
    return RedirectResponse(f"https://t.me/{bot.username}?start={token}", 303)
