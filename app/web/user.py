from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import (utcnow, 
    ADMIN_NOTIFY,
    DEFAULT_NOTIFY,
    SIZING_MODES,
    Bot,
    ExchangeAccount,
    ExecutionLog,
    NotificationLog,
    SessionLocal,
    Trade,
    User,
    get_global,
)
from ..engine import performance as perf
from ..engine.exchange import NEEDS_PASSWORD, pool, test_connection
from ..engine.sizing import (
    allocation_table,
    effective_capital,
    effective_mode,
    max_open_trades,
    tradable_capital,
)
from ..engine.trader import force_exit
from ..security import encrypt, flash, form_with_csrf, mask, require_user
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
        bot_names = {b.id: b.name for b in s.scalars(select(Bot))}
        rows = [{"acc": a, "key": "" if a.is_signal else mask(a.api_key_enc), "alloc": allocation_table(gs, a),
                 "bots": [bot_names.get(i, f"#{i}") for i in (a.bots or [])],
                 "mode": effective_mode(gs, a), "capital": effective_capital(gs, a),
                 "tradable": tradable_capital(gs, a), "mot": max_open_trades(gs, a),
                 "open": s.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id, Trade.status == "open"))}
                for a in accs]
    return render(request, "accounts.html", gs=gs, rows=rows)


def _public_bots() -> list[Bot]:
    with SessionLocal() as s:
        return list(s.scalars(select(Bot).where(Bot.enabled.is_(True), Bot.public.is_(True)).order_by(Bot.sort, Bot.id)))


def _form_ctx(gs, acc, kind):
    bots = _public_bots()
    ids = [b.id for b in bots]
    _, groups = perf.visible_bots()
    gids = {g.id for g in groups}
    sections = [(g, [b for b in bots if b.group_id == g.id]) for g in groups]
    sections = [(g, bs) for g, bs in sections if bs]
    rest = [b for b in bots if b.group_id not in gids]
    if rest:
        sections.append((None, rest))
    return dict(gs=gs, acc=acc, kind=kind, key_mask=mask(acc.api_key_enc) if acc and not acc.is_signal else "",
                stats_json=perf.stats_json(ids, 90), modes=[m for m in SIZING_MODES if m in (gs.sizing_modes or [])],
                bots=bots, cards={m: perf.bot_summaries(ids, m, 90) for m in ("all", "long")},
                followed=(acc.bots or []) if acc else (ids if len(ids) == 1 else []), sections=sections,
                pair_bot=(acc.pair_bot or {}) if acc else {}, server_ip=_server_ip(), **_catalog_ctx(gs, bots))


def _catalog_ctx(gs, bots) -> dict:
    """Offered pairs for the form: their bots (with the admin's rules) and each exchange's minimum order."""
    from ..engine import markets
    from ..engine.offers import offers_by_pair, pair_rules
    from ..engine.offers import bot_leverage
    names = {b.id: b.name for b in bots}
    cache = markets.all_info()
    catalog, blev = [], {}
    for pair, offers in sorted(offers_by_pair().items()):
        offers = [o for o in offers if o.bot_id in names]
        if not offers:
            continue
        r = pair_rules(offers)
        ex = {}
        for e in gs.allowed_exchanges or []:
            pairs = (cache.get(e) or {}).get("pairs")
            if pairs:  # known: listed (min order) or not listed (None)
                i = pairs.get(pair)
                ex[e] = markets.min_order(i) if i else None
        catalog.append({"pair": pair, "mandatory": r["mandatory"], "default_bot": r["default_bot"], "ex": ex,
                        "bots": [{"id": o.bot_id, "name": names[o.bot_id], "lev": o.leverage, "min": o.min_capital,
                                  "blev": blev.setdefault(o.bot_id, bot_leverage(o.bot_id))} for o in offers]})
    return {"catalog": catalog}


def _rule_errors(request, gs, kind, exchange, mode, leverage, sizing, pairs, capital, mot, chosen_bot, catalog):
    """Per-pair minimums: the exchange's minimum order at this leverage and the admin's minimum capital."""
    from ..engine.account_check import check_pairs
    from ..engine.offers import forced_leverage
    from ..engine.sizing import pair_budget
    tmp = ExchangeAccount(kind=kind, exchange=exchange, trading_mode=mode, leverage=leverage, sizing_mode=sizing,
                          pairs=pairs, capital=capital, max_open_trades=mot, pair_bot=chosen_bot)
    sel = {}
    from ..engine.offers import bot_leverage
    for pp in pairs:
        b = chosen_bot.get(pp)
        lev = 1 if mode == "spot" else max(1, min(forced_leverage(pp, b) or (bot_leverage(b) if b else None) or leverage,
                                                   gs.max_leverage))
        sel[pp] = {"margin": pair_budget(gs, tmp, pp), "leverage": lev, "bot": chosen_bot.get(pp)}
    items = check_pairs(exchange if kind == "exchange" else "", mode, sel, catalog)
    return [t["text"] for t in check_texts(request, items)]


async def _key_errors(request, gs, acc, exchange, mode, api_key, secret, password, sandbox, dry_run, capital,
                      sizing, pairs) -> list[str]:
    """Blocking problems of the API key (and the balance, unless it is a dry run)."""
    from ..engine.account_check import check_keys
    from ..security import decrypt
    key = api_key or (decrypt(acc.api_key_enc) if acc else "") or ""
    sec = secret or (decrypt(acc.secret_enc) if acc else "") or ""
    pw = password or (decrypt(acc.password_enc) if acc else "") or ""
    if not key or not sec:
        return []  # reported as missing keys already
    res = await check_keys(exchange, mode, key, sec, pw, sandbox, gs.stake_currency)
    if not res["reachable"]:
        flash(request, tr(request, "ck_saved_unchecked"), "warn")
        return []
    texts = check_texts(request, res["items"])
    errs = [x["text"] for x in texts if x["status"] == "error"]
    for x in texts:
        if x["status"] == "warn":
            flash(request, x["text"], "warn")
    bal = res.get("balance")
    need = sum(max(0.0, float(v or 0)) for v in pairs.values()) if sizing == "custom_amounts" else capital
    if not errs and bal and need > bal["total"] + 1e-9:
        msg = tr(request, "ck_not_enough", need=f"{need:,.2f}", have=f"{bal['total']:,.2f}", cur=bal["currency"])
        if dry_run:
            flash(request, msg + " " + tr(request, "ck_dry_ok"), "warn")
        else:
            errs.append(msg)
    return errs


def _server_ip() -> str:
    from .guides import server_ip
    return server_ip()


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
        if kind == "exchange" and gs.require_email_verification and not user.email_verified:
            flash(request, tr(request, "err_verify_first"), "error")
            return RedirectResponse("/settings/security", 303)
        errors = []
        exchange = str(f.get("exchange", "")) if kind == "exchange" else ""
        if kind == "exchange" and exchange not in gs.allowed_exchanges:
            errors.append(tr(request, "err_exchange"))
        mode = "spot" if f.get("trading_mode") == "spot" else "futures"
        from ..engine.offers import offers_by_pair as _obp
        simple = bool(_obp())  # pair catalog in use: the user only gives the total capital
        leverage = int(_num(f.get("leverage"), 1)) if mode == "futures" else 1
        if simple:
            leverage = 1  # never used: the admin's fixed leverage or the bot's own applies
        if not 1 <= leverage <= gs.max_leverage:
            errors.append(tr(request, "err_leverage", max=gs.max_leverage))
        sizing = str(f.get("sizing_mode", ""))
        if simple:
            sizing = "equal_split"
        elif sizing not in (gs.sizing_modes or []):
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
        public_ids = {b.id for b in _public_bots()}
        bots = sorted({int(k.split("__", 1)[1]) for k in f.keys()
                       if k.startswith("bot__") and k.split("__", 1)[1].isdigit()} & public_ids)
        # pair catalog: each offered pair from exactly ONE of its offered bots; mandatory pairs must be included
        from ..engine.offers import offers_by_pair, pair_rules
        catalog = {pp: pair_rules(o) for pp, o in offers_by_pair().items()}
        chosen_bot: dict[str, int] = {}
        for pp in pairs:
            rules = catalog.get(pp)
            v = str(f.get(f"pbot__{pp}", ""))
            pick = int(v) if v.isdigit() else None
            if rules:
                if pick not in rules["bots"]:
                    pick = rules["default_bot"] if pick is None and rules["default_bot"] in rules["bots"] else (
                        rules["bots"][0] if pick is None and len(rules["bots"]) == 1 else None)
                if pick is None:
                    errors.append(tr(request, "err_pick_bot", pair=pp))
                    continue
                chosen_bot[pp] = pick
            elif pick in public_ids:
                chosen_bot[pp] = pick
        for pp, rules in catalog.items():
            if rules["mandatory"] and pp not in pairs:
                errors.append(tr(request, "err_mandatory_pair", pair=pp))
        bots = sorted(set(bots) | set(chosen_bot.values()))
        if not bots:
            errors.append(tr(request, "err_bots"))
        capital = sum(pairs.values()) if sizing == "custom_amounts" else _num(f.get("capital"))
        if capital < gs.min_capital:
            errors.append(tr(request, "err_min_capital", min=f"{gs.min_capital:,.0f}", cur=gs.stake_currency))
        mot = None if simple else (int(_num(f.get("max_open_trades"), 0)) or None)
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
        # exchange rules per pair (listed? order big enough at this leverage?) + the admin's minimums
        if not errors:
            errors += _rule_errors(request, gs, kind, exchange, mode, leverage, sizing, pairs, capital, mot,
                                   chosen_bot, catalog)
        # the API key itself: works, may trade futures, withdrawals off, enough money
        if not errors and kind == "exchange":
            errors += await _key_errors(request, gs, acc, exchange, mode, api_key, secret, password,
                                        bool(f.get("sandbox")), bool(f.get("dry_run")), capital, sizing, pairs)
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
        # per pair: take signals only from one chosen bot (it is followed automatically)
        acc.bots = sorted(bots)
        acc.pair_bot = dict(chosen_bot) or None
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
        exchange = acc.exchange
        s.commit()
    flash(request, msg, "ok" if ok else "error")
    if not ok:  # say what the exchange's error means and how to fix it
        from .guides import explain, server_ip
        hint = explain(msg)
        guide = "/guides/binance" if exchange == "binance" else f"/guides/exchanges#ex-{exchange}"
        text = f"💡 {tr(request, 'g_err_what')} {tr(request, hint, ip=server_ip() or '—')}" if hint else "📘"
        flash(request, text, "warn", guide, tr(request, "g_how") + " →")
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
        bot_names = {b.id: b.name for b in s.scalars(select(Bot))}
    return render(request, "trades.html", trades=rows, acc_names=names, cur=cur, bot_names=bot_names)


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
    keys = [k for k in NOTIFY_KEYS if user.is_admin or k not in ADMIN_NOTIFY]
    from ..db import PushSubscription
    from ..notify.alerts import alert_bot_username
    with SessionLocal() as s:
        devices = list(s.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id)
                                 .order_by(PushSubscription.id)))
    from .common import _local, visitor_tz, zones
    with SessionLocal() as s:
        now_local = _local(utcnow(), visitor_tz(request, user, get_global(s))).strftime("%Y-%m-%d %H:%M")
    return render(request, "settings.html", tz_list=zones(), now_local=now_local,
                  notify={**DEFAULT_NOTIFY, **(user.notify or {})},
                  notify_keys=keys, bot_username=bot.username, devices=devices,
                  alert_bot=await alert_bot_username() if user.is_admin else "")


@router.post("/settings")
async def settings_save(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        action = f.get("action")
        if action == "profile":
            u.notify = {k: bool(f.get(f"n__{k}")) for k in NOTIFY_KEYS if u.is_admin or k not in ADMIN_NOTIFY}
            u.trading_enabled = bool(f.get("trading_enabled"))
            u.name = str(f.get("name", u.name)).strip()[:120]
            from .common import THEMES
            u.theme = str(f.get("theme")) if f.get("theme") in THEMES else None
            u.phone = str(f.get("phone", u.phone)).strip()[:32]
            from .common import valid_tz
            u.timezone = valid_tz(f.get("timezone"))
            flash(request, tr(request, "msg_saved"))
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


# ------------------------------------------------------------------ API key / pairs check (button in the form)
_check_hits: dict[int, list[float]] = {}


def check_texts(request: Request, items: list[dict]) -> list[dict]:
    from .guides import server_ip
    out = []
    for i in items:
        p = dict(i.get("params") or {})
        hint = p.pop("hint", "")
        text = tr(request, i["key"], ip=server_ip() or "—", **p)
        if hint:
            text += " — " + tr(request, hint, ip=server_ip() or "—")
        out.append({"status": i["status"], "text": text})
    return out


@router.post("/accounts/check")
async def account_check(request: Request):
    """Read everything we need from the exchange with these keys and report it, before saving."""
    import hmac
    import time

    from fastapi.responses import JSONResponse

    from ..engine.account_check import check_keys, check_pairs
    from ..engine.offers import offers_by_pair, pair_rules
    user = require_user(request)
    data = await request.json()
    if not hmac.compare_digest(str(data.get("csrf", "")), request.session.get("csrf", "")):
        raise HTTPException(400, "CSRF")
    now = time.time()
    hits = [t for t in _check_hits.get(user.id, []) if now - t < 600]
    if len(hits) >= 20:
        return JSONResponse({"ok": False, "items": [{"status": "error", "text": tr(request, "err_too_many")}]}, 429)
    _check_hits[user.id] = hits + [now]
    exchange, mode = str(data.get("exchange", "")), "spot" if data.get("trading_mode") == "spot" else "futures"
    key, secret, pw = (str(data.get(k, "")).strip() for k in ("api_key", "secret", "password"))
    with SessionLocal() as s:
        gs = get_global(s)
        if exchange not in (gs.allowed_exchanges or []):
            raise HTTPException(400, "exchange")
        acc_id = data.get("acc_id")
        if str(acc_id or "").isdigit() and (not key or not secret):  # editing: use the stored keys
            acc = _own(s, user, int(acc_id))
            from ..security import decrypt
            key, secret = key or decrypt(acc.api_key_enc) or "", secret or decrypt(acc.secret_enc) or ""
            pw = pw or decrypt(acc.password_enc) or ""
        cur = gs.stake_currency
    if not key or not secret:
        return JSONResponse({"ok": False, "items": [{"status": "error", "text": tr(request, "err_keys")}]})
    res = await check_keys(exchange, mode, key, secret, pw, bool(data.get("sandbox")), cur)
    sel = {str(p).upper(): v for p, v in (data.get("pairs") or {}).items() if isinstance(v, dict)}
    rules = {p: pair_rules(o) for p, o in offers_by_pair().items()}
    pair_items = check_pairs(exchange, mode, sel, rules)
    return JSONResponse({"ok": res["ok"] and not pair_items, "keys_ok": res["ok"], "reachable": res["reachable"],
                         "balance": res["balance"], "items": check_texts(request, res["items"] + pair_items)})
