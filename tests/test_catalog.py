"""Pair catalog (offers per bot), exchange minimums, API key check, and the portfolio form's rules."""
import asyncio
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.db import (Bot, BotTrade, ExchangeAccount, PairOffer, SessionLocal, User, get_global, init_db, utcnow)
from app.engine import account_check, markets
from app.engine import exchange as exmod
from app.engine.offers import bot_may_send, forced_leverage, migrate_offers, sync_allowed_pairs
from app.main import app
from app.security import hash_password

MARKETS = {f"{b}/USDT:USDT": {"symbol": f"{b}/USDT:USDT", "base": b, "quote": "USDT", "settle": "USDT", "swap": True,
                              "linear": True, "active": True, "contractSize": 1,
                              "limits": {"cost": {"min": mc}, "amount": {"min": ma}, "leverage": {"max": 50}}}
           for b, mc, ma in (("BTC", 100, 0.001), ("ETH", 20, 0.001), ("SSV", 5, 0.1))}


class FakeBinance:
    """Public markets + a configurable private side."""
    has = {"fetchPositionMode": True, "fetchPositions": True}
    perms = {"enableReading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
    hedged, positions, balance, fail = False, [], 500.0, None

    def __init__(self, params):
        self.params = params

    async def load_markets(self):
        return MARKETS

    async def fetch_tickers(self, symbols=None):
        return {"BTC/USDT:USDT": {"last": 100000}, "ETH/USDT:USDT": {"last": 3000}, "SSV/USDT:USDT": {"last": 2}}

    async def fetch_balance(self):
        if FakeBinance.fail:
            raise FakeBinance.fail
        return {"free": {"USDT": self.balance}, "total": {"USDT": self.balance}}

    async def sapi_get_account_apirestrictions(self):
        return dict(FakeBinance.perms)

    async def fetch_position_mode(self, symbol=None):
        return {"hedged": FakeBinance.hedged}

    async def fetch_positions(self, symbols=None):
        return list(FakeBinance.positions)

    async def close(self):
        pass


@pytest.fixture
def env(monkeypatch):
    init_db()
    monkeypatch.setattr(exmod.ccxt, "binanceusdm", FakeBinance)
    FakeBinance.perms = {"enableReading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
    FakeBinance.hedged, FakeBinance.positions, FakeBinance.balance, FakeBinance.fail = False, [], 500.0, None
    with SessionLocal() as s:
        gs = get_global(s)
        saved = (list(gs.allowed_pairs or []), list(gs.allowed_exchanges or []), gs.market_cache, gs.min_capital,
                 gs.max_leverage, gs.offers_migrated, gs.sizing_modes)
        gs.allowed_exchanges, gs.min_capital, gs.max_leverage = ["binance"], 10, 10
        gs.sizing_modes = ["equal_split", "pair_weights", "custom_amounts"]
        a = Bot(name="CatA", webhook_secret="cat-a", state={"whitelist": ["BTC/USDT:USDT", "ETH/USDT", "SSV/USDT"]})
        b = Bot(name="CatB", webhook_secret="cat-b", state={"whitelist": ["BTC/USDT", "SSV/USDT"]})
        s.add_all([a, b])
        s.commit()
        ids = (a.id, b.id)
        for i, (bot, pair, r) in enumerate([(a.id, "BTC/USDT", 0.2), (b.id, "BTC/USDT", -0.1), (a.id, "ETH/USDT", 0.1)]):
            s.add(BotTrade(bot_id=bot, ft_trade_id=900000 + i, pair=pair, side="short", leverage=5, is_open=False,
                           open_date=utcnow() - dt.timedelta(days=3), close_date=utcnow() - dt.timedelta(days=2),
                           profit_ratio=r, profit_abs=r * 100, source="api"))
        s.commit()
    asyncio.run(markets.refresh(["binance"]))
    yield ids
    with SessionLocal() as s:
        s.query(PairOffer).delete()
        s.query(ExchangeAccount).filter(ExchangeAccount.label.like("cat-%")).delete(synchronize_session=False)
        s.query(User).filter(User.email.like("cat-%")).delete(synchronize_session=False)
        s.query(BotTrade).filter(BotTrade.bot_id.in_(ids)).delete(synchronize_session=False)
        s.query(Bot).filter(Bot.id.in_(ids)).delete(synchronize_session=False)
        gs = get_global(s)
        (gs.allowed_pairs, gs.allowed_exchanges, gs.market_cache, gs.min_capital, gs.max_leverage, gs.offers_migrated,
         gs.sizing_modes) = saved
        s.commit()


def offer(pair, bot, **kw):
    with SessionLocal() as s:
        s.add(PairOffer(pair=pair, bot_id=bot, **{"enabled": True, **kw}))
        sync_allowed_pairs(s)
        s.commit()


# ------------------------------------------------------------------ offers + dispatcher
def test_offers_rules_and_dispatch(env):
    from app.engine.dispatcher import eligible_accounts
    from app.signal import Signal
    a, b = env
    offer("BTC/USDT", a)
    offer("BTC/USDT", b, is_default=True)
    with SessionLocal() as s:
        assert "BTC/USDT" in get_global(s).allowed_pairs
        assert bot_may_send(s, "BTC/USDT", a) and bot_may_send(s, "BTC/USDT", b)
        assert bot_may_send(s, "XRP/USDT", a)  # unmanaged pair: old behaviour
        u = User(email="cat-u@x.com", name="U", password_hash="x", status="active", trading_enabled=True)
        s.add(u)
        s.commit()
        chose_a = ExchangeAccount(user_id=u.id, kind="signal", label="cat-1", pairs={"BTC/USDT": 1}, bots=[a, b],
                                  pair_bot={"BTC/USDT": a})
        legacy = ExchangeAccount(user_id=u.id, kind="signal", label="cat-2", pairs={"BTC/USDT": 1}, bots=[a, b])
        s.add_all([chose_a, legacy])
        gs = get_global(s)
        gs.signal_only_enabled, gs.entries_enabled = True, True
        s.commit()
        ids = (chose_a.id, legacy.id)
    sig = Signal(action="short", symbol="BTC/USDT:USDT")
    from_a, from_b = eligible_accounts(sig, "BTC/USDT", a), eligible_accounts(sig, "BTC/USDT", b)
    assert ids[0] in from_a and ids[0] not in from_b  # takes BTC only from its chosen bot
    assert ids[1] in from_b and ids[1] not in from_a  # no choice saved: only from the default bot, never twice
    with SessionLocal() as s:  # withdraw the offer from bot b: b may not send BTC any more
        s.query(PairOffer).filter_by(pair="BTC/USDT", bot_id=b).update({"enabled": False})
        s.commit()
        assert not bot_may_send(s, "BTC/USDT", b)
    assert eligible_accounts(sig, "BTC/USDT", b) == []


def test_migration_from_old_allowed_list(env):
    a, b = env
    with SessionLocal() as s:
        gs = get_global(s)
        gs.offers_migrated, gs.allowed_pairs = False, ["BTC/USDT", "ETH/USDT", "XRP/USDT"]
        s.commit()
    migrate_offers()
    with SessionLocal() as s:
        rows = {(o.pair, o.bot_id): o for o in s.query(PairOffer)}
    assert set(rows) == {("BTC/USDT", a), ("BTC/USDT", b), ("ETH/USDT", a)}  # XRP: no bot trades it -> left unmanaged
    assert rows[("ETH/USDT", a)].is_default and not rows[("BTC/USDT", a)].is_default


# ------------------------------------------------------------------ exchange rules
def test_market_minimums(env):
    i = markets.info("BTC/USDT", "binance")
    assert markets.min_order(i) == 100 and markets.min_margin(i, 5) == 20
    assert markets.min_order(markets.info("SSV/USDT", "binance")) == 5
    assert markets.info("DOGE/USDT", "binance") is None


def test_check_pairs(env):
    a, _ = env
    rules = {"SSV/USDT": {"min_capital": {a: 50}}}
    items = account_check.check_pairs("binance", "futures", {
        "BTC/USDT": {"margin": 15, "leverage": 5, "bot": a}, "ETH/USDT": {"margin": 30, "leverage": 3, "bot": a},
        "SSV/USDT": {"margin": 20, "leverage": 5, "bot": a}, "DOGE/USDT": {"margin": 99, "leverage": 5, "bot": a}}, rules)
    by = {(i["key"], i["params"]["pair"]) for i in items}
    assert by == {("ck_pair_below_exchange", "BTC/USDT"), ("ck_pair_below_admin", "SSV/USDT"),
                  ("ck_pair_not_listed", "DOGE/USDT")}


# ------------------------------------------------------------------ API key check
def run_check(**kw):
    return asyncio.run(account_check.check_keys("binance", "futures", "k", "s", **kw))


def test_check_keys(env):
    r = run_check()
    assert r["ok"] and r["balance"]["total"] == 500 and {i["key"] for i in r["items"]} >= {"ck_key_ok", "ck_futures_on",
                                                                                           "ck_withdraw_off", "ck_ip_on"}
    FakeBinance.perms = {**FakeBinance.perms, "enableWithdrawals": True, "ipRestrict": False}
    r = run_check()
    assert not r["ok"] and ("error", "ck_withdraw_on") in {(i["status"], i["key"]) for i in r["items"]}
    assert ("warn", "ck_ip_off") in {(i["status"], i["key"]) for i in r["items"]}
    FakeBinance.perms = {**FakeBinance.perms, "enableWithdrawals": False, "enableFutures": False}
    assert ("error", "ck_futures_off") in {(i["status"], i["key"]) for i in run_check()["items"]}
    FakeBinance.perms["enableFutures"] = True
    FakeBinance.hedged, FakeBinance.positions = True, [{"symbol": "XRP/USDT:USDT", "contracts": 3}]
    keys = {(i["status"], i["key"]) for i in run_check()["items"]}
    assert ("warn", "ck_hedge") in keys and ("ok", "ck_positions") in keys  # no complaint: taken over / asked
    FakeBinance.fail = exmod.ccxt.AuthenticationError('{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')
    r = run_check()
    assert not r["ok"] and r["items"][0]["key"] == "ck_key_bad" and r["items"][0]["params"]["hint"] == "g_err_2015"
    FakeBinance.fail = exmod.ccxt.NetworkError("timeout")
    r = run_check()
    assert not r["reachable"] and r["items"][0]["key"] == "ck_unreachable"


# ------------------------------------------------------------------ admin catalog page
def login(email, admin=False):
    from app.auth import captcha
    captcha.MIN_SECONDS = 0
    with SessionLocal() as s:
        u = s.query(User).filter_by(email=email).first()
        if u is None:
            s.add(User(email=email, name="C", password_hash=hash_password("Xy-12345678-zz"), status="active",
                       is_admin=admin, lang="en", trading_enabled=True))
            s.commit()
    c = TestClient(app)
    page = c.get("/login").text
    data = {k: re.search(rf'name="{k}" value="([^"]+)"', page).group(1) for k in ("csrf", "ft")}
    c.post("/login", data={**data, "email": email, "password": "Xy-12345678-zz"})
    return c, re.search(r'name="csrf" value="([^"]+)"', c.get("/settings").text).group(1)


def test_admin_catalog_autosave_and_bulk(env):
    a, b = env
    c, csrf = login("cat-admin@x.com", admin=True)
    html = c.get("/admin/pairs").text
    assert 'data-pair="BTC/USDT"' in html and "CatA" in html and "≥100$" in html
    set_ = lambda **kw: c.post("/admin/pairs/set", json={"csrf": csrf, **kw})  # noqa: E731
    assert set_(pair="BTC/USDT", bot_id=a, field="enabled", value=True).status_code == 200
    assert set_(pair="BTC/USDT", bot_id=b, field="is_default", value=True).status_code == 200
    assert set_(pair="BTC/USDT", bot_id=a, field="min_capital", value="25").status_code == 200
    assert set_(pair="BTC/USDT", bot_id=a, field="leverage", value="3").status_code == 200
    assert set_(pair="BTC/USDT", field="mandatory", value=True).status_code == 200
    assert c.post("/admin/pairs/set", json={"csrf": "bad", "pair": "BTC/USDT", "bot_id": a, "field": "enabled",
                                            "value": True}).status_code == 400
    with SessionLocal() as s:
        rows = {o.bot_id: o for o in s.query(PairOffer).filter_by(pair="BTC/USDT")}
        assert rows[a].enabled and rows[a].min_capital == 25 and rows[a].leverage == 3 and rows[a].mandatory
        assert rows[b].enabled and rows[b].is_default and not rows[a].is_default  # default implies offered
        assert "BTC/USDT" in get_global(s).allowed_pairs
    assert forced_leverage("BTC/USDT", a) == 3 and forced_leverage("BTC/USDT", b) is None
    c.post("/admin/pairs/bulk", data={"csrf": csrf, "action": "best_default", "days": "90"})
    with SessionLocal() as s:
        assert s.query(PairOffer).filter_by(pair="BTC/USDT", bot_id=a).one().is_default  # +20 beats -10
    c.post("/admin/pairs/bulk", data={"csrf": csrf, "action": "offer_bot", "bot_id": str(b)})
    with SessionLocal() as s:
        assert s.query(PairOffer).filter_by(pair="SSV/USDT", bot_id=b, enabled=True).count() == 1
    c.post("/admin/pairs/bulk", data={"csrf": csrf, "action": "withdraw_all"})
    with SessionLocal() as s:
        assert s.query(PairOffer).filter_by(enabled=True).count() == 0
        assert "BTC/USDT" not in get_global(s).allowed_pairs
    assert TestClient(app).get("/admin/pairs", follow_redirects=False).status_code in (303, 403)


# ------------------------------------------------------------------ the user's portfolio form
def save(c, csrf, **fields):
    data = {"csrf": csrf, "kind": "exchange", "label": "cat-p", "exchange": "binance", "trading_mode": "futures",
            "api_key": "k", "secret": "s", "no_withdraw": "on", **fields}
    return c.post("/accounts/new", data=data)


def test_form_rules(env):
    """Catalog portfolios: a total split equally (or an amount per pair, see below); leverage is the admin's/bot's."""
    a, b = env
    with SessionLocal() as s:
        get_global(s).tradable_balance_ratio = 0.9
        s.commit()
    offer("BTC/USDT", a)
    offer("BTC/USDT", b)  # two bots, no default -> user must choose
    offer("ETH/USDT", a, mandatory=True, leverage=3)
    offer("SSV/USDT", b, min_capital=50)
    c, csrf = login("cat-user@x.com")
    form = c.get("/accounts/new?kind=exchange").text
    assert 'class="catrow" data-p="BTC/USDT"' in form and "🔒" in form
    assert 'name="max_open_trades"' not in form and 'type="number" name="leverage"' not in form
    assert 'value="custom_amounts"' in form and 'class="pv"' in form  # per-pair amounts are a choice
    errs = save(c, csrf, capital="300", **{"pair__BTC/USDT": "on"}).text
    assert "Choose which bot BTC/USDT" in errs and "ETH/USDT is mandatory" in errs
    # 60 x 90% / 3 pairs = 18 per pair: BTC at the bot's own 5x needs 20 (Binance min 100); SSV's admin minimum is 50
    errs = save(c, csrf, capital="60", leverage="10", **{"pair__BTC/USDT": "on", "pbot__BTC/USDT": str(a),
                                                         "pair__ETH/USDT": "on", "pair__SSV/USDT": "on"}).text
    assert "BTC/USDT: margin per trade is 18.00 but the exchange needs at least 20.00 at 5x" in errs
    assert "SSV/USDT: this pair" in errs
    # nothing typed is lost, and each problem pair is a question
    assert 'name="capital" id="capital" value="60' in errs and 'name="api_key" autocomplete="off" value="k"' in errs
    assert 'name="pfix__SSV/USDT" value="remove"' in errs and 'name="pfix__SSV/USDT" value="keep"' not in errs
    errs = save(c, csrf, capital="60", **{"pair__BTC/USDT": "on", "pbot__BTC/USDT": str(a), "pair__ETH/USDT": "on",
                                          "pair__SSV/USDT": "on", "pfix__SSV/USDT": "remove"}).text
    assert "Removed from the portfolio: SSV/USDT" in errs and "SSV/USDT: this pair" not in errs
    assert "margin per trade is 27.00" not in errs  # 60 x 90% / 2 = 27 now: BTC is fine
    with SessionLocal() as s:  # ... so it was saved without SSV; start over for the next checks
        saved = s.query(ExchangeAccount).filter_by(label="cat-p").one()
        assert set(saved.pairs) == {"BTC/USDT", "ETH/USDT"}
        s.delete(saved)
        s.commit()
    FakeBinance.balance = 40.0
    errs = save(c, csrf, capital="100", **{"pair__BTC/USDT": "on", "pbot__BTC/USDT": str(a), "pair__ETH/USDT": "on"}).text
    assert "needs 100.00 USDT but the account holds 40.00" in errs
    FakeBinance.balance = 500.0
    FakeBinance.perms = {**FakeBinance.perms, "enableWithdrawals": True}
    errs = save(c, csrf, capital="100", **{"pair__BTC/USDT": "on", "pbot__BTC/USDT": str(a), "pair__ETH/USDT": "on"}).text
    assert "Withdrawals are ENABLED" in errs
    FakeBinance.perms["enableWithdrawals"] = False
    r = save(c, csrf, capital="100", max_open_trades="1",
             **{"pair__BTC/USDT": "on", "pbot__BTC/USDT": str(b), "pair__ETH/USDT": "on"})
    assert r.status_code == 200
    with SessionLocal() as s:
        acc = s.query(ExchangeAccount).filter_by(label="cat-p").one()
        assert acc.pair_bot == {"BTC/USDT": b, "ETH/USDT": a} and set(acc.bots) == {a, b}
        assert acc.sizing_mode == "equal_split" and acc.max_open_trades is None and acc.capital == 100  # no limit
        from app.engine.sizing import leverage_for, max_open_trades, pair_budget
        gs = get_global(s)
        assert leverage_for(gs, acc, "ETH/USDT", 5.0) == 3  # admin's fixed leverage wins over the bot's 5x
        assert leverage_for(gs, acc, "BTC/USDT", 5.0) == 5  # otherwise the bot's own leverage
        assert max_open_trades(gs, acc) == 2 and abs(pair_budget(gs, acc, "BTC/USDT") - 45) < 1e-9  # 100 x 90% / 2
        acc_id = acc.id
    # the check button (JSON), with the stored keys
    res = c.post("/accounts/check", json={"csrf": csrf, "acc_id": acc_id, "exchange": "binance", "trading_mode": "futures",
                                          "pairs": {"BTC/USDT": {"margin": 10, "leverage": 5, "bot": b}}}).json()
    assert res["keys_ok"] and not res["ok"] and res["balance"]["total"] == 500
    assert any("BTC/USDT" in i["text"] and i["status"] == "error" for i in res["items"])
    assert c.post("/accounts/check", json={"csrf": "x", "exchange": "binance"}).status_code == 400


def test_catalog_bulk_apply(env):
    a, b = env
    c, csrf = login("cat-admin2@x.com", admin=True)
    html = c.get("/admin/pairs").text
    assert 'id="f-group"' in html and 'id="sel-all"' in html and 'data-pair="BTC/USDT" data-bot="%d"' % a in html
    ap = lambda act, rows, value=None: c.post("/admin/pairs/apply", json={"csrf": csrf, "action": act, "value": value,  # noqa: E731
                                                                         "rows": rows})
    rows = [["BTC/USDT", a], ["BTC/USDT", b], ["SSV/USDT", b]]
    assert ap("offer", rows).json()["n"] == 3
    assert ap("leverage", rows[:2], "4").status_code == 200 and ap("min_capital", [["SSV/USDT", b]], "30").status_code == 200
    assert ap("default", [["BTC/USDT", a], ["BTC/USDT", b]]).status_code == 200  # last one of a pair wins
    assert ap("mandatory", [["SSV/USDT", b]]).status_code == 200
    with SessionLocal() as s:
        o = {(x.pair, x.bot_id): x for x in s.query(PairOffer)}
        assert all(o[(p, bb)].enabled for p, bb in rows) and o[("BTC/USDT", a)].leverage == 4
        assert o[("BTC/USDT", b)].is_default and not o[("BTC/USDT", a)].is_default
        assert o[("SSV/USDT", b)].min_capital == 30 and o[("SSV/USDT", b)].mandatory
    assert ap("leverage", rows[:1], "").status_code == 200  # empty = remove
    assert ap("withdraw", rows).status_code == 200
    with SessionLocal() as s:
        assert s.query(PairOffer).filter_by(enabled=True).count() == 0
        assert s.query(PairOffer).filter_by(pair="BTC/USDT", bot_id=a).one().leverage is None
    assert ap("explode", rows).status_code == 400 and ap("offer", []).status_code == 400
    assert c.post("/admin/pairs/apply", json={"csrf": "x", "action": "offer", "rows": rows}).status_code == 400


def test_form_per_pair_amounts_and_closed_pair(env):
    """The user may give each pair its own amount; a pair the exchange has closed for now can be kept or dropped."""
    a, b = env
    offer("BTC/USDT", a)
    offer("ETH/USDT", a)
    offer("SSV/USDT", b)
    c, csrf = login("cat-user2@x.com")
    pairs = {"pair__BTC/USDT": "on", "pair__ETH/USDT": "on", "value__BTC/USDT": "25", "value__ETH/USDT": "7"}
    r = save(c, csrf, label="cat-p2", sizing_mode="custom_amounts", **pairs)
    assert r.status_code == 200 and "cat-p2" in r.text
    with SessionLocal() as s:
        acc = s.query(ExchangeAccount).filter_by(label="cat-p2").one()
        from app.engine.sizing import pair_budget
        gs = get_global(s)
        assert acc.sizing_mode == "custom_amounts" and acc.capital == 32 and acc.pairs == {"BTC/USDT": 25, "ETH/USDT": 7}
        assert pair_budget(gs, acc, "BTC/USDT") == 25 and pair_budget(gs, acc, "ETH/USDT") == 7
        acc_id = acc.id
    MARKETS["SSV/USDT:USDT"]["active"] = False
    try:
        asyncio.run(markets.refresh(["binance"]))
        more = {**pairs, "pair__SSV/USDT": "on", "value__SSV/USDT": "10"}
        page = c.post(f"/accounts/{acc_id}/edit", data={"csrf": csrf, "kind": "exchange", "label": "cat-p2",
                                                        "exchange": "binance", "trading_mode": "futures",
                                                        "no_withdraw": "on", "sizing_mode": "custom_amounts", **more}).text
        assert "SSV/USDT is not tradable" in page and 'name="pfix__SSV/USDT" value="keep"' in page
        assert 'name="value__BTC/USDT" data-p="BTC/USDT" value="25' in page  # amounts kept
        c.post(f"/accounts/{acc_id}/edit", data={"csrf": csrf, "kind": "exchange", "label": "cat-p2", "exchange": "binance",
                                                 "trading_mode": "futures", "no_withdraw": "on",
                                                 "sizing_mode": "custom_amounts", "pfix__SSV/USDT": "keep", **more})
        with SessionLocal() as s:
            assert s.get(ExchangeAccount, acc_id).pairs == {"BTC/USDT": 25, "ETH/USDT": 7, "SSV/USDT": 10}
    finally:
        MARKETS["SSV/USDT:USDT"]["active"] = True
