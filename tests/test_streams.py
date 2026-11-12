"""Scaling: account WebSockets replace balance/position/price requests; outgoing IPs each with their own budget;
exits before entries when requests are scarce."""
import asyncio

import pytest

from tests import fake_exchange as fx

import app.engine.exchange as exmod
from app.db import Bot, ExchangeAccount, ExecutionLog, SessionLocal, Trade, User, get_global, init_db
from app.engine import netips, ratelimit
from app.engine.dispatcher import dispatch
from app.engine.exchange import pool
from app.engine.streams import manager
from app.security import encrypt
from app.signal import Signal


class FakeStream:
    """What ccxt pro gives: first the snapshot, then updates pushed by the exchange (here: one, then silence)."""

    def __init__(self, balance=None, positions=None, marks=None, rest_positions=None):
        self.b, self.p, self.m = balance, positions, marks
        self.rest_b = balance
        self.rest_p = list(positions or []) if rest_positions is None else rest_positions  # what REST says
        self.closed = False

    async def fetch_balance(self):  # the starting balance (REST, once) and the periodic check
        return self.b if self.b is not None else self.rest_b

    async def fetch_positions(self):  # the periodic check against the stream
        return self.rest_p

    async def watch_balance(self):
        if self.b is None:
            await asyncio.Event().wait()
        b, self.b = self.b, None
        return b

    async def watch_positions(self):
        if self.p is None:
            await asyncio.Event().wait()
        p, self.p = self.p, None
        return p

    async def watch_mark_prices(self):
        if self.m is None:
            await asyncio.Event().wait()
        m, self.m = self.m, None
        return m

    async def close(self):
        self.closed = True


@pytest.fixture
def live(monkeypatch):
    import app.engine.streams as streams_mod
    monkeypatch.setattr(streams_mod, "VERIFY_FIRST", 0)
    init_db()
    monkeypatch.setattr(exmod.ccxt, "binanceusdm", fx.FakeExchange)
    fx.FakeExchange.positions = []
    with SessionLocal() as s:
        gs = get_global(s)
        saved = (list(gs.allowed_pairs or []), list(gs.allowed_exchanges or []), gs.entries_enabled)
        gs.allowed_pairs, gs.allowed_exchanges, gs.entries_enabled = ["BTC/USDT", "ETH/USDT"], ["binance"], True
        bot = Bot(name="StreamBot", webhook_secret="stream-bot")
        u = User(email="stream@x.com", name="S", password_hash="x", status="active", trading_enabled=True)
        s.add_all([bot, u])
        s.commit()
        acc = ExchangeAccount(user_id=u.id, kind="exchange", label="s-live", exchange="binance", trading_mode="futures",
                              dry_run=False, sizing_mode="wallet_pct", wallet_pct=50, pairs={"BTC/USDT": 1.0, "ETH/USDT": 1.0},
                              bots=[bot.id], pair_bot={"BTC/USDT": bot.id, "ETH/USDT": bot.id},
                              api_key_enc=encrypt("k"), secret_enc=encrypt("s"))
        s.add(acc)
        s.commit()
        ids = (acc.id, bot.id, u.id)
    yield ids
    manager.factory = None
    manager.marks.clear()
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


def test_entry_and_live_view_from_the_stream(live):
    acc_id, bot_id, _ = live
    import time as _t
    pushed = FakeStream(balance={"USDT": {"free": 8000.0, "total": 8000.0}, "total": {"USDT": 8000.0}},
                        positions=[{"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 0.01, "contractSize": 1,
                                    "entryPrice": 61000.0, "leverage": 5, "unrealizedPnl": 0}])
    manager.factory = lambda acc: pushed

    async def go():
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, acc_id)
            s.expunge(acc)
        await manager.start(acc, exmod.client_version(acc))
        await asyncio.sleep(0.05)
        assert manager.live(acc_id) and manager.wallet(acc_id, "USDT") == 8000
        assert manager.status(acc)["mode"] == "live"
        manager.marks["ETH/USDT:USDT"] = (_t.time(), 3000.0)
        manager.marks["BTC/USDT:USDT"] = (_t.time(), 60000.0)
        client = await pool.get(acc)
        client.ex.id = "binanceusdm"  # behave like Binance futures (fill returned with the order)
        out = await dispatch(Signal(action="long", symbol="ETHUSDT", pair="ETH/USDT:USDT", ft_trade_id=5, leverage=2,
                                    id="ws-1"), None, bot_id)
        assert [o.status for o in out] == ["opened"], out
        with SessionLocal() as s:
            tr = s.query(Trade).filter_by(account_id=acc_id, pair="ETH/USDT").one()
            # 8,000 wallet (from the stream) x 50% / 2 pairs = 2,000 margin, x2 at the mark price 3,000
            assert tr.amount == pytest.approx(1.333)
            calls = [c["method"] for c in s.query(ExecutionLog).filter_by(account_id=acc_id).one().calls]
        assert "fetch_balance" not in calls, calls  # the balance is pushed
        assert "fetch_positions" in calls  # but whether a position is open is always asked of the exchange itself
        assert fx.FakeExchange.instances[-1].orders[-1]["params"].get("newOrderRespType") == "RESULT"
        # live page numbers: the BTC short we hold is priced from the mark stream, no request
        from app.engine import livepos
        st = await livepos.refresh(acc_id, max_age=0)
        btc = st["rows"]["BTC/USDT:USDT|short"]
        assert btc["mark"] == 60000 and btc["upnl"] == pytest.approx(10.0)  # (61,000 - 60,000) x 0.01
        assert manager.free_estimate(acc_id, "USDT") == pytest.approx(8000 - 120 - 0, rel=0.01)  # 600 notional / 5x
        await manager.stop(acc_id)
        assert pushed.closed and not manager.live(acc_id)
        assert manager.status(acc)["mode"] == "api"  # shown on the portfolio card: works with normal requests
        acc.dry_run = True
        assert manager.status(acc) is None  # paper trading: nothing to show
    asyncio.run(go())


def test_outgoing_ips_and_their_own_budgets():
    init_db()
    assert netips.usable("127.0.0.2") == "" and netips.usable("203.0.113.77") and netips.usable("x")
    with SessionLocal() as s:
        gs = get_global(s)
        gs.outbound_ips = ["127.0.0.2", "127.0.0.3"]
        u = User(email="ips@x.com", name="I", password_hash="x", status="active")
        s.add(u)
        s.commit()
        a1 = ExchangeAccount(user_id=u.id, kind="exchange", label="ip-1", exchange="binance")
        a2 = ExchangeAccount(user_id=u.id, kind="exchange", label="ip-2", exchange="binance")
        s.add_all([a1, a2])
        s.commit()
        s.expunge_all()
    try:
        assert {netips.ip_for(a1), netips.ip_for(a2)} == {"127.0.0.2", "127.0.0.3"}  # spread, least used first
        assert netips.ip_for(a1) == a1.out_ip  # and kept
        from app.web.guides import server_ips
        assert {"127.0.0.2", "127.0.0.3"} <= set(server_ips())  # users are told to allow every one

        async def go():  # a real request really leaves from the chosen address
            from aiohttp import web
            seen = []

            async def h(req):
                seen.append(req.remote)
                return web.json_response({})
            app_ = web.Application()
            app_.router.add_get("/", h)
            runner = web.AppRunner(app_)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            from ccxt.async_support.binanceusdm import binanceusdm as Real
            ex = Real({})
            netips.bind(ex, "127.0.0.3")
            gov = ratelimit.Governor()
            gov.attach(ex, "127.0.0.3")
            async with ex.session.get(f"http://127.0.0.1:{port}/") as r:
                await r.json()
            await ex.close()
            await runner.cleanup()
            assert seen == ["127.0.0.3"]
            gov2 = ratelimit.Governor()
            await gov2.acquire("binanceusdm@127.0.0.2", 5, "trade")
            await gov2.acquire("binanceusdm@127.0.0.3", 5, "trade")
            assert {g["gid"] for g in gov2.snapshot()} == {"binanceusdm@127.0.0.2", "binanceusdm@127.0.0.3"}
            assert gov2.groups["binanceusdm@127.0.0.2"].limit == 2400
        asyncio.run(go())
    finally:
        with SessionLocal() as s:
            get_global(s).outbound_ips = None
            s.query(ExchangeAccount).filter(ExchangeAccount.label.in_(["ip-1", "ip-2"])).delete(synchronize_session=False)
            s.query(User).filter_by(email="ips@x.com").delete()
            s.commit()


def test_exits_before_entries():
    gov = ratelimit.Governor()
    gov.budget_pct = 100
    gov.group("binanceusdm").limit = 100

    async def go():
        await gov.acquire("binanceusdm", 88, "trade")
        old = ratelimit.MAX_WAIT["trade"]
        ratelimit.MAX_WAIT["trade"] = 0.3
        try:
            t0 = asyncio.get_running_loop().time()
            await gov.acquire("binanceusdm", 5, "trade")  # entries stop at 90%: this one has to wait
            assert asyncio.get_running_loop().time() - t0 >= 0.25
        finally:
            ratelimit.MAX_WAIT["trade"] = old
        t0 = asyncio.get_running_loop().time()
        await gov.acquire("binanceusdm", 5, "exit")  # the last 10% is kept for exits: no wait
        assert asyncio.get_running_loop().time() - t0 < 0.1
    asyncio.run(go())


def test_live_without_any_balance_change_and_after_a_reconnect(live):
    """Binance pushes the balance only when it changes: a quiet account must still go live (starting balance read
    once); after a dropped connection everything is read again before it counts as live."""
    acc_id, _, _ = live

    class Quiet(FakeStream):
        drops = 1

        async def watch_balance(self):  # no balance change ever
            await asyncio.Event().wait()

        async def watch_positions(self):
            if Quiet.drops:
                Quiet.drops -= 1
                await asyncio.sleep(0.02)
                raise ConnectionError("connection reset")
            return await FakeStream.watch_positions(self)

    pushed = Quiet(balance={"USDT": {"free": 500.0, "total": 500.0}, "total": {"USDT": 500.0}}, positions=[])
    manager.factory = lambda acc: pushed
    import app.engine.streams as streams_mod

    async def go():
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, acc_id)
            s.expunge(acc)
        old = streams_mod.asyncio.sleep
        await manager.start(acc, exmod.client_version(acc))
        await asyncio.sleep(0.01)
        assert manager.status(acc)["mode"] == "connecting"  # positions snapshot not there yet
        for _ in range(60):  # first positions attempt drops, then retried after 2 s
            await asyncio.sleep(0.1)
            if manager.live(acc_id):
                break
        assert manager.live(acc_id) and manager.wallet(acc_id, "USDT") == 500
        assert manager.status(acc)["mode"] == "live"
        await manager.stop(acc_id)
        assert old is streams_mod.asyncio.sleep
    asyncio.run(go())


def test_wallet_balances(live):
    """Dashboard wallet: from the live connection when there is one, else one read per minute (then cached)."""
    acc_id, _, _ = live
    from app.engine import livepos
    import time as _t

    async def go():
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, acc_id)
            s.expunge(acc)
        livepos._bal.clear()
        b = (await livepos.balances([acc], "USDT"))[acc_id]
        assert b == {"wallet": 10_000.0, "equity": 10_000.0, "free": 10_000.0, "live": False, "cur": "USDT"}  # REST (fake)
        client = await pool.get(acc)
        client.ex.balance["USDT"] = 1.0
        assert (await livepos.balances([acc], "USDT"))[acc_id]["wallet"] == 10_000  # cached for a minute
        assert livepos.cached_balances([acc], "USDT")[acc_id]["wallet"] == 10_000  # what the page renders at once
        pushed = FakeStream(balance={"USDT": {"free": 700.0, "total": 700.0}, "total": {"USDT": 700.0}},
                            positions=[{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.01, "contractSize": 1,
                                        "entryPrice": 60000.0, "leverage": 5}])
        manager.factory = lambda a: pushed
        await manager.start(acc, exmod.client_version(acc))
        await asyncio.sleep(0.05)
        manager.marks["BTC/USDT:USDT"] = (_t.time(), 61000.0)
        b = (await livepos.balances([acc], "USDT"))[acc_id]
        assert b["live"] and b["wallet"] == 700 and b["equity"] == pytest.approx(710)  # +10 open profit
        await manager.stop(acc_id)
        livepos._bal.clear()
    asyncio.run(go())


def test_a_silent_stream_is_caught_and_replaced(live):
    """Seen in production: the user-data stream stayed 'connected' but delivered nothing for hours, so the relay
    believed closed positions were still open (and 'adopted' them instead of opening new ones) and didn't see the
    ones it had just opened. The periodic REST check replaces the stream's state and rebuilds the connection;
    trading never decides from the stream."""
    acc_id, bot_id, _ = live
    stale = [{"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 0.01, "contractSize": 1, "entryPrice": 61000.0}]
    real = [{"symbol": "ETH/USDT:USDT", "side": "long", "contracts": 0.5, "contractSize": 1, "entryPrice": 3000.0}]
    pushed = FakeStream(balance={"USDT": {"free": 900.0, "total": 900.0}, "total": {"USDT": 900.0}},
                        positions=stale, rest_positions=real)
    manager.factory = lambda acc: pushed
    fx.FakeExchange.positions = []  # what the trading client reads: no BTC position

    async def go():
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, acc_id)
            s.expunge(acc)
        await manager.start(acc, exmod.client_version(acc))
        st = manager.accounts[acc_id]
        for _ in range(50):
            await asyncio.sleep(0.02)
            if st.drifts:
                break
        assert st.drifts == 1 and "BTC/USDT short" in st.last_drift and "ETH/USDT long" in st.last_drift
        assert set(st.positions) == {"ETH/USDT:USDT|long"}  # replaced by what the exchange really has
        assert st.version[0] == "drift"  # sync() rebuilds the connection
        # the trading path doesn't use the stream: a BTC entry opens a real position, nothing is "adopted"
        st.positions = {"BTC/USDT:USDT|short": stale[0]}  # even if the stream froze again
        out = await dispatch(Signal(action="short", symbol="BTCUSDT", pair="BTC/USDT:USDT", ft_trade_id=9, leverage=2,
                                    id="ws-drift"), None, bot_id)
        assert [o.status for o in out] == ["opened"], out
        await manager.stop(acc_id)
    asyncio.run(go())
