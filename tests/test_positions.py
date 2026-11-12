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
    fx.FakeExchange.positions, fx.FakeExchange.fills = [], []
    from app.engine import livepos
    livepos._cache.clear()
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


async def pos_sync(acc_id):
    from app.engine.positions import sync_account
    return await sync_account(acc_id, notify=False)


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


def test_open_time_from_fills():
    import datetime as dt

    from app.engine.livepos import opened_at_from_fills
    t = lambda h: int(dt.datetime(2026, 11, 1, h).replace(tzinfo=dt.UTC).timestamp() * 1000)  # noqa: E731
    fills = [{"side": "buy", "amount": 1, "timestamp": t(1)}, {"side": "sell", "amount": 1, "timestamp": t(2)},  # old
             {"side": "buy", "amount": 0.3, "timestamp": t(5)}, {"side": "buy", "amount": 0.2, "timestamp": t(7)}]
    assert opened_at_from_fills(fills, "long", 0.5) == dt.datetime(2026, 11, 1, 5)
    flip = [{"side": "buy", "amount": 1, "timestamp": t(1)}, {"side": "sell", "amount": 1.4, "timestamp": t(3)}]
    assert opened_at_from_fills(flip, "short", 0.4) == dt.datetime(2026, 11, 1, 3)  # long flipped to short
    assert opened_at_from_fills(fills[2:3], "long", 0.5) is None  # history too short: unknown


def test_live_numbers_from_the_exchange_and_reconcile(env):
    import datetime as dt
    import time as _t

    from app.engine import livepos, reconcile
    c, csrf, acc_id, bot_id = env
    with SessionLocal() as s:
        acc = s.get(ExchangeAccount, acc_id)
        acc.enabled = True
        acc.pairs, acc.pair_bot = {"ETH/USDT": 1.0, "BTC/USDT": 1.0}, {"ETH/USDT": bot_id, "BTC/USDT": bot_id}
        s.commit()
    now = int(_t.time() * 1000)
    eth = pos("ETH/USDT:USDT", "long", 0.5, 2900.0)
    eth.update(markPrice=3000.0, unrealizedPnl=50.0, liquidationPrice=2400.0)
    fx.FakeExchange.positions = [eth]
    fx.FakeExchange.fills = [{"symbol": "ETH/USDT:USDT", "side": "buy", "amount": 0.5, "timestamp": now - 3 * 86400_000}]
    monitor.live[bot_id] = {"at": dt.datetime.now(), "error": "", "trades": [
        {"ft_trade_id": 7, "pair": "BTC/USDT", "side": "short", "stake_amount": 100.0, "leverage": 2,
         "open_rate": 61000.0, "open_date": dt.datetime(2026, 11, 9)}]}
    run(pos_sync(acc_id))  # takes ETH over (in the chosen pairs)
    with SessionLocal() as s:
        tr = s.query(Trade).filter_by(account_id=acc_id, pair="ETH/USDT").one()
        tr.entry_price = 1.0  # our record drifted: the exchange is the truth
        s.commit()
        tid = tr.id
    livepos._cache.clear()
    page = c.get("/dashboard").text  # opens at once, without asking the exchange
    assert 'id="upnl-card"' in page and "…" in page

    async def read_live():  # what /trades/live does, plus the background open-time lookup
        await livepos.refresh(acc_id, max_age=0)
        await livepos.drain()
    run(read_live())
    page = c.get("/dashboard").text  # now from what was read
    assert "+50.00" in page and "3,000" in page and "2,400" in page  # live price, P/L, liquidation
    assert "1 in profit · 0 in loss" in page
    with SessionLocal() as s:
        tr = s.get(Trade, tid)
        assert tr.entry_price == 2900 and abs((dt.datetime.now(dt.UTC).replace(tzinfo=None) - tr.opened_at).days - 3) <= 1  # real open time
    live = c.get("/trades/live").json()
    assert live[str(tid)]["upnl"] == 50 and live[str(tid)]["pct"] == pytest.approx(50 / 290 * 100)

    # bots vs exchange: the bot is short BTC (account isn't), the account is long ETH (bot isn't)
    res = run(reconcile.compare(acc_id))
    kinds = {r["pair"]: r["kind"] for r in res["diffs"]}
    assert kinds == {"BTC/USDT": "bot_only", "ETH/USDT": "exch_only"} and res["bot_total"] == 1
    assert res["exchange_total"] == 1 and res["open"] == 2
    page = c.get(f"/accounts/{acc_id}/positions").text
    assert "The bot is in a trade; the account isn" in page and "Only on the exchange" in page
    assert "2 difference(s) await your decision" in c.get("/accounts").text and "Reconcile with the bots" in c.get("/accounts").text
    keys = {r["pair"]: r["key"] for r in res["diffs"]}
    c.post(f"/accounts/{acc_id}/reconcile", data={"csrf": csrf, f"r__{keys['ETH/USDT']}": "keep",
                                                  f"r__{keys['BTC/USDT']}": "open"})
    with SessionLocal() as s:
        btc = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT", status="open").one()
        assert btc.side == "short" and btc.ft_trade_id == 7 and btc.bot_id == bot_id and btc.enter_tag == "catch-up"
        assert s.get(ExchangeAccount, acc_id).recon_ack == [keys["ETH/USDT"]]
    fx.FakeExchange.positions.append(pos("BTC/USDT:USDT", "short", btc.amount, 60000.0))
    livepos._cache.clear()
    res = run(reconcile.compare(acc_id))
    assert [r["pair"] for r in res["diffs"]] == ["ETH/USDT"] and res["open"] == 0 and res["matched"] == 1
    c.post(f"/accounts/{acc_id}/reconcile", data={"csrf": csrf, f"r__{keys['ETH/USDT']}": "close"})
    assert [p["symbol"] for p in fx.FakeExchange.positions] == ["BTC/USDT:USDT"]  # ETH closed on the exchange
    with SessionLocal() as s:
        assert s.get(Trade, tid).status == "closed"

    # a ghost: our record says BTC is open, the exchange has nothing (e.g. 'adopted' from a frozen stream)
    fx.FakeExchange.positions = []
    livepos._cache.clear()
    res = run(reconcile.compare(acc_id))
    ghost = next(r for r in res["diffs"] if r["pair"] == "BTC/USDT")
    assert ghost["kind"] == "bot_only" and ghost["ghost"] and ghost["trade_id"] == btc.id
    assert "no position on the exchange" in c.get(f"/accounts/{acc_id}/positions").text
    c.post(f"/accounts/{acc_id}/reconcile", data={"csrf": csrf, f"r__{ghost['key']}": "open"})
    with SessionLocal() as s:
        old = s.get(Trade, btc.id)
        assert old.status == "void" and old.exit_reason == "not_on_exchange" and old.realized_pnl == 0
        new = s.query(Trade).filter_by(account_id=acc_id, pair="BTC/USDT", status="open").one()
        assert new.id != btc.id and new.enter_tag == "catch-up"
    last = fx.FakeExchange.instances[-1].orders[-1]  # really ordered this time
    assert last["symbol"] == "BTC/USDT:USDT" and last["side"] == "sell" and not last["params"].get("reduceOnly")

    # the page applies over fetch: a JSON answer (then it reads everything again); never two applies at once
    from app.web import user as uweb
    r = c.post(f"/accounts/{acc_id}/reconcile", data={"csrf": csrf}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.json() == {"lines": [], "failed": 0}

    async def while_busy():
        lock = uweb._recon_locks.setdefault(acc_id, asyncio.Lock())
        async with lock:
            import httpx
            from app.main import app as _app
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app), base_url="http://testserver",
                                         cookies=dict(c.cookies)) as ac:
                return await ac.post(f"/accounts/{acc_id}/reconcile", data={"csrf": csrf},
                                     headers={"X-Requested-With": "fetch"})
    r = asyncio.run(while_busy())
    assert r.status_code == 409 and r.json()["busy"]
    page = c.get(f"/accounts/{acc_id}/positions").text
    assert 'id="rcbusy"' in page or "rc_all_matched" not in page
