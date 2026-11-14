"""Spot portfolios in their own quote coin: the bots' BTC/USDT signal is traded as BTC/EUR on the user's exchange,
with capital, balance and results in EUR; a coin without a EUR market there is skipped with the reason."""
import asyncio

import pytest

from tests import fake_exchange as fx
from tests.test_catalog import FakeBinance, FakeSpot, env as catalog_env, login, offer, save  # noqa: F401

import app.engine.exchange as exmod
from app.db import Bot, ExchangeAccount, ExecutionLog, SessionLocal, Trade, User, get_global, init_db
from app.engine.dispatcher import dispatch
from app.engine.exchange import pool
from app.security import encrypt
from app.signal import Signal


@pytest.fixture
def eur(monkeypatch):
    init_db()
    monkeypatch.setattr(exmod.ccxt, "binance", fx.FakeExchange)
    with SessionLocal() as s:
        gs = get_global(s)
        saved = (list(gs.allowed_pairs or []), list(gs.allowed_exchanges or []), gs.entries_enabled)
        gs.allowed_pairs, gs.allowed_exchanges, gs.entries_enabled = ["BTC/USDT", "ETH/USDT"], ["binance"], True
        bot = Bot(name="EurBot", webhook_secret="eur-bot")
        u = User(email="eur@x.com", name="E", password_hash="x", status="active", trading_enabled=True, lang="en")
        s.add_all([bot, u])
        s.commit()
        acc = ExchangeAccount(user_id=u.id, kind="exchange", label="eur-spot", exchange="binance", trading_mode="spot",
                              quote="EUR", dry_run=False, capital=1000, pairs={"BTC/USDT": 1.0, "ETH/USDT": 1.0},
                              bots=[bot.id], pair_bot={"BTC/USDT": bot.id, "ETH/USDT": bot.id},
                              api_key_enc=encrypt("k"), secret_enc=encrypt("s"))
        s.add(acc)
        s.commit()
        ids = (acc.id, bot.id, u.id)
    yield ids
    asyncio.run(pool.drop(ids[0]))
    with SessionLocal() as s:
        s.query(Trade).filter_by(account_id=ids[0]).delete()
        s.query(ExecutionLog).filter_by(account_id=ids[0]).delete()
        s.query(ExchangeAccount).filter_by(id=ids[0]).delete()
        s.query(Bot).filter_by(id=ids[1]).delete()
        s.query(User).filter_by(id=ids[2]).delete()
        gs = get_global(s)
        gs.allowed_pairs, gs.allowed_exchanges, gs.entries_enabled = saved
        s.commit()


def test_usdt_signal_traded_in_eur(eur):
    acc_id, bot_id, _ = eur

    def sig(action, pair, n, **kw):
        return asyncio.run(dispatch(Signal(action=action, symbol=pair.replace("/", ""), pair=pair, ft_trade_id=n,
                                           price=60000.0 if pair.startswith("BTC") else 3000.0, id=f"eur-{action}-{n}", **kw),
                                    None, bot_id))
    out = sig("long", "BTC/USDT", 1)
    assert [o.status for o in out] == ["opened"], out
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT").one()
        assert tr.symbol == "BTC/EUR" and tr.currency == "EUR"
        assert tr.entry_price == 55000  # the EUR market's own price, not the bot's USDT price
        assert tr.stake == pytest.approx(500, rel=0.02) and tr.leverage == 1  # 1,000 EUR / 2 pairs (lot size), spot
    order = fx.FakeExchange.instances[-1].orders[-1]
    assert order["symbol"] == "BTC/EUR" and order["side"] == "buy"
    # ETH has no EUR market on this exchange: skipped, with the reason
    out = sig("long", "ETH/USDT", 2)
    assert [o.status for o in out] == ["skipped"] and "EUR" in out[0].message and "ETH" in out[0].message
    # shorts don't exist on spot
    assert [o.status for o in sig("short", "BTC/USDT", 3)] in ([], ["none"])
    # the bot exits: sold on BTC/EUR, result in EUR
    out = sig("close_long", "BTC/USDT", 1)
    assert [o.status for o in out] == ["closed"], out
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT").one()
        assert tr.status == "closed" and tr.currency == "EUR"
    assert fx.FakeExchange.instances[-1].orders[-1]["symbol"] == "BTC/EUR"


def test_form_quote_currency(catalog_env):
    """The form offers the exchange's quote coins; pairs not traded in the chosen one are named; money is in it."""
    import asyncio as _a

    from app.engine import markets
    a, b = catalog_env
    offer("BTC/USDT", a)
    offer("ETH/USDT", a, min_capital=50)
    _a.run(markets.refresh(["binance"]))
    assert markets.quotes_on("binance") == ["USDT", "USDC", "EUR"]
    assert markets.usd_rate("EUR") == pytest.approx(1.087)
    assert markets.convert(100, "EUR", "USDT") == pytest.approx(108.7)
    c, csrf = login("cat-quote-u@x.com")
    form = c.get("/accounts/new?kind=exchange").text
    assert 'id="quote"' in form and '"binance": ["USDT", "USDC", "EUR"]' in form and "BTC/USDT" in form
    pairs = {"pair__BTC/USDT": "on", "pair__ETH/USDT": "on"}
    # EUR: ETH has no EUR market -> named, can be removed
    page = save(c, csrf, label="cat-q-eur", trading_mode="spot", quote="EUR", capital="400", **pairs).text
    assert "ETH isn&#39;t traded against EUR on Binance" in page and 'name="pfix__ETH/USDT"' in page
    # a quote this exchange doesn't have
    page = save(c, csrf, label="cat-q-gbp", trading_mode="spot", quote="GBP", capital="400", **pairs).text
    assert "Binance has no spot markets in GBP" in page
    # USDC works for both; saved in USDC
    r = save(c, csrf, label="cat-q-usdc", trading_mode="spot", quote="USDC", capital="400", **pairs)
    assert r.status_code == 200
    with SessionLocal() as s:
        acc = s.query(ExchangeAccount).filter_by(label="cat-q-usdc").one()
        assert acc.quote == "USDC" and acc.currency("USDT") == "USDC" and acc.trading_mode == "spot"
    # the admin's minimum (50 USDT) is compared in EUR: 50 USDT = 46 EUR, so 46 EUR per pair is enough
    r = save(c, csrf, label="cat-q-eur2", api_key="k2", trading_mode="spot", quote="EUR", capital="46.1",
             **{"pair__BTC/USDT": "on"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.query(ExchangeAccount).filter_by(label="cat-q-eur2").one().quote == "EUR"


def test_market_test_compares_with_the_bot(eur):
    """Our BTC/EUR spot trade next to the bot's own BTC/USDT futures trade, both without leverage."""
    import datetime as dt

    from app.db import BotTrade, utcnow
    from app.engine import marketcompare
    acc_id, bot_id, uid = eur
    with SessionLocal() as s:
        s.add(Trade(user_id=uid, account_id=acc_id, bot_id=bot_id, ft_trade_id=77, pair="BTC/USDT", symbol="BTC/EUR",
                    side="long", status="closed", stake=500, leverage=1, realized_pnl=10.0, entry_price=55000,
                    closed_at=utcnow() - dt.timedelta(days=1)))
        s.add(BotTrade(bot_id=bot_id, ft_trade_id=77, pair="BTC/USDT", side="long", leverage=5, is_open=False,
                       profit_ratio=0.05, profit_abs=5, open_date=utcnow() - dt.timedelta(days=2),
                       close_date=utcnow() - dt.timedelta(days=1)))
        s.commit()
    try:
        r = marketcompare.compare()
        m = next(x for x in r["markets"] if x["market"] == "Binance · spot · EUR")
        assert m["trades"] == 1 and m["ours"] == pytest.approx(2.0) and m["bot"] == pytest.approx(1.0)  # 10/500; 5%/5x
        assert m["diff"] == pytest.approx(1.0) and m["better"] == 1 and m["cur"] == "EUR"
        assert [p["pair"] for p in r["pairs"] if p["market"] == "Binance · spot · EUR"] == ["BTC/USDT"]
        from tests.test_watchdog import _admin_client
        admin, aid = _admin_client()
        try:
            html = admin.get("/admin/performance").text
            assert 'id="markets"' in html and "Binance · spot · EUR" in html and "+1.00" in html
        finally:
            with SessionLocal() as s:
                s.query(User).filter_by(id=aid).delete()
                s.commit()
    finally:
        with SessionLocal() as s:
            s.query(BotTrade).filter_by(bot_id=bot_id).delete()
            s.commit()


def test_futures_in_usdc(eur, monkeypatch):
    """Futures in another settle coin: the bots' BTC/USDT and 1000PEPE/USDT are traded as the USDC perpetuals
    (BTC/USDC:USDC, KPEPE/USDC:USDC like on Hyperliquid); the exchange's leverage cap applies; a coin without a
    USDC perpetual is skipped with the reason; money and results are in USDC."""
    acc_id, bot_id, _ = eur
    monkeypatch.setattr(exmod.ccxt, "binanceusdm", fx.FakeExchange)
    with SessionLocal() as s:
        gs = get_global(s)
        gs.allowed_pairs = ["BTC/USDT", "ETH/USDT", "1000PEPE/USDT"]
        acc = s.get(ExchangeAccount, acc_id)
        acc.trading_mode, acc.quote, acc.capital = "futures", "USDC", 900
        acc.pairs = {"BTC/USDT": 1.0, "ETH/USDT": 1.0, "1000PEPE/USDT": 1.0}
        acc.pair_bot = {p: bot_id for p in acc.pairs}
        s.commit()
    fx.FakeExchange.positions = []

    def sig(action, pair, n, **kw):
        return asyncio.run(dispatch(Signal(action=action, symbol=pair.replace("/", ""), pair=pair, ft_trade_id=n,
                                           id=f"usdc-{action}-{n}", **kw), None, bot_id))
    # the leverage is fixed: 5x where the exchange allows 3x is refused, never opened at another leverage
    out = sig("short", "BTC/USDT", 20, leverage=5)
    assert [o.status for o in out] == ["skipped"] and "5x" in out[0].message and "3x" in out[0].message, out
    out = sig("short", "BTC/USDT", 21, leverage=3)
    assert [o.status for o in out] == ["opened"], out
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT").one()
        assert tr.symbol == "BTC/USDC:USDC" and tr.currency == "USDC" and tr.leverage == 3
        assert tr.entry_price == 60010  # the USDC market's own price
    ex = fx.FakeExchange.instances[-1]
    assert ex.orders[-1]["symbol"] == "BTC/USDC:USDC" and ex.leverage["BTC/USDC:USDC"] == 3
    out = sig("long", "1000PEPE/USDT", 22, leverage=2)
    assert [o.status for o in out] == ["opened"], out
    with SessionLocal() as s:
        assert s.query(Trade).filter_by(account_id=acc_id, pair="1000PEPE/USDT").one().symbol == "KPEPE/USDC:USDC"
    out = sig("long", "ETH/USDT", 23)
    assert [o.status for o in out] == ["skipped"] and "USDC" in out[0].message and "ETH" in out[0].message
    # positions on the exchange are shown under the bots' pairs (KPEPE/USDC:USDC -> 1000PEPE/USDT)
    from app.engine.positions import normalize
    rows = normalize([{"symbol": "KPEPE/USDC:USDC", "side": "long", "contracts": 100, "contractSize": 1,
                       "entryPrice": 0.01}], quote="USDC")
    assert rows[0]["pair"] == "1000PEPE/USDT"
    out = sig("close_short", "BTC/USDT", 21)
    assert [o.status for o in out] == ["closed"], out
    assert ex.orders[-1]["symbol"] == "BTC/USDC:USDC" and ex.orders[-1]["params"].get("reduceOnly")


def test_fixed_leverage_above_the_exchange_max_is_refused_in_the_form_check():
    from app.engine.account_check import check_pairs
    init_db()
    with SessionLocal() as s:
        gs = get_global(s)
        saved = gs.market_cache
        gs.market_cache = {"hyperliquid": {"pairs": {}, "spot": {}, "fut": {
            "1000PEPE/USDC": {"symbol": "KPEPE/USDC:USDC", "active": True, "min_cost": 10.0, "max_leverage": 10.0,
                              "price": 0.01, "contract_size": 1.0}}}}
        s.commit()
    try:
        sel = {"1000PEPE/USDT": {"margin": 50, "leverage": 20}}
        items = check_pairs("hyperliquid", "futures", sel, quote="USDC")
        assert [(i["status"], i["key"]) for i in items] == [("error", "ck_lev_capped")]
        assert items[0]["params"] == {"pair": "1000PEPE/USDT", "lev": 20, "max": 10, "ex": "Hyperliquid"}
        sel["1000PEPE/USDT"]["leverage"] = 10
        assert check_pairs("hyperliquid", "futures", sel, quote="USDC") == []
    finally:
        with SessionLocal() as s:
            get_global(s).market_cache = saved
            s.commit()


def test_credentials_per_exchange(catalog_env):
    """Hyperliquid asks for a wallet address and an API wallet's private key (not an API key/secret); wrong formats
    are named before asking the exchange; a Coinbase PEM key pasted on one line is rebuilt."""
    from app.engine.exchange import _pem, credential_problem, credentials
    with SessionLocal() as s:
        gs = get_global(s)
        gs.allowed_exchanges = ["binance", "hyperliquid", "coinbase"]
        s.commit()
    c, csrf = login("cat-cred-u@x.com")
    form = c.get("/accounts/new?kind=exchange").text
    assert "Main wallet address (0x" in form and "API wallet private key" in form and "API key name (organizations/" in form
    page = save(c, csrf, label="cat-hl", exchange="hyperliquid", api_key="my-api-key", secret="s").text
    assert "0x followed by 40 hex" in page
    page = save(c, csrf, label="cat-hl", exchange="hyperliquid", api_key="0x" + "a" * 40, secret="short").text
    assert "64 hex characters" in page
    r = c.post("/accounts/check", json={"csrf": csrf, "exchange": "hyperliquid", "trading_mode": "futures",
                                         "api_key": "nope", "secret": "x", "pairs": {}})
    assert "0x followed by 40 hex" in r.text
    assert credential_problem("hyperliquid", "0x" + "a" * 40, "b" * 64) is None
    assert credentials("hyperliquid", "0x" + "a" * 40, "b" * 64, None) == {"walletAddress": "0x" + "a" * 40,
                                                                          "privateKey": "0x" + "b" * 64}
    one_line = "-----BEGIN EC PRIVATE KEY-----" + "Q" * 100 + "-----END EC PRIVATE KEY-----"
    pem = credentials("coinbase", "organizations/o/apiKeys/k", one_line, None)["secret"]
    assert pem == _pem(one_line) and pem.splitlines() == ["-----BEGIN EC PRIVATE KEY-----", "Q" * 64, "Q" * 36,
                                                          "-----END EC PRIVATE KEY-----"]
    assert credential_problem("coinbase", "-----BEGIN PUBLIC", one_line) == "err_cb_keyname"


def test_a_portfolio_form_sent_twice_saves_once(catalog_env, monkeypatch):
    """Saving checks the key with the exchange (a few seconds): a second submit of the same form (double click,
    Enter + click) arrives meanwhile. Only one portfolio is created; the second answer says it was saved."""
    import re as _re

    import httpx

    from app.main import app as _app
    from tests.test_catalog import FakeBinance
    a, _b = catalog_env
    offer("BTC/USDT", a)
    c, csrf = login("cat-twice-u@x.com")
    form = c.get("/accounts/new?kind=exchange").text
    token = _re.search(r'name="form_token" value="([^"]+)"', form).group(1)
    assert 'id="savebtn"' in form
    slow = FakeBinance.fetch_balance

    async def fetch_balance(self):  # the exchange takes its time
        await asyncio.sleep(0.3)
        return await slow(self)
    monkeypatch.setattr(FakeBinance, "fetch_balance", fetch_balance)
    data = {"csrf": csrf, "kind": "exchange", "label": "cat-twice", "exchange": "binance", "trading_mode": "futures",
            "api_key": "twice-k", "secret": "s", "no_withdraw": "on", "form_token": token, "pair__BTC/USDT": "on",
            "capital": "300"}

    async def both():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://testserver",
                                     cookies=dict(c.cookies)) as ac:
            return await asyncio.gather(ac.post("/accounts/new", data=data), ac.post("/accounts/new", data=data))
    results = asyncio.run(both())
    with SessionLocal() as s:
        assert s.query(ExchangeAccount).filter_by(label="cat-twice").count() == 1
    assert sorted(r.status_code for r in results) == [303, 303]
    # a fresh form (new token) is a new save as usual; errors don't use the token up
    form2 = c.get("/accounts/new?kind=exchange").text
    t2 = _re.search(r'name="form_token" value="([^"]+)"', form2).group(1)
    assert t2 != token
    page = save(c, csrf, label="cat-twice2", api_key="", secret="", form_token=t2).text  # missing keys: an error
    assert "already saved" not in page


class FakeHL(fx.FakeExchange):
    """Like Hyperliquid: the order answer has the real average price but no fee and no status; the order status
    later carries only the slippage cap as "price" (5% away); the fills have the exact price and the fee."""
    id = "hyperliquid"
    fills_log: list = []

    async def create_order(self, symbol, type_, side, amount, price, params):
        await super().create_order(symbol, type_, side, amount, price, params)
        o = self.orders[-1]
        FakeHL.fills_log.append({"order": o["id"], "symbol": symbol, "side": side, "amount": amount, "price": o["price"],
                                 "fee": {"cost": 0.01, "currency": "USDC"}, "timestamp": 1})
        return {"id": o["id"], "status": None, "filled": amount, "average": o["price"]}

    async def fetch_order(self, oid, symbol):
        o = next(o for o in self.orders if o["id"] == oid)
        cap = o["price"] * (1.05 if o["side"] == "buy" else 0.95)
        return {"id": oid, "status": "closed", "filled": o["amount"], "average": None, "price": cap}

    async def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        return [dict(f) for f in FakeHL.fills_log if f["symbol"] == symbol]


def test_hyperliquid_fill_price_and_fees(eur, monkeypatch):
    """Seen live: exits were recorded at Hyperliquid's 5% slippage cap (a +0.66 profit shown as -0.53) and fees
    as zero. The real fill price and fee are used; the result is net of entry and exit fees."""
    acc_id, bot_id, _ = eur
    monkeypatch.setattr(exmod.ccxt, "hyperliquid", FakeHL)
    FakeHL.fills_log = []
    with SessionLocal() as s:
        gs = get_global(s)
        gs.allowed_exchanges = ["binance", "hyperliquid"]
        acc = s.get(ExchangeAccount, acc_id)
        acc.exchange, acc.trading_mode, acc.quote, acc.capital = "hyperliquid", "futures", "USDC", 900
        acc.pairs, acc.pair_bot = {"BTC/USDT": 1.0}, {"BTC/USDT": bot_id}
        s.commit()
    fx.FakeExchange.positions = []

    def sig(action, n, **kw):
        return asyncio.run(dispatch(Signal(action=action, symbol="BTCUSDT", pair="BTC/USDT", ft_trade_id=n,
                                           id=f"hl-{action}-{n}", **kw), None, bot_id))
    assert [o.status for o in sig("short", 31, leverage=3)] == ["opened"]
    fx.PRICES["BTC/USDC:USDC"] = 59000.0  # the price falls: the short is in profit
    try:
        assert [o.status for o in sig("close_short", 31)] == ["closed"]
    finally:
        fx.PRICES["BTC/USDC:USDC"] = 60010.0
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT").one()
        assert tr.entry_price == pytest.approx(60010) and tr.exit_price == pytest.approx(59000)  # not the cap (61950)
        assert tr.fees == pytest.approx(0.02)
        assert tr.realized_pnl == pytest.approx((60010 - 59000) * tr.closed_amount - 0.02)
        assert tr.realized_pnl > 0
        # the repair reads the same fills: nothing left to change
        tid = tr.id
    from app.engine.repair import from_fills
    rows = asyncio.run(from_fills(acc_id))
    r = next(x for x in rows if x["id"] == tid)
    assert r["missing"] == 0 and r["new"]["pnl"] == pytest.approx(r["old"]["pnl"], abs=1e-6)
    # an old record (exit at the cap, no fees) is corrected by it
    with SessionLocal() as s:
        tr = s.get(Trade, tid)
        tr.exit_value, tr.realized_pnl, tr.fees = 61950 * tr.closed_amount, -1.0, 0.0
        s.commit()
    rows = asyncio.run(from_fills(acc_id, apply=True))
    with SessionLocal() as s:
        tr = s.get(Trade, tid)
        assert tr.exit_price == pytest.approx(59000) and tr.realized_pnl > 0 and tr.fees == pytest.approx(0.02)


def test_execution_quality_against_the_bot(eur):
    """A short the bot entered at 100 and left at 90; we sold at 99.9 (worse: 10 bp) 4 s after it and bought back at
    90.18 (worse: 20 bp) 6 s after it. Results without leverage, net of fees, and their difference."""
    import datetime as dt

    from app.db import BotTrade, Order, WebhookEvent, utcnow
    from app.engine import execquality
    acc_id, bot_id, uid = eur
    t0 = utcnow() - dt.timedelta(hours=3)
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        acc.trading_mode, acc.quote = "futures", ""
        s.add(BotTrade(bot_id=bot_id, ft_trade_id=501, pair="SOL/USDT", side="short", leverage=5, is_open=False,
                       open_date=t0, close_date=t0 + dt.timedelta(hours=2), open_rate=100.0, close_rate=90.0,
                       profit_ratio=0.49))  # Freqtrade: 9.8% x 5 after its fees
        tr = Trade(user_id=uid, account_id=acc_id, bot_id=bot_id, ft_trade_id=501, pair="SOL/USDT",
                   symbol="SOL/USDT:USDT", side="short", status="closed", stake=20.0, leverage=5, amount=0,
                   closed_amount=1.0, entry_price=99.9, exit_value=90.18, realized_pnl=9.62, fees=0.1,
                   opened_at=t0 + dt.timedelta(seconds=4), closed_at=t0 + dt.timedelta(hours=2, seconds=6))
        s.add(tr)
        s.flush()
        w1 = WebhookEvent(bot_id=bot_id, received_at=t0 + dt.timedelta(seconds=1), msg_type="entry", status="accepted")
        w2 = WebhookEvent(bot_id=bot_id, received_at=t0 + dt.timedelta(hours=2, seconds=2), msg_type="exit", status="accepted")
        s.add_all([w1, w2])
        s.flush()
        s.add_all([Order(trade_id=tr.id, exchange_order_id="a", kind="entry", side="sell", amount=1, filled=1, price=99.9,
                         created_at=t0 + dt.timedelta(seconds=4)),
                   Order(trade_id=tr.id, exchange_order_id="b", kind="exit", side="buy", amount=1, filled=1, price=90.18,
                         created_at=t0 + dt.timedelta(hours=2, seconds=6)),
                   ExecutionLog(webhook_event_id=w1.id, account_id=acc_id, trade_id=tr.id, action="short",
                                pair="SOL/USDT", status="opened", duration_ms=2500),
                   ExecutionLog(webhook_event_id=w2.id, account_id=acc_id, trade_id=tr.id, action="close_short",
                                pair="SOL/USDT", status="closed", duration_ms=3500)])
        s.commit()
        tid, ids = tr.id, (w1.id, w2.id)
    try:
        rep = execquality.report([acc_id], 30)
        r = next(x for x in rep["rows"] if x["id"] == tid)
        assert r["slip_entry"] == pytest.approx(10.0) and r["slip_exit"] == pytest.approx(20.0)
        assert r["delay_entry"] == pytest.approx(4) and r["delay_exit"] == pytest.approx(6)
        assert r["signal_entry"] == pytest.approx(1) and r["exec_entry"] == pytest.approx(2.5)
        assert r["ours_pct"] == pytest.approx(9.62) and r["bot_pct"] == pytest.approx(9.8)
        assert r["diff_pct"] == pytest.approx(-0.18) and rep["total"]["worse"] == 1
        from app.security import hash_password
        with SessionLocal() as s:
            s.get(User, uid).password_hash = hash_password("Xy-12345678-zz")
            s.get(User, uid).is_admin = True  # execution quality is an admin page
            s.commit()
        c, _ = login("eur@x.com")
        page = c.get("/execution?days=30").text
        assert "SOL/USDT" in page and "+10.0" in page and "+20.0" in page and "-0.18%" in page
        assert "Execution quality" in page and "/execution" in c.get("/dashboard").text
    finally:
        with SessionLocal() as s:
            s.query(Order).filter_by(trade_id=tid).delete()
            s.query(WebhookEvent).filter(WebhookEvent.id.in_(ids)).delete()
            s.query(BotTrade).filter_by(bot_id=bot_id).delete()
            s.commit()


class FakeHLNoFill(FakeHL):
    """Hyperliquid answers an IOC order that found no counterparty with an error: nothing was traded."""
    async def create_order(self, symbol, type_, side, amount, price, params):
        if params.get("reduceOnly"):
            return {"id": None, "status": "rejected", "filled": None, "average": None,
                    "info": {"error": "Order could not immediately match against any resting orders."}}
        return await FakeHL.create_order(self, symbol, type_, side, amount, price, params)


def test_hyperliquid_unfilled_exit_keeps_the_trade_open(eur, monkeypatch):
    """Seen live: the bot exited NIGHT, the site marked it closed, but Hyperliquid had rejected the IOC order and
    the position stayed open. An order with no fill is an error now; the record stays open."""
    acc_id, bot_id, _ = eur
    monkeypatch.setattr(exmod.ccxt, "hyperliquid", FakeHLNoFill)
    FakeHL.fills_log = []
    with SessionLocal() as s:
        gs = get_global(s)
        gs.allowed_exchanges = ["binance", "hyperliquid"]
        acc = s.get(ExchangeAccount, acc_id)
        acc.exchange, acc.trading_mode, acc.quote, acc.capital = "hyperliquid", "futures", "USDC", 900
        acc.pairs, acc.pair_bot = {"BTC/USDT": 1.0}, {"BTC/USDT": bot_id}
        s.commit()
    fx.FakeExchange.positions = []

    def sig(action, n, **kw):
        return asyncio.run(dispatch(Signal(action=action, symbol="BTCUSDT", pair="BTC/USDT", ft_trade_id=n,
                                           id=f"hlnf-{action}-{n}", **kw), None, bot_id))
    assert [o.status for o in sig("short", 41, leverage=3)] == ["opened"]
    out = sig("close_short", 41)
    assert [o.status for o in out] == ["error"] and "not filled" in out[0].message, out
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT").one()
        assert tr.status == "open" and tr.closed_amount == 0


class FakeHLSlow(FakeHL):
    """Hyperliquid's info endpoint timing out for a while (seen live on a NEAR exit)."""
    timeouts = 0

    async def create_order(self, symbol, type_, side, amount, price, params):
        if params.get("reduceOnly") and FakeHLSlow.timeouts > 0:
            FakeHLSlow.timeouts -= 1
            raise exmod.ccxt.RequestTimeout("hyperliquid POST https://api.hyperliquid.xyz/info")
        return await FakeHL.create_order(self, symbol, type_, side, amount, price, params)


def test_exit_retried_after_network_errors(eur, monkeypatch):
    import app.engine.trader as trader
    acc_id, bot_id, _ = eur
    monkeypatch.setattr(exmod.ccxt, "hyperliquid", FakeHLSlow)
    monkeypatch.setattr(trader, "EXIT_RETRY_WAITS", (0, 0))
    monkeypatch.setattr(trader, "EXIT_RETRY_EVERY", 0)
    FakeHL.fills_log, FakeHLSlow.timeouts = [], 0
    with SessionLocal() as s:
        gs = get_global(s)
        gs.allowed_exchanges = ["binance", "hyperliquid"]
        acc = s.get(ExchangeAccount, acc_id)
        acc.exchange, acc.trading_mode, acc.quote, acc.capital = "hyperliquid", "futures", "USDC", 900
        acc.pairs, acc.pair_bot = {"BTC/USDT": 1.0}, {"BTC/USDT": bot_id}
        s.commit()
    fx.FakeExchange.positions = []

    def sig(action, n, **kw):
        return asyncio.run(dispatch(Signal(action=action, symbol="BTCUSDT", pair="BTC/USDT", ft_trade_id=n,
                                           id=f"slow-{action}-{n}-{FakeHLSlow.timeouts}", **kw), None, bot_id))
    assert [o.status for o in sig("short", 51, leverage=3)] == ["opened"]
    FakeHLSlow.timeouts = 2  # two timeouts, then it answers: closed by the quick retries
    assert [o.status for o in sig("close_short", 51)] == ["closed"]
    assert [o.status for o in sig("short", 52, leverage=3)] == ["opened"]
    FakeHLSlow.timeouts = 5  # still failing after the quick retries: kept and placed again in the background
    assert [o.status for o in sig("close_short", 52)] == ["error"]
    assert (acc_id, "BTC/USDT") in trader._pending_exits
    asyncio.run(trader.retry_pending_exits())
    with SessionLocal() as s:
        assert s.query(Trade).filter_by(account_id=acc_id, ft_trade_id=52).one().status == "closed"
    assert (acc_id, "BTC/USDT") not in trader._pending_exits
