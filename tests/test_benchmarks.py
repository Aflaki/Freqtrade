"""Market benchmarks: fetching (fake exchange + fake CoinGecko), storage, % series and bot-vs-market chart."""
import asyncio
import datetime as dt
import types

import httpx
import pytest

import app.engine.benchmarks as bench
from app.db import BenchmarkPrice, SessionLocal, init_db
from app.engine.performance import market_chart

DAY = 86_400_000
N_DAYS = 1500  # > 1000 so pagination is exercised; data ends yesterday like a real exchange
START_DAY = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=N_DAYS)
START = int(dt.datetime.combine(START_DAY, dt.time(), dt.UTC).timestamp() * 1000)


class FakeExchange:
    def __init__(self, params):
        pass

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        base = {"BTC/USDT": 50_000, "ETH/USDT": 3_000}.get(symbol, 100)
        days = [START + i * DAY for i in range(N_DAYS)]
        days = [d for d in days if d >= (since or 0)][:limit]
        return [[d, 0, 0, 0, base * (1 + (d - START) / DAY / 1000), 0] for d in days]  # +0.1%/day

    async def close(self):
        pass


def coingecko(request: httpx.Request):
    path = request.url.path
    if path.endswith("/coins/markets"):
        coins = [("btc", "bitcoin", "Bitcoin"), ("eth", "ethereum", "Ethereum"), ("usdt", "tether", "Tether"),
                 ("xrp", "ripple", "XRP"), ("steth", "staked-ether", "Lido Staked Ether"), ("bnb", "binancecoin", "BNB"),
                 ("usdc", "usd-coin", "USDC"), ("sol", "solana", "Solana"), ("doge", "dogecoin", "Dogecoin"),
                 ("wbtc", "wrapped-bitcoin", "Wrapped Bitcoin"), ("ada", "cardano", "Cardano"), ("trx", "tron", "TRON")]
        return httpx.Response(200, json=[{"symbol": s, "id": i, "name": n, "market_cap": 1e9} for s, i, n in coins])
    if path.endswith("/global/market_cap_chart"):
        return httpx.Response(401, json={"error": "pro only"})
    if path.endswith("/global"):
        return httpx.Response(200, json={"data": {"total_market_cap": {"usd": 3.0e12}}})
    if "/market_chart" in path:
        now = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
        return httpx.Response(200, json={"market_caps": [[now - k * DAY, 1e11] for k in range(365, -1, -1)]})
    return httpx.Response(404)


def test_update_and_chart(monkeypatch):
    init_db()
    for ex in bench.EXCHANGES:
        monkeypatch.setattr(bench.ccxt, ex, FakeExchange)
    real = httpx.AsyncClient
    from app.engine import coingecko as cg
    monkeypatch.setattr(cg.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(coingecko), **kw))
    monkeypatch.setattr(cg, "GAP_KEYLESS", 0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bench.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(cg.asyncio, "sleep", lambda s: real_sleep(0))

    st = asyncio.run(bench.update_all())
    assert st["symbols"] == ["BTC", "ETH", "XRP", "BNB", "SOL"], st  # no stablecoins / wrapped tokens
    assert not st["errors"], st["errors"]
    with SessionLocal() as s:
        assert s.query(BenchmarkPrice).filter_by(symbol="BTC").count() == N_DAYS  # paginated over 1000
        total = s.query(BenchmarkPrice).filter_by(symbol="TOTAL").all()
        assert len(total) >= 300 and any(t.source == "top10-scaled" for t in total)
        today = [t for t in total if t.source == "coingecko-global"]
        assert today and today[0].close == 3.0e12
    assert bench.is_approx_total()
    assert bench.symbols()[:3] == ["TOTAL", "BTC", "ETH"]

    # % series from a start date: first close = 0%, +0.1% per day afterwards
    ser = bench.series("BTC", START_DAY, START_DAY + dt.timedelta(days=10))
    assert ser[0][1] == 0 and ser[-1][1] == pytest.approx(1.0, rel=1e-3)

    # bot vs market on the same dates
    def trade(day, amount):
        d = dt.datetime.combine(START_DAY, dt.time()) + dt.timedelta(days=day)
        return types.SimpleNamespace(profit_abs=amount, profit_ratio=0.01, stake_amount=None, leverage=1.0,
                                     open_date=d, close_date=d, pair="BTC/USDT", bot_id=1)
    mk = market_chart([trade(1, 50), trade(10, 50)], capital=1000, days=None)
    rows = {r["key"]: r for r in mk["rows"]}
    assert rows["bot"]["ret"] == pytest.approx(10.0)       # 100 profit on 1000
    assert "BTC" in rows and "TOTAL" in rows
    assert mk["chart"]["unit"] == "%" and len(mk["chart"]["xticks"]) == 5 and mk["chart"]["yticks"]

    no_cap = market_chart([trade(1, 50)], capital=None, days=30)
    assert not no_cap["has_capital"] and no_cap["rows"] == []


def test_coingecko_waits_and_retries_on_429(monkeypatch):
    """Without a key CoinGecko allows ~5 calls/min: calls are spaced out and a 429 is retried, not fatal."""
    from app.engine import coingecko as cg
    init_db()
    calls, slept = [], []
    answers = iter([429, 200])

    def handler(request):
        calls.append(str(request.url))
        code = next(answers)
        return httpx.Response(code, json={"ok": code == 200}, headers={"retry-after": "7"} if code == 429 else {})
    real = httpx.AsyncClient
    monkeypatch.setattr(cg.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(cg.asyncio, "sleep", fake_sleep)
    cg._last = 0.0
    assert asyncio.run(cg.get("/global")) == {"ok": True}
    assert len(calls) == 2 and 61 in slept  # keyless: waits at least a minute after a 429
    assert any(9 < s <= 13 for s in slept)  # and keeps 13 s between calls
