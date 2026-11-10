"""Positions already open on the exchange when a portfolio is switched on (e.g. from a retired Freqtrade copy):
chosen pairs are taken over and closed by the bot's exit; the others wait for the user's close/keep decision."""
import asyncio

import pytest

from tests import fake_exchange as fx
from tests.test_catalog import login

import app.engine.exchange as exmod
from app.db import Bot, ExchangeAccount, ExecutionLog, SessionLocal, Trade, User, get_global, init_db
from app.engine.botmonitor import monitor
from app.engine.dispatcher import dispatch
from app.engine.exchange import pool
from app.security import encrypt
from app.signal import Signal


def pos(symbol, side, contracts, entry, lev=5):
    notional = contracts * entry
    return {"symbol": symbol, "side": side, "contracts": contracts, "contractSize": 1, "entryPrice": entry,
            "markPrice": entry, "leverage": lev, "notional": notional, "initialMargin": notional / lev,
            "unrealizedPnl": 1.5}


@pytest.fixture
def env(monkeypatch):
    init_db()
    monkeypatch.setattr(exmod.ccxt, "binanceusdm", fx.FakeExchange)
    fx.FakeExchange.positions = []
    with SessionLocal() as s:
        gs = get_global(s)
        saved = (list(gs.allowed_pairs or []), list(gs.allowed_exchanges or []), gs.entries_enabled)
        gs.allowed_pairs, gs.allowed_exchanges, gs.entries_enabled = ["BTC/USDT", "ETH/USDT"], ["binance"], True
        b = Bot(name="PosBot", webhook_secret="pos-bot")
        s.add(b)
        s.commit()
        bot_id = b.id
    c, csrf = login("pos-u@x.com")
    with SessionLocal() as s:
        u = s.query(User).filter_by(email="pos-u@x.com").one()
        acc = ExchangeAccount(user_id=u.id, kind="exchange", label="pos-1", exchange="binance", trading_mode="futures",
                              dry_run=False, enabled=False, pairs={"ETH/USDT": 1.0}, bots=[bot_id],
                              pair_bot={"ETH/USDT": bot_id}, capital=1000, api_key_enc=encrypt("k"),
                              secret_enc=encrypt("s"))
        s.add(acc)
        s.commit()
        acc_id = acc.id
    yield c, csrf, acc_id, bot_id
    monitor.live.pop(bot_id, None)
    fx.FakeExchange.positions = []
    asyncio.run(pool.drop(acc_id))
    with SessionLocal() as s:
        s.query(Trade).filter_by(account_id=acc_id).delete()
        s.query(ExecutionLog).filter_by(account_id=acc_id).delete()
        s.query(ExchangeAccount).filter_by(id=acc_id).delete()
        s.query(Bot).filter_by(id=bot_id).delete()
        gs = get_global(s)
        gs.allowed_pairs, gs.allowed_exchanges, gs.entries_enabled = saved
        s.commit()


def run(coro):
    return asyncio.run(coro)


def test_take_over_and_ask(env):
    c, csrf, acc_id, bot_id = env
    # the bot is in an ETH long right now (live poll); the old copy left ETH long + BTC short on the account
    monitor.live[bot_id] = {"trades": [{"ft_trade_id": 77, "pair": "ETH/USDT", "side": "long", "stake_amount": 100.0}]}
    fx.FakeExchange.positions = [pos("ETH/USDT:USDT", "long", 0.5, 2900.0), pos("BTC/USDT:USDT", "short", 0.01, 61000.0)]

    n_orders = sum(len(i.orders) for i in fx.FakeExchange.instances)
    r = c.post(f"/accounts/{acc_id}/toggle", data={"csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == f"/accounts/{acc_id}/positions"  # switched on -> asked about BTC
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id).one()
        assert tr.adopted and tr.pair == "ETH/USDT" and tr.side == "long" and tr.bot_id == bot_id
        assert tr.ft_trade_id == 77 and tr.amount == 0.5 and tr.entry_price == 2900 and tr.stake == pytest.approx(290)
        assert tr.scale == pytest.approx(2.9) and tr.leverage == 5
        acc = s.get(ExchangeAccount, acc_id)
        assert [p["pair"] for p in acc.undecided_positions] == ["BTC/USDT"]
    assert sum(len(i.orders) for i in fx.FakeExchange.instances) == n_orders  # nothing bought or sold

    page = c.get(f"/accounts/{acc_id}/positions").text
    assert "BTC/USDT" in page and "PosBot" in page and "same trade now" in page and 'value="close"' in page
    assert "waiting for your decision" in c.get("/accounts").text

    # a second scan changes nothing (no double take-over)
    c.post(f"/accounts/{acc_id}/positions/scan", data={"csrf": csrf})
    with SessionLocal() as s:
        assert s.query(Trade).filter_by(account_id=acc_id).count() == 1

    # keep BTC: never asked again, never touched
    key = "BTC/USDT:USDT|short"
    r = c.post(f"/accounts/{acc_id}/positions", data={"csrf": csrf, f"d__{key}": "keep"}, follow_redirects=False)
    assert r.headers["location"] == "/accounts"
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        assert acc.kept_positions == [key] and acc.undecided_positions == []
    assert "waiting for your decision" not in c.get("/accounts").text

    # the bot's exit closes the taken-over ETH position on the exchange
    out = run(dispatch(Signal(action="close_long", symbol="ETHUSDT", pair="ETH/USDT:USDT", ft_trade_id=77,
                              exit_reason="roi", id="pos-x1"), None, bot_id))
    assert [o.status for o in out] == ["closed"]
    assert [p["symbol"] for p in fx.FakeExchange.positions] == ["BTC/USDT:USDT"]  # kept one untouched

    # changed mind: close BTC now (reduce-only market order)
    r = c.post(f"/accounts/{acc_id}/positions", data={"csrf": csrf, f"d__{key}": "close"}, follow_redirects=False)
    assert r.headers["location"] == "/accounts" and fx.FakeExchange.positions == []
    last = [o for i in fx.FakeExchange.instances for o in i.orders][-1]
    assert last["symbol"] == "BTC/USDT:USDT" and last["side"] == "buy" and last["params"] == {"reduceOnly": True}
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        assert not acc.foreign_positions and not acc.kept_positions


def test_entry_takes_over_position_on_exchange(env):
    """Portfolio saved before the scan (or a position opened by hand later): the bot's entry adopts it, no double."""
    c, csrf, acc_id, bot_id = env
    with SessionLocal() as s:
        s.get(ExchangeAccount, acc_id).enabled = True
        s.commit()
    fx.FakeExchange.positions = [pos("ETH/USDT:USDT", "short", 0.2, 3100.0)]
    n_orders = sum(len(i.orders) for i in fx.FakeExchange.instances)
    out = run(dispatch(Signal(action="short", symbol="ETHUSDT", pair="ETH/USDT:USDT", ft_trade_id=88, stake=50.0,
                              leverage=5, id="pos-e1"), None, bot_id))
    assert [o.status for o in out] == ["adopted"]
    assert sum(len(i.orders) for i in fx.FakeExchange.instances) == n_orders  # nothing bought
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id).one()
        assert tr.adopted and tr.ft_trade_id == 88 and tr.side == "short" and tr.scale == pytest.approx(124 / 50)
        tr.status = "closed"  # forget it: the short is now someone else's
        s.commit()
    # opposite direction on the exchange: left alone, skipped with a reason
    out = run(dispatch(Signal(action="long", symbol="ETHUSDT", pair="ETH/USDT:USDT", ft_trade_id=89, stake=50.0,
                              id="pos-e2"), None, bot_id))
    assert [o.status for o in out] == ["skipped"] and "SHORT" in out[0].message
    assert sum(len(i.orders) for i in fx.FakeExchange.instances) == n_orders
