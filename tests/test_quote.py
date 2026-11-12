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
    out = sig("short", "BTC/USDT", 21, leverage=5)
    assert [o.status for o in out] == ["opened"] and "max 3x" in out[0].message, out
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
