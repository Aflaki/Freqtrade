"""Sizing by a share of the wallet (compounding, with the user's floor), the admin's cap on the wallet share
(site default + per user), and fixed amounts that must stay under that cap."""
import asyncio

import pytest

from tests import fake_exchange as fx
from tests.test_catalog import FakeBinance, env as catalog_env, login, offer, save  # noqa: F401  (fixture)

import app.engine.exchange as exmod
from app.db import Bot, ExchangeAccount, ExecutionLog, SessionLocal, Trade, User, get_global, init_db
from app.engine.dispatcher import dispatch
from app.engine.exchange import pool, wallet_of
from app.engine.sizing import pair_budget, wallet_cap
from app.security import encrypt
from app.signal import Signal


def test_wallet_balance_without_open_pnl():
    raw = {"total": {"USDT": 1060.0}, "info": {"assets": [{"asset": "USDT", "walletBalance": "1000.5",
                                                           "marginBalance": "1060.0"}]}}
    assert wallet_of(raw, "USDT") == 1000.5  # Binance futures: total includes open P/L, the wallet doesn't
    assert wallet_of({"total": {"USDT": 42.0}}, "USDT") == 42.0


def test_budget_and_caps():
    init_db()
    with SessionLocal() as s:
        gs = get_global(s)
        acc = ExchangeAccount(kind="exchange", sizing_mode="wallet_pct", wallet_pct=75, min_stake=6,
                              pairs={"BTC/USDT": 1, "ETH/USDT": 1, "SOL/USDT": 1}, wallet_balance=2000)
        assert pair_budget(gs, acc, "BTC/USDT") == 500  # 2000 x 75% / 3
        assert pair_budget(gs, acc, "BTC/USDT", 2400) == 600  # profits compound into the next trades
        assert pair_budget(gs, acc, "BTC/USDT", 20) == 6  # after losses: never below the user's floor
        assert wallet_cap(gs, User(max_wallet_pct=70)) == 0.7 and wallet_cap(gs, User()) == gs.tradable_balance_ratio
        fixed = ExchangeAccount(kind="exchange", sizing_mode="equal_split", capital=100, pairs={"A/USDT": 1, "B/USDT": 1})
        assert pair_budget(gs, fixed, "A/USDT") == 50  # the whole amount is split, no hidden 90%


def test_form_wallet_mode_and_caps(catalog_env):
    a, b = catalog_env
    offer("BTC/USDT", a)
    offer("ETH/USDT", a)
    c, csrf = login("cat-wallet@x.com")
    with SessionLocal() as s:
        s.query(User).filter_by(email="cat-wallet@x.com").update({"max_wallet_pct": 70})
        s.commit()
    form = c.get("/accounts/new?kind=exchange").text
    assert 'value="wallet_pct"' in form and 'name="wallet_pct"' in form and 'max="70.0"' in form
    pairs = {"pair__BTC/USDT": "on", "pair__ETH/USDT": "on"}
    # fixed total above 70% of the 500 wallet
    page = save(c, csrf, label="cat-w", capital="400", **pairs).text
    assert "at most 70% of the wallet (350.00 of 500.00 USDT)" in page
    # share of the wallet above the user's cap
    page = save(c, csrf, label="cat-w", sizing_mode="wallet_pct", wallet_pct="80", **pairs).text
    assert "between 1 and 70%" in page and 'name="wallet_pct" id="wallet_pct" value="80"' in page
    r = save(c, csrf, label="cat-w", sizing_mode="wallet_pct", wallet_pct="60", min_stake="25", **pairs)
    assert r.status_code == 200
    with SessionLocal() as s:
        acc = s.query(ExchangeAccount).filter_by(label="cat-w").one()
        assert acc.sizing_mode == "wallet_pct" and acc.wallet_pct == 60 and acc.min_stake == 25
        assert acc.wallet_balance == 500 and acc.capital == 300 and pair_budget(get_global(s), acc, "BTC/USDT") == 150
    # a small wallet: 50 x 60% / 2 = 15 per trade, below BTC's minimum (100 at the bot's 5x = 20); a low floor
    # doesn't help, so it is a pair problem to decide
    FakeBinance.balance = 50.0
    page = save(c, csrf, label="cat-w2", api_key="k2", sizing_mode="wallet_pct", wallet_pct="60", min_stake="5",
                **pairs).text
    assert "BTC/USDT: margin per trade is 15.00" in page and 'name="pfix__BTC/USDT"' in page


@pytest.fixture
def live(monkeypatch):
    init_db()
    monkeypatch.setattr(exmod.ccxt, "binanceusdm", fx.FakeExchange)
    fx.FakeExchange.positions = []
    with SessionLocal() as s:
        gs = get_global(s)
        saved = (list(gs.allowed_pairs or []), list(gs.allowed_exchanges or []), gs.entries_enabled)
        gs.allowed_pairs, gs.allowed_exchanges, gs.entries_enabled = ["BTC/USDT", "ETH/USDT"], ["binance"], True
        bot = Bot(name="WalletBot", webhook_secret="wallet-bot")
        u = User(email="wallet-live@x.com", name="W", password_hash="x", status="active", trading_enabled=True)
        s.add_all([bot, u])
        s.commit()
        acc = ExchangeAccount(user_id=u.id, kind="exchange", label="w-live", exchange="binance", trading_mode="futures",
                              dry_run=False, sizing_mode="wallet_pct", wallet_pct=50, min_stake=1000,
                              pairs={"BTC/USDT": 1.0, "ETH/USDT": 1.0}, bots=[bot.id],
                              pair_bot={"BTC/USDT": bot.id, "ETH/USDT": bot.id}, api_key_enc=encrypt("k"),
                              secret_enc=encrypt("s"))
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


def test_compounding_at_entry(live):
    acc_id, bot_id, _ = live

    def enter(pair, n):
        sym = pair.replace("/", "")
        return asyncio.run(dispatch(Signal(action="long", symbol=sym, pair=f"{pair}:USDT", ft_trade_id=n, leverage=1,
                                           id=f"w-{n}"), None, bot_id))

    out = enter("ETH/USDT", 1)  # fake wallet: 10,000 -> 50% / 2 pairs = 2,500
    assert [o.status for o in out] == ["opened"]
    client = asyncio.run(pool.get(SessionLocal().get(ExchangeAccount, acc_id)))
    client.ex.balance["USDT"] = 12_000.0  # profits arrived (later: the 20 s balance read has expired)
    client._bal = None
    enter("BTC/USDT", 2)
    with SessionLocal() as s:
        eth, btc = (s.query(Trade).filter_by(account_id=acc_id, pair=p).one() for p in ("ETH/USDT", "BTC/USDT"))
        assert eth.stake == pytest.approx(2499, abs=3) and btc.stake == pytest.approx(3000, abs=60)  # 12,000 x 50% / 2
        assert s.get(ExchangeAccount, acc_id).wallet_balance == 12_000
        eth.status = btc.status = "closed"
        s.commit()
    client.ex.balance["USDT"] = 1_500.0  # after losses: 1,500 x 50% / 2 = 375 < the floor 1,000
    client._bal = None
    enter("ETH/USDT", 3)
    with SessionLocal() as s:
        t3 = s.query(Trade).filter_by(account_id=acc_id, ft_trade_id=3).one()
        assert t3.stake == pytest.approx(1000, abs=5)


def test_one_key_one_portfolio_and_delete_with_open(catalog_env):
    a, b = catalog_env
    offer("BTC/USDT", a, leverage=5)
    c, csrf = login("cat-dup@x.com")
    pairs = {"pair__BTC/USDT": "on"}
    assert save(c, csrf, label="cat-d1", api_key="DUPKEY", capital="100", **pairs).status_code == 200
    page = save(c, csrf, label="cat-d2", api_key="DUPKEY", capital="100", **pairs).text
    assert "already used by the portfolio “cat-d1”" in page
    res = c.post("/accounts/check", json={"csrf": csrf, "exchange": "binance", "trading_mode": "futures",
                                          "api_key": "DUPKEY", "secret": "s"}).json()
    assert not res["ok"] and "cat-d1" in res["items"][0]["text"]
    with SessionLocal() as s:  # a duplicate saved before this check existed, holding an open position
        d1 = s.query(ExchangeAccount).filter_by(label="cat-d1").one()
        assert d1.api_key_hash
        d2 = ExchangeAccount(user_id=d1.user_id, kind="exchange", label="cat-d2", exchange="binance",
                             api_key_enc=d1.api_key_enc, api_key_hash=d1.api_key_hash, pairs={"BTC/USDT": 1})
        s.add(d2)
        s.commit()
        s.add(Trade(user_id=d1.user_id, account_id=d2.id, pair="BTC/USDT", symbol="BTC/USDT:USDT", side="long",
                    amount=0.01, entry_price=100000, adopted=True))
        s.commit()
        d1_id, d2_id = d1.id, d2.id
    page = c.get("/accounts").text
    assert "same API key as “cat-d1”" in page and "Futures 5x" in page  # the pairs' real leverage, not 1x
    c.post(f"/accounts/{d2_id}/delete", data={"csrf": csrf})
    with SessionLocal() as s:
        assert s.get(ExchangeAccount, d2_id) is not None  # open position: asks what to do first
    c.post(f"/accounts/{d2_id}/delete", data={"csrf": csrf, "open": "leave"})
    with SessionLocal() as s:
        assert s.get(ExchangeAccount, d2_id) is None and s.query(Trade).filter_by(account_id=d2_id).count() == 0
        assert s.get(ExchangeAccount, d1_id) is not None
    assert "same API key" not in c.get("/accounts").text
