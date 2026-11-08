from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import (DEFAULT_NOTIFY, SIZING_MODES, ExchangeAccount, ExecutionLog, NotificationLog, SessionLocal, Trade,
                  User, get_global)
from ..engine import performance as perf
from ..engine.exchange import NEEDS_PASSWORD, pool, test_connection
from ..engine.sizing import allocation_table, effective_capital, effective_mode, max_open_trades, tradable_capital
from ..engine.trader import force_exit
from ..security import encrypt, flash, form_with_csrf, hash_password, mask, require_user, verify_password
from ..telegram import bot
from .common import render, tr

router = APIRouter()


def _num(v, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


@router.get("/dashboard")
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
        acc_names = {a.id: a.label or a.exchange or "Signals" for a in accounts}
        total_capital = sum(effective_capital(gs, a) for a in accounts)
    return render(request, "dashboard.html", gs=gs, accounts=accounts, open_trades=open_trades, closed=closed,
                  pnl=pnl, n_closed=n_closed, wins=wins, acc_names=acc_names, total_capital=total_capital)


# ------------------------------------------------------------------ portfolios (exchange or signal-only)
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
        rows = [{"acc": a, "key": "" if a.is_signal else mask(a.api_key_enc), "alloc": allocation_table(gs, a),
                 "mode": effective_mode(gs, a), "capital": effective_capital(gs, a),
                 "tradable": tradable_capital(gs, a), "mot": max_open_trades(gs, a),
                 "open": s.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id, Trade.status == "open"))}
                for a in accs]
    return render(request, "accounts.html", gs=gs, rows=rows)


def _form_ctx(gs, acc, kind):
    return dict(gs=gs, acc=acc, kind=kind, key_mask=mask(acc.api_key_enc) if acc and not acc.is_signal else "",
                stats_json=perf.stats_json(90), modes=[m for m in SIZING_MODES if m in (gs.sizing_modes or [])])


@router.get("/accounts/new")
async def account_new(request: Request):
    require_user(request)
    kind = "signal" if request.query_params.get("kind") == "signal" else "exchange"
    with SessionLocal() as s:
        gs = get_global(s)
    if kind == "signal" and not gs.signal_only_enabled:
        raise HTTPException(404)
    return render(request, "account_form.html", **_form_ctx(gs, None, kind))


@router.get("/accounts/{acc_id}/edit")
async def account_edit(acc_id: int, request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id)
    return render(request, "account_form.html", **_form_ctx(gs, acc, acc.kind))


async def _save_account(request: Request, acc_id: int | None):
    user = require_user(request)
    f = await form_with_csrf(request)
    back = f"/accounts/{acc_id}/edit" if acc_id else f"/accounts/new?kind={f.get('kind', 'exchange')}"
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id) if acc_id else None
        kind = acc.kind if acc else ("signal" if f.get("kind") == "signal" else "exchange")
        errors = []
        exchange = str(f.get("exchange", "")) if kind == "exchange" else ""
        if kind == "exchange" and exchange not in gs.allowed_exchanges:
            errors.append(tr(request, "err_exchange"))
        mode = "spot" if f.get("trading_mode") == "spot" else "futures"
        leverage = int(_num(f.get("leverage"), 1)) if mode == "futures" else 1
        if not 1 <= leverage <= gs.max_leverage:
            errors.append(tr(request, "err_leverage", max=gs.max_leverage))
        sizing = str(f.get("sizing_mode", ""))
        if sizing not in (gs.sizing_modes or []):
            sizing = (gs.sizing_modes or ["equal_split"])[0]
        pairs: dict[str, float] = {}
        for p in gs.allowed_pairs:
            if f.get(f"pair__{p}"):
                v = _num(f.get(f"value__{p}"), 0 if sizing == "custom_amounts" else 1.0)
                if sizing == "custom_amounts" and v <= 0:
                    errors.append(tr(request, "err_amount_pair", pair=p))
                pairs[p] = v if v > 0 else 1.0
        if not pairs:
            errors.append(tr(request, "err_pairs"))
        capital = sum(pairs.values()) if sizing == "custom_amounts" else _num(f.get("capital"))
        if capital < gs.min_capital:
            errors.append(tr(request, "err_min_capital", min=f"{gs.min_capital:,.0f}", cur=gs.stake_currency))
        mot = int(_num(f.get("max_open_trades"), 0)) or None
        sl = _num(f.get("stoploss_pct"), 0) or None
        dll = _num(f.get("daily_loss_limit_pct"), 0) or None
        if sl is not None and not 1 <= sl <= 100:
            errors.append(tr(request, "err_stoploss"))
        api_key = str(f.get("api_key", "")).strip()
        secret = str(f.get("secret", "")).strip()
        password = str(f.get("password", "")).strip()
        if kind == "exchange":
            if acc is None and (not api_key or not secret):
                errors.append(tr(request, "err_keys"))
            if acc is None and exchange in NEEDS_PASSWORD and not password:
                errors.append(tr(request, "err_passphrase"))
            if not f.get("no_withdraw"):
                errors.append(tr(request, "err_no_withdraw"))
        if errors:
            for e in errors:
                flash(request, e, "error")
            return RedirectResponse(back, 303)
        if acc is None:
            acc = ExchangeAccount(user_id=user.id, kind=kind)
            s.add(acc)
        elif acc.exchange != exchange or acc.trading_mode != mode:
            if s.scalar(select(func.count(Trade.id)).where(Trade.account_id == acc.id, Trade.status == "open")):
                flash(request, tr(request, "err_change_with_open"), "error")
                return RedirectResponse(back, 303)
        acc.label = str(f.get("label", "")).strip()[:80]
        acc.exchange, acc.trading_mode, acc.leverage = exchange, mode, leverage
        acc.margin_mode = "cross" if f.get("margin_mode") == "cross" else "isolated"
        acc.sizing_mode, acc.pairs, acc.capital, acc.max_open_trades = sizing, pairs, capital, mot
        acc.stoploss_pct, acc.daily_loss_limit_pct = sl, dll
        if kind == "exchange":
            acc.sandbox = bool(f.get("sandbox"))
            acc.dry_run = bool(f.get("dry_run"))
            if api_key:
                acc.api_key_enc = encrypt(api_key)
            if secret:
                acc.secret_enc = encrypt(secret)
            if password:
                acc.password_enc = encrypt(password)
            acc.last_check_ok, acc.last_check_msg = None, ""
        else:
            acc.dry_run = True
        s.commit()
        new_id = acc.id
    await pool.drop(new_id)
    flash(request, tr(request, "msg_saved_test" if kind == "exchange" else "msg_saved"))
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
        flash(request, tr(request, "msg_enabled" if acc.enabled else "msg_disabled"))
    return RedirectResponse("/accounts", 303)


@router.post("/accounts/{acc_id}/delete")
async def account_delete(acc_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        acc = _own(s, user, acc_id)
        if s.scalar(select(func.count(Trade.id)).where(Trade.account_id == acc.id, Trade.status == "open")):
            flash(request, tr(request, "err_delete_open"), "error")
            return RedirectResponse("/accounts", 303)
        s.delete(acc)
        s.commit()
    await pool.drop(acc_id)
    flash(request, tr(request, "msg_deleted"))
    return RedirectResponse("/accounts", 303)


# ------------------------------------------------------------------ trades
@router.get("/trades")
async def trades(request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        rows = s.scalars(select(Trade).where(Trade.user_id == user.id).order_by(Trade.opened_at.desc()).limit(300)).all()
        names = {a.id: a.label or a.exchange or "Signals" for a in s.scalars(
            select(ExchangeAccount).where(ExchangeAccount.user_id == user.id))}
        cur = get_global(s).stake_currency
    return render(request, "trades.html", trades=rows, acc_names=names, cur=cur)


@router.post("/trades/{trade_id}/close")
async def trade_close(trade_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    out = await force_exit(trade_id, user.id)
    if out is None:
        flash(request, tr(request, "err_not_found"), "error")
    else:
        flash(request, f"#{trade_id}: {out.status} {out.message}", "ok" if out.status in ("closed", "partial") else "error")
    return RedirectResponse(request.headers.get("referer") or "/trades", 303)


@router.post("/trades/close-all")
async def trades_close_all(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        ids = list(s.scalars(select(Trade.id).where(Trade.user_id == user.id, Trade.status == "open")))
    done = 0
    for i in ids:
        out = await force_exit(i, user.id)
        done += bool(out and out.status == "closed")
    flash(request, tr(request, "msg_closed_n", n=done, total=len(ids)))
    return RedirectResponse("/dashboard", 303)


# ------------------------------------------------------------------ activity log (own)
@router.get("/activity")
async def activity(request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        execs = s.scalars(select(ExecutionLog).where(ExecutionLog.user_id == user.id)
                          .order_by(ExecutionLog.id.desc()).limit(100)).all()
        notes = s.scalars(select(NotificationLog).where(NotificationLog.user_id == user.id)
                          .order_by(NotificationLog.id.desc()).limit(100)).all()
    return render(request, "activity.html", execs=execs, notes=notes)


# ------------------------------------------------------------------ settings / telegram
NOTIFY_KEYS = list(DEFAULT_NOTIFY)


@router.get("/settings")
async def settings_page(request: Request):
    user = require_user(request)
    return render(request, "settings.html", notify={**DEFAULT_NOTIFY, **(user.notify or {})},
                  notify_keys=NOTIFY_KEYS, bot_username=bot.username)


@router.post("/settings")
async def settings_save(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        action = f.get("action")
        if action == "profile":
            u.notify = {k: bool(f.get(f"n__{k}")) for k in NOTIFY_KEYS}
            u.trading_enabled = bool(f.get("trading_enabled"))
            u.name = str(f.get("name", u.name)).strip()[:120]
            u.phone = str(f.get("phone", u.phone)).strip()[:32]
            flash(request, tr(request, "msg_saved"))
        elif action == "password":
            if not verify_password(str(f.get("old", "")), u.password_hash):
                flash(request, tr(request, "err_old_password"), "error")
            elif len(str(f.get("new", ""))) < 8:
                flash(request, tr(request, "err_password_len"), "error")
            else:
                u.password_hash = hash_password(str(f["new"]))
                flash(request, tr(request, "msg_password_changed"))
        elif action == "telegram_unlink":
            u.telegram_chat_id = None
            flash(request, tr(request, "msg_saved"))
        s.commit()
    return RedirectResponse("/settings", 303)


@router.post("/telegram/link")
async def telegram_link(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    if not bot.username:
        flash(request, tr(request, "err_bot_missing"), "error")
        return RedirectResponse("/settings", 303)
    token = secrets.token_urlsafe(16)
    with SessionLocal() as s:
        s.get(User, user.id).telegram_link_token = token
        s.commit()
    return RedirectResponse(f"https://t.me/{bot.username}?start={token}", 303)
