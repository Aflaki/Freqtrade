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
from ..security import current_user, encrypt, flash, form_with_csrf, mask, require_user
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
    """Like the bots' performance page, but for the user's own trades: live positions, results, chart vs the
    market, pairs, closed trades; for all portfolios, one portfolio, or one bot."""
    user = require_user(request)
    from ..engine import livepos, myperf
    from ..engine.streams import manager as streams
    from .public import PERIODS
    qp = request.query_params
    market = qp.get("market") if qp.get("market") in ("long", "short") else "all"
    period = qp.get("days", "all") if qp.get("days", "all") in PERIODS else "all"
    rep = myperf.report(user.id, qp.get("scope", ""), market, PERIODS[period])
    with SessionLocal() as s:
        gs = get_global(s)
        accounts = rep["accounts"]
        all_open = list(s.scalars(select(Trade).where(Trade.user_id == user.id, Trade.status == "open")
                                  .order_by(Trade.opened_at.desc())))
        for t in all_open:
            s.expunge(t)
        n_closed = s.scalar(select(func.count(Trade.id)).where(Trade.user_id == user.id, Trade.status == "closed")) or 0
        wins = s.scalar(select(func.count(Trade.id)).where(
            Trade.user_id == user.id, Trade.status == "closed", Trade.realized_pnl > 0)) or 0
    from ..engine.markets import all_info, convert
    cache = all_info()
    disp, approx = myperf.display_currency(accounts, gs.stake_currency)  # all portfolios together

    def d(v, src):  # money in the dashboard's coin
        return (convert(v, src, disp, cache) or v) if v is not None else None
    with SessionLocal() as s:
        pnl = sum(d(v or 0, (sym or "/USDT").split("/")[-1].split(":")[0]) for sym, v in s.execute(
            select(Trade.symbol, func.sum(Trade.realized_pnl)).where(Trade.user_id == user.id).group_by(Trade.symbol)))
    open_trades = [t for t in all_open if t.account_id in rep["acc_ids"] and (not rep["bot_sel"] or t.bot_id == rep["bot_sel"])
                   and (market == "all" or t.side == market)]
    live = livepos.cached_for_trades(all_open)  # no exchange call here: the page asks /trades/live right after
    acc_open: dict[int, int] = {}
    bot_open: dict[int, int] = {}
    for t in all_open:
        acc_open[t.account_id] = acc_open.get(t.account_id, 0) + 1
        if t.bot_id:
            bot_open[t.bot_id] = bot_open.get(t.bot_id, 0) + 1
    acc_capital = {a.id: effective_capital(gs, a) for a in accounts}
    bals = livepos.cached_balances(accounts, gs.stake_currency)  # live numbers follow from /accounts/balances
    by_id = {a.id: a for a in accounts}
    cur_of = {a.id: a.currency(gs.stake_currency) for a in accounts}
    trade_cur = {t.id: t.currency for t in all_open}
    return render(request, "dashboard.html", bals=bals, disp_cur=disp, approx=approx, cur_of=cur_of,
                  wallet_total=sum(d(b["wallet"] or 0, b.get("cur") or cur_of.get(i, disp)) for i, b in bals.items()),
                  equity_total=sum(d(b["equity"] if b["equity"] is not None else b["wallet"] or 0,
                                     b.get("cur") or cur_of.get(i, disp)) for i, b in bals.items()), gs=gs, accounts=accounts, open_trades=open_trades, all_open=len(all_open),
                  pnl=pnl, n_closed=n_closed, wins=wins, acc_capital=acc_capital,
                  total_capital=sum(d(v, cur_of[i]) for i, v in acc_capital.items() if i in by_id),
                  acc_open=acc_open, bot_open=bot_open, live=live,
                  live_upnl=sum(d(x.get("upnl") or 0, trade_cur.get(k, disp)) for k, x in live.items()),
                  live_win=sum(1 for x in live.values() if (x.get("upnl") or 0) > 0),
                  live_loss=sum(1 for x in live.values() if (x.get("upnl") or 0) < 0),
                  streams={a.id: streams.status(a) for a in accounts},
                  acc_opts=[(f"a{a.id}", "🧺 " + rep["acc_names"][a.id]) for a in accounts],
                  bot_opts=[(f"b{b.id}", "🤖 " + b.name) for b in rep["bots"]],
                  market=market, period=period, periods=list(PERIODS), **{k: rep[k] for k in (
                      "data", "pairs", "acc_cards", "bot_cards", "acc_names", "bot_names", "bots", "acc_sel", "bot_sel",
                      "scope", "scope_label")})


@router.get("/accounts/balances")
async def accounts_balances(request: Request):
    """Wallet / equity / free margin of the user's real portfolios (polled by the dashboard)."""
    from fastapi.responses import JSONResponse

    from ..engine import livepos
    user = require_user(request)
    with SessionLocal() as s:
        cur = get_global(s).stake_currency
        accs = list(s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)))
        for a in accs:
            s.expunge(a)
    out = await livepos.balances(accs, cur)
    disp, cache, _ = _disp(user.id)
    from ..engine.markets import convert
    for b in out.values():
        c = b.get("cur") or cur
        for k in ("wallet", "equity"):
            if b.get(k) is not None:
                b[k + "_d"] = convert(b[k], c, disp, cache) if c != disp else b[k]
    return JSONResponse({str(k): v for k, v in out.items()})


@router.get("/trades/live")
async def trades_live(request: Request):
    """Live price and profit/loss of the user's open trades (polled by the dashboard)."""
    from fastapi.responses import JSONResponse

    from ..engine import livepos
    user = require_user(request)
    with SessionLocal() as s:
        mine = list(s.scalars(select(Trade).where(Trade.user_id == user.id, Trade.status == "open")))
    live = await livepos.for_trades(mine)
    disp, cache, curs = _disp(user.id, mine)
    from ..engine.markets import convert
    for k, v in live.items():
        c = curs.get(k, disp)
        v["cur"] = c
        if v.get("upnl") is not None:
            v["upnl_d"] = convert(v["upnl"], c, disp, cache) if c != disp else v["upnl"]
    return JSONResponse({str(k): {kk: (vv.isoformat() if hasattr(vv, "isoformat") else vv) for kk, vv in v.items()}
                         for k, v in live.items()})


def _disp(user_id: int, trades=None):
    """The dashboard's coin for this user, the market cache, and each trade's coin."""
    from ..engine import myperf
    from ..engine.markets import all_info
    with SessionLocal() as s:
        stake = get_global(s).stake_currency
        accs = list(s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user_id)))
        disp = myperf.display_currency(accs, stake)[0]
    return disp, all_info(), {t.id: t.currency for t in trades or []}


# ------------------------------------------------------------------ portfolios (exchange or signal-only)
def _own(s, user: User, acc_id: int) -> ExchangeAccount:
    acc = s.get(ExchangeAccount, acc_id)
    if acc is None or acc.user_id != user.id:
        raise HTTPException(404)
    return acc


@router.get("/accounts")
async def accounts(request: Request):
    from ..engine.streams import manager as _streams
    user = require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
        accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)).all()
        bot_names = {b.id: b.name for b in s.scalars(select(Bot))}
        rows = [{"acc": a, "key": "" if a.is_signal else mask(a.api_key_enc), "alloc": allocation_table(gs, a),
                 "bots": [bot_names.get(i, f"#{i}") for i in (a.bots or [])],
                 "mode": effective_mode(gs, a), "capital": effective_capital(gs, a),
                 "tradable": tradable_capital(gs, a), "mot": max_open_trades(gs, a),
                 "open": s.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id, Trade.status == "open")),
                 "adopted": s.scalar(select(func.count(Trade.id)).where(Trade.account_id == a.id, Trade.status == "open",
                                                                        Trade.adopted.is_(True))),
                 "stream": _streams.status(a)}
                for a in accs]
        by_hash: dict[str, list] = {}
        for a in accs:
            if a.api_key_hash:
                by_hash.setdefault(a.api_key_hash, []).append(a)
        for r in rows:
            twins = [o for o in by_hash.get(r["acc"].api_key_hash or "", []) if o.id != r["acc"].id]
            r["same_key"] = ", ".join(o.label or o.exchange for o in twins)
    return render(request, "accounts.html", gs=gs, rows=rows)


def _key_owner(s, h: str | None, exclude_id: int | None):
    """Another portfolio (of anyone) that already uses this API key."""
    if not h:
        return None
    q = select(ExchangeAccount).where(ExchangeAccount.api_key_hash == h)
    if exclude_id is not None:
        q = q.where(ExchangeAccount.id != exclude_id)
    return s.scalars(q).first()


def _public_bots() -> list[Bot]:
    with SessionLocal() as s:
        return list(s.scalars(select(Bot).where(Bot.enabled.is_(True), Bot.public.is_(True)).order_by(Bot.sort, Bot.id)))


def _form_ctx(gs, acc, kind, draft=None, issues=None, user=None):
    bots = _public_bots()
    ids = [b.id for b in bots]
    _, groups = perf.visible_bots()
    gids = {g.id for g in groups}
    sections = [(g, [b for b in bots if b.group_id == g.id]) for g in groups]
    sections = [(g, bs) for g, bs in sections if bs]
    rest = [b for b in bots if b.group_id not in gids]
    if rest:
        sections.append((None, rest))
    src = draft or acc  # draft: what the user typed, shown again after an error
    return dict(gs=gs, acc=acc, kind=kind, key_mask=mask(acc.api_key_enc) if acc and not acc.is_signal else "",
                stats_json=perf.stats_json(ids, 90), modes=[m for m in SIZING_MODES if m in (gs.sizing_modes or []) and m != "wallet_pct"],
                bots=bots, cards={m: perf.bot_summaries(ids, m, 90) for m in ("all", "long")},
                followed=(src.bots or []) if src else (ids if len(ids) == 1 else []), sections=sections,
                pair_bot=(src.pair_bot or {}) if src else {}, server_ip=_server_ip(), **_catalog_ctx(gs, bots),
                draft=draft, issues=issues or [], cap_pct=round(_cap(gs, user) * 100, 2))


def _cap(gs, user) -> float:
    from ..engine.sizing import wallet_cap
    return wallet_cap(gs, user)


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
        ex, spot, fut = {}, {}, {}
        base = pair.split("/")[0]
        for e in gs.allowed_exchanges or []:
            pairs = (cache.get(e) or {}).get("pairs")
            if pairs:  # known: listed (min order) or not listed (None)
                i = pairs.get(pair)
                ex[e] = markets.min_order(i) if i else None
            sp = (cache.get(e) or {}).get("spot")
            if sp:  # spot: {quote: [min order, 24h volume]} for this coin; a missing quote = not listed
                spot[e] = {k.split("/")[1]: [markets.min_order(i), i.get("volume")]
                           for k, i in sp.items() if k.split("/")[0] == base}
            fu = (cache.get(e) or {}).get("fut")
            if fu:  # futures: {settle coin: [min order, 24h volume, max leverage]}
                fut[e] = {k.split("/")[1]: [markets.min_order(i), i.get("volume"), i.get("max_leverage")]
                          for k, i in fu.items() if k.split("/")[0] == base}
        catalog.append({"pair": pair, "mandatory": r["mandatory"], "default_bot": r["default_bot"], "ex": ex, "spot": spot, "fut": fut,
                        "bots": [{"id": o.bot_id, "name": names[o.bot_id], "lev": o.leverage, "min": o.min_capital,
                                  "blev": blev.setdefault(o.bot_id, bot_leverage(o.bot_id))} for o in offers]})
    from ..engine.exchange import QUOTES, SPOT_ONLY
    quotes = {e: markets.quotes_on(e, cache) for e in gs.allowed_exchanges or []}
    fut_quotes = {e: markets.quotes_on(e, cache, mode="futures") for e in gs.allowed_exchanges or []}
    rates = {q: markets.usd_rate(q, cache) for q in QUOTES}
    return {"catalog": catalog, "spot_quotes": quotes, "fut_quotes": fut_quotes, "all_quotes": list(QUOTES), "usd_rates": rates, "spot_only": sorted(SPOT_ONLY),
            "shallow": markets.SHALLOW_VOLUME}


def _rule_errors(request, gs, kind, exchange, mode, leverage, sizing, pairs, capital, mot, chosen_bot, catalog,
                 quote: str = "", **wallet):
    """Per-pair minimums: the exchange's minimum order at this leverage and the admin's minimum capital."""
    from ..engine.account_check import check_pairs
    from ..engine.offers import forced_leverage
    from ..engine.sizing import pair_budget
    tmp = ExchangeAccount(kind=kind, exchange=exchange, trading_mode=mode, leverage=leverage, sizing_mode=sizing,
                          pairs=pairs, capital=capital, max_open_trades=mot, pair_bot=chosen_bot, **wallet)
    sel = {}
    from ..engine.offers import bot_leverage
    for pp in pairs:
        b = chosen_bot.get(pp)
        lev = 1 if mode == "spot" else max(1, min(forced_leverage(pp, b) or (bot_leverage(b) if b else None) or leverage,
                                                   gs.max_leverage))
        sel[pp] = {"margin": pair_budget(gs, tmp, pp), "leverage": lev, "bot": chosen_bot.get(pp)}
    items = check_pairs(exchange if kind == "exchange" else "", mode, sel, catalog, quote=quote, stake=gs.stake_currency)
    for i, x in zip(items, check_texts(request, items)):
        i["text"] = x["text"]
    return items


async def _key_errors(request, gs, acc, exchange, mode, api_key, secret, password, cur, sandbox, dry_run, capital,
                      sizing, pairs, cap: float = 1.0) -> tuple[list[str], dict | None]:
    """Blocking problems of the API key (and the money, unless it is a dry run), and the balance read."""
    from ..engine.account_check import check_keys
    from ..security import decrypt
    key = api_key or (decrypt(acc.api_key_enc) if acc else "") or ""
    sec = secret or (decrypt(acc.secret_enc) if acc else "") or ""
    pw = password or (decrypt(acc.password_enc) if acc else "") or ""
    if not key or not sec:
        return [], None  # reported as missing keys already
    res = await check_keys(exchange, mode, key, sec, pw, sandbox, cur)
    if not res["reachable"]:
        flash(request, tr(request, "ck_saved_unchecked"), "warn")
        return [], None
    texts = check_texts(request, res["items"])
    errs = [x["text"] for x in texts if x["status"] == "error"]
    for x in texts:
        if x["status"] == "warn":
            flash(request, x["text"], "warn")
    bal = res.get("balance")
    need = sum(max(0.0, float(v or 0)) for v in pairs.values()) if sizing == "custom_amounts" else capital
    if not errs and bal and sizing != "wallet_pct":
        wallet = bal.get("wallet", bal["total"])
        msg = ""
        if need > bal["total"] + 1e-9:
            msg = tr(request, "ck_not_enough", need=f"{need:,.2f}", have=f"{bal['total']:,.2f}", cur=bal["currency"])
        elif need > wallet * cap + 1e-9:  # the rest of the wallet is the reserve for drawdowns
            msg = tr(request, "ck_over_cap", need=f"{need:,.2f}", pct=f"{cap * 100:g}", max=f"{wallet * cap:,.2f}",
                     have=f"{wallet:,.2f}", cur=bal["currency"])
        if msg and dry_run:
            flash(request, msg + " " + tr(request, "ck_dry_ok"), "warn")
        elif msg:
            errs.append(msg)
    return errs, bal


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
    return render(request, "account_form.html", **_form_ctx(gs, None, kind, user=current_user(request)))


@router.get("/accounts/{acc_id}/edit")
async def account_edit(acc_id: int, request: Request):
    user = require_user(request)
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id)
    return render(request, "account_form.html", **_form_ctx(gs, acc, acc.kind, user=user))


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
        from ..engine import markets as _mk
        from ..engine.exchange import EXCHANGES as _EX, QUOTES, SPOT_ONLY
        if kind == "exchange" and exchange in SPOT_ONLY and mode != "spot":
            errors.append(tr(request, "err_spot_only", ex=_EX.get(exchange, exchange)))
        quote = ""  # the coin this portfolio trades in: BTC/USDT -> BTC/<quote> spot or the BTC perpetual settled in it
        if kind == "exchange":
            have = _mk.quotes_on(exchange, mode=mode)
            default = gs.stake_currency if not have or gs.stake_currency in have else have[0]
            quote = str(f.get("quote", "")).strip().upper() or default
            if quote not in QUOTES or (have and quote not in have):
                errors.append(tr(request, "err_quote", quote=quote, ex=_EX.get(exchange, exchange)))
            if mode == "futures" and quote == gs.stake_currency:
                quote = ""  # the bots' own coin: nothing to convert (as before)
        cur = quote or gs.stake_currency
        from ..engine.offers import offers_by_pair as _obp
        simple = bool(_obp())  # pair catalog in use: the user only gives the total capital
        leverage = int(_num(f.get("leverage"), 1)) if mode == "futures" else 1
        if simple:
            leverage = 1  # never used: the admin's fixed leverage or the bot's own applies
        if not 1 <= leverage <= gs.max_leverage:
            errors.append(tr(request, "err_leverage", max=gs.max_leverage))
        sizing = str(f.get("sizing_mode", ""))
        if simple:  # one total split equally, the user's own amount per pair, or a share of the wallet
            ok = ("equal_split", "custom_amounts", "wallet_pct") if kind == "exchange" else ("equal_split", "custom_amounts")
            sizing = sizing if sizing in ok else "equal_split"
        elif sizing not in (gs.sizing_modes or []):
            sizing = (gs.sizing_modes or ["equal_split"])[0]
        pairs: dict[str, float] = {}
        for p in gs.allowed_pairs:
            if f.get(f"pair__{p}"):
                v = _num(f.get(f"value__{p}"), 0 if sizing == "custom_amounts" else 1.0)
                if sizing == "custom_amounts" and v <= 0:
                    errors.append(tr(request, "err_amount_pair", pair=p))
                pairs[p] = v if v > 0 else 1.0
        # pair catalog: each offered pair from exactly ONE of its offered bots; mandatory pairs must be included
        from ..engine.offers import offers_by_pair, pair_rules
        catalog = {pp: pair_rules(o) for pp, o in offers_by_pair().items()}
        # the user's answers to earlier pair problems: drop a pair, or keep one that is only closed for now
        removed = [pp for pp in pairs if f.get(f"pfix__{pp}") == "remove" and not (catalog.get(pp) or {}).get("mandatory")]
        for pp in removed:
            pairs.pop(pp)
            errors = [e for e in errors if pp not in e]
        if removed:
            flash(request, tr(request, "msg_pairs_removed", pairs=", ".join(removed)), "warn")
        if not pairs:
            errors.append(tr(request, "err_pairs"))
        public_ids = {b.id for b in _public_bots()}
        bots = sorted({int(k.split("__", 1)[1]) for k in f.keys()
                       if k.startswith("bot__") and k.split("__", 1)[1].isdigit()} & public_ids)
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
        from ..engine.sizing import wallet_cap
        cap = wallet_cap(gs, user)
        wallet_pct = _num(f.get("wallet_pct"), 0) if sizing == "wallet_pct" else None
        min_stake = (_num(f.get("min_stake"), 0) or None) if sizing == "wallet_pct" else None
        if sizing == "wallet_pct":
            if not 1 <= (wallet_pct or 0) <= cap * 100 + 1e-9:
                errors.append(tr(request, "err_wallet_pct", max=f"{cap * 100:g}"))
        elif (_mk.convert(capital, cur, gs.stake_currency) or capital) < gs.min_capital:
            errors.append(tr(request, "err_min_capital", min=f"{_mk.convert(gs.min_capital, gs.stake_currency, cur) or gs.min_capital:,.0f}",
                             cur=cur))
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
        new_hash = None
        if kind == "exchange" and not errors:
            from ..db import key_hash
            from ..security import decrypt
            eff_key = api_key or (decrypt(acc.api_key_enc) if acc and acc.exchange == exchange else "") or ""
            new_hash = key_hash(exchange, eff_key) if eff_key else None
            other = _key_owner(s, new_hash, acc.id if acc else None)
            if other is not None:
                errors.append(tr(request, "err_key_in_use", name=other.label or other.exchange)
                              if other.user_id == user.id else tr(request, "err_key_in_use_other"))
        # the API key itself: works, may trade futures, withdrawals off, enough money (and the balance)
        bal = None
        if not errors and kind == "exchange":
            errs, bal = await _key_errors(request, gs, acc, exchange, mode, api_key, secret, password, cur,
                                          bool(f.get("sandbox")), bool(f.get("dry_run")), capital, sizing, pairs, cap)
            errors += errs
        wallet_now = (bal or {}).get("wallet", (bal or {}).get("total")) if bal else (acc.wallet_balance if acc else None)
        if sizing == "wallet_pct":
            capital = (wallet_now or 0) * (wallet_pct or 0) / 100
            if not errors and wallet_now is not None and capital < gs.min_capital:
                errors.append(tr(request, "err_min_capital", min=f"{gs.min_capital:,.0f}", cur=gs.stake_currency))
        # exchange rules per pair (listed? order big enough at this leverage?) + the admin's minimums
        issues: list[dict] = []
        if not errors:
            wallet = {"wallet_pct": wallet_pct, "min_stake": min_stake, "wallet_balance": wallet_now}
            for it in _rule_errors(request, gs, kind, exchange, mode, leverage, sizing, pairs, capital, mot,
                                   chosen_bot, catalog, quote=quote, **wallet):
                pp = it["params"].get("pair", "")
                if it["key"] == "ck_pair_inactive" and f.get(f"pfix__{pp}") == "keep":
                    continue  # the user keeps it: its trades are skipped while the exchange has it closed
                issues.append({"pair": pp, "key": it["key"], "text": it["text"],
                               "can_keep": it["key"] == "ck_pair_inactive",
                               "mandatory": bool((catalog.get(pp) or {}).get("mandatory"))})
            if issues:
                errors.append(tr(request, "err_pairs_decide"))
        if errors:  # show the form again with everything the user typed, and ask about problem pairs
            for e in errors:
                flash(request, e, "error")
            from types import SimpleNamespace
            draft = SimpleNamespace(
                id=acc.id if acc else None, label=str(f.get("label", "")).strip()[:80], exchange=exchange,
                trading_mode=mode, leverage=leverage, margin_mode="cross" if f.get("margin_mode") == "cross" else "isolated",
                sizing_mode=sizing, pairs={**pairs, **{i["pair"]: pairs.get(i["pair"], 1.0) for i in issues}},
                capital=_num(f.get("capital")) or (capital if sizing != "custom_amounts" else 0), max_open_trades=mot,
                bots=bots, pair_bot=chosen_bot, stoploss_pct=sl, daily_loss_limit_pct=dll, dry_run=bool(f.get("dry_run")),
                sandbox=bool(f.get("sandbox")), api_key=api_key, secret=secret, password=password,
                no_withdraw=bool(f.get("no_withdraw")), quote=quote, wallet_pct=wallet_pct or _num(f.get("wallet_pct"), 0) or None,
                min_stake=_num(f.get("min_stake"), 0) or None, wallet_balance=wallet_now)
            return render(request, "account_form.html", **_form_ctx(gs, acc, kind, draft, issues, user))
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
        acc.wallet_pct, acc.min_stake = wallet_pct, min_stake
        acc.quote = quote
        if wallet_now is not None and bal:
            acc.wallet_balance, acc.wallet_balance_at = wallet_now, utcnow()
        # per pair: take signals only from one chosen bot (it is followed automatically)
        acc.bots = sorted(bots)
        acc.pair_bot = dict(chosen_bot) or None
        acc.stoploss_pct, acc.daily_loss_limit_pct = sl, dll
        if kind == "exchange":
            acc.sandbox = bool(f.get("sandbox"))
            acc.dry_run = bool(f.get("dry_run"))
            if api_key:
                acc.api_key_enc = encrypt(api_key)
            acc.api_key_hash = new_hash or acc.api_key_hash
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
    return await _after_activation(request, new_id)


async def _after_activation(request: Request, acc_id: int) -> RedirectResponse:
    """Positions already open on the exchange: take over those in the chosen pairs, ask about the others."""
    from ..engine.positions import can_scan, sync_account
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        scan = acc is not None and acc.enabled and can_scan(acc)
    if not scan:
        return RedirectResponse("/accounts", 303)
    res = await sync_account(acc_id)
    if res["error"]:
        flash(request, tr(request, "pos_scan_failed", err=res["error"]), "warn")
    if res["adopted"]:
        flash(request, tr(request, "pos_adopted_n", n=len(res["adopted"]),
                          pairs=", ".join(f"{p['pair']} {p['side'].upper()}" for p in res["adopted"])))
    if res["undecided"]:
        flash(request, tr(request, "pos_ask_n", n=len(res["undecided"])), "warn")
        return RedirectResponse(f"/accounts/{acc_id}/positions", 303)
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
        enabled = acc.enabled
        flash(request, tr(request, "msg_enabled" if acc.enabled else "msg_disabled"))
    if enabled:
        return await _after_activation(request, acc_id)
    return RedirectResponse("/accounts", 303)


# ------------------------------------------------------------------ positions already on the exchange
@router.get("/accounts/{acc_id}/positions")
async def account_positions(acc_id: int, request: Request):
    user = require_user(request)
    from ..engine import livepos
    from ..engine.positions import bot_trade, can_scan
    with SessionLocal() as s:
        mine = list(s.scalars(select(Trade).where(Trade.account_id == acc_id, Trade.user_id == user.id,
                                                  Trade.status == "open", Trade.adopted.is_(True))))
    live = await livepos.for_trades(mine)  # also brings entry, size and open time in line with the exchange
    from ..engine import reconcile
    recon = await reconcile.compare(acc_id)
    with SessionLocal() as s:
        gs = get_global(s)
        acc = _own(s, user, acc_id)
        if not can_scan(acc):
            raise HTTPException(404)
        bot_names = {b.id: b.name for b in s.scalars(select(Bot))}
        adopted = []
        for tr_ in s.scalars(select(Trade).where(Trade.account_id == acc.id, Trade.status == "open",
                                                 Trade.adopted.is_(True)).order_by(Trade.id)):
            bt = bot_trade(tr_.bot_id, tr_.pair, tr_.side)
            match = "same" if bt and not bt.get("opposite") else ("opposite" if bt else "none")
            adopted.append({"t": tr_, "bot": bot_names.get(tr_.bot_id, "—"), "match": match, "L": live.get(tr_.id, {})})
        kept = set(acc.kept_positions or [])
        foreign = [{**p, "kept": p["key"] in kept} for p in acc.foreign_positions or []]
        s.expunge(acc)
    return render(request, "positions.html", gs=gs, acc=acc, adopted=adopted, foreign=foreign, recon=recon,
                  bot_names=bot_names)


@router.post("/accounts/{acc_id}/reconcile")
async def account_reconcile(acc_id: int, request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    from ..engine import reconcile
    with SessionLocal() as s:
        _own(s, user, acc_id)
    decisions = {k[3:]: str(v) for k, v in f.items() if k.startswith("r__") and str(v) in ("close", "keep", "open", "skip")}
    out = await reconcile.act(acc_id, user.id, decisions)
    for r in out["closed"]:
        flash(request, tr(request, "rc_closed", pair=r["pair"], side=r["side"].upper()))
    for r in out["opened"]:
        flash(request, tr(request, "rc_opened", pair=r["pair"], side=r["side"].upper()))
    if out["kept"]:
        flash(request, tr(request, "rc_kept", n=len(out["kept"])))
    for r in out["failed"]:
        flash(request, tr(request, "rc_failed", pair=r["pair"], err=r.get("error") or r.get("message") or "?"), "error")
    return RedirectResponse(f"/accounts/{acc_id}/positions#recon", 303)


@router.post("/accounts/{acc_id}/positions/scan")
async def account_positions_scan(acc_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    from ..engine.positions import sync_account
    with SessionLocal() as s:
        _own(s, user, acc_id)
    res = await sync_account(acc_id)
    if res["error"]:
        flash(request, tr(request, "pos_scan_failed", err=res["error"]), "error")
    else:
        flash(request, tr(request, "pos_scanned", a=len(res["adopted"]), n=len(res["foreign"])))
    return RedirectResponse(f"/accounts/{acc_id}/positions", 303)


@router.post("/accounts/{acc_id}/positions")
async def account_positions_decide(acc_id: int, request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    from ..engine.positions import decide
    with SessionLocal() as s:
        _own(s, user, acc_id)
    decisions = {k[3:]: str(v) for k, v in f.items() if k.startswith("d__") and str(v) in ("close", "keep")}
    out = await decide(acc_id, decisions)
    for p in out["closed"]:
        flash(request, tr(request, "pos_closed_one", pair=p["pair"], side=p["side"].upper(), price=f"{p['price']:g}"))
    if out["kept"]:
        flash(request, tr(request, "pos_kept_n", n=len(out["kept"])))
    for p in out["failed"]:
        flash(request, tr(request, "pos_close_failed", pair=p["pair"], err=p["error"]), "error")
    return RedirectResponse(f"/accounts/{acc_id}/positions" if out["failed"] else "/accounts", 303)


@router.post("/accounts/{acc_id}/delete")
async def account_delete(acc_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    f = await request.form()
    how = str(f.get("open", ""))  # what to do with open positions: "leave" on the exchange, or "close" them
    with SessionLocal() as s:
        acc = _own(s, user, acc_id)
        open_ids = list(s.scalars(select(Trade.id).where(Trade.account_id == acc.id, Trade.status == "open")))
    if open_ids and how == "close":
        for tid in open_ids:
            await force_exit(tid, user.id)
        with SessionLocal() as s:
            left = s.scalar(select(func.count(Trade.id)).where(Trade.account_id == acc_id, Trade.status == "open"))
        if left:
            flash(request, tr(request, "err_delete_close_failed", n=left), "error")
            return RedirectResponse("/accounts", 303)
    elif open_ids and how != "leave":
        flash(request, tr(request, "err_delete_open"), "error")
        return RedirectResponse("/accounts", 303)
    with SessionLocal() as s:
        acc = _own(s, user, acc_id)
        s.delete(acc)  # its trades and orders go with it (the positions themselves stay on the exchange)
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
        cur = str(data.get("quote") or "").upper() or gs.stake_currency
        stake = gs.stake_currency
    if not key or not secret:
        return JSONResponse({"ok": False, "items": [{"status": "error", "text": tr(request, "err_keys")}]})
    from ..db import key_hash
    with SessionLocal() as s:
        other = _key_owner(s, key_hash(exchange, key), int(acc_id) if str(acc_id or "").isdigit() else None)
        if other is not None:
            text = (tr(request, "err_key_in_use", name=other.label or other.exchange) if other.user_id == user.id
                    else tr(request, "err_key_in_use_other"))
            return JSONResponse({"ok": False, "items": [{"status": "error", "text": text}]})
    res = await check_keys(exchange, mode, key, secret, pw, bool(data.get("sandbox")), cur)
    sel = {str(p).upper(): v for p, v in (data.get("pairs") or {}).items() if isinstance(v, dict)}
    rules = {p: pair_rules(o) for p, o in offers_by_pair().items()}
    pair_items = check_pairs(exchange, mode, sel, rules, quote=cur, stake=stake)
    from ..engine.account_check import position_items
    pos_items = position_items(res.get("positions") or [], set(sel))
    res["items"] += pos_items
    return JSONResponse({"ok": res["ok"] and not pair_items, "keys_ok": res["ok"], "reachable": res["reachable"],
                         "balance": res["balance"], "items": check_texts(request, res["items"] + pair_items)})
