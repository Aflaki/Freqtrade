"""Open trades ("live" tab): Freqtrade /status parsing, merge with webhook-only bots, filters, page + partial."""
import asyncio
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Bot, BotTrade, SessionLocal, init_db
from app.engine import botmonitor, performance as perf
from app.engine.botmonitor import monitor, parse_open_trades
from app.main import app
from app.security import encrypt

# trimmed from a real Freqtrade 2024+/api/v1/status answer (futures, 5x short)
STATUS = [
    {"trade_id": 9801, "pair": "SSV/USDT:USDT", "is_short": True, "leverage": 5.0, "open_date": "2026-11-09 08:00:00",
     "open_timestamp": 1794211200000, "open_rate": 2.188, "current_rate": 2.10, "profit_ratio": 0.2011,
     "profit_pct": 20.11, "profit_abs": 20.07, "stake_amount": 100.0, "stop_loss_abs": 2.3, "enter_tag": "rsi_x",
     "has_open_orders": False},
    {"trade_id": 9802, "pair": "BTC/USDT:USDT", "is_short": False, "leverage": 5.0, "open_date": "2026-11-09T09:30:00+00:00",
     "open_rate": 100000, "current_rate": 99000, "profit_ratio": -0.0502, "profit_abs": -5.02, "stake_amount": 100.0,
     "enter_tag": "", "has_open_orders": True},
    {"trade_id": 9803, "pair": "DOGE/USDT:USDT", "is_short": False, "leverage": 3, "open_date": "2026-11-09 07:00:00",
     "open_rate": 0.2, "current_rate": 0.21, "profit_ratio": 0.15, "profit_abs": 15.0, "stake_amount": 100.0},
]


def test_parse_status():
    rows = parse_open_trades(STATUS + ["junk", {"no": "pair"}])
    assert len(rows) == 3
    a = rows[0]
    assert a["pair"] == "SSV/USDT" and a["side"] == "short" and a["leverage"] == 5.0
    assert a["profit_ratio"] == pytest.approx(0.2011) and a["stop_loss"] == 2.3 and not a["pending"]
    assert a["open_date"] == dt.datetime(2026, 11, 9, 8, 0)
    assert rows[1]["open_date"] == dt.datetime(2026, 11, 9, 9, 30) and rows[1]["pending"]


@pytest.fixture
def bots(monkeypatch):
    init_db()
    with SessionLocal() as s:
        api = Bot(name="LiveAPI", webhook_secret="live-sec-1", api_url="http://ft.local:8080", api_user="u",
                  api_pass_enc=encrypt("p"), pair_filter_mode="blacklist", pair_filter=["DOGE/USDT"])
        hook = Bot(name="LiveHook", webhook_secret="live-sec-2")
        s.add_all([api, hook])
        s.commit()
        s.add(BotTrade(bot_id=hook.id, ft_trade_id=5, pair="ETH/USDT", side="long", leverage=2.0,
                       open_date=dt.datetime(2026, 11, 9, 6, 0), open_rate=3000.0, stake_amount=50.0, is_open=True))
        s.commit()
        ids = (api.id, hook.id)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/status" and request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json=STATUS)
    real = httpx.AsyncClient
    monkeypatch.setattr(botmonitor.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    yield ids
    monitor.live.clear()
    with SessionLocal() as s:
        s.query(BotTrade).filter(BotTrade.bot_id.in_(ids)).delete()
        s.query(Bot).filter(Bot.id.in_(ids)).delete()
        s.commit()


def test_live_merge_and_filters(bots):
    api_id, hook_id = bots
    asyncio.run(monitor.poll_live())
    assert len(monitor.live[api_id]["trades"]) == 3 and hook_id not in monitor.live
    with SessionLocal() as s:
        bs = [s.get(Bot, api_id), s.get(Bot, hook_id)]
    live = perf.live_trades(bs)
    pairs = {r["pair"] for r in live["rows"]}
    assert pairs == {"SSV/USDT", "BTC/USDT", "ETH/USDT"}  # DOGE is blacklisted for that bot
    assert live["profit_abs"] == pytest.approx(15.05) and live["stake"] == 200.0
    assert live["winning"] == 1 and live["priced"] == 2 and live["longs"] == 2 and live["shorts"] == 1
    eth = next(r for r in live["rows"] if r["pair"] == "ETH/USDT")
    assert eth["current_rate"] is None and not eth["live"]  # webhook-only bot: no price
    assert [r["pair"] for r in perf.live_trades(bs, "short")["rows"]] == ["SSV/USDT"]


def test_live_keeps_last_data_when_unreachable(bots, monkeypatch):
    api_id, _ = bots
    asyncio.run(monitor.poll_live())
    real = httpx.AsyncClient
    monkeypatch.setattr(botmonitor.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(502)), **kw))
    asyncio.run(monitor.poll_live())
    assert len(monitor.live[api_id]["trades"]) == 3 and monitor.live[api_id]["error"]
    with SessionLocal() as s:
        assert perf.live_trades([s.get(Bot, api_id)])["stale"] == ["LiveAPI"]


def test_live_tab_and_partial(bots):
    api_id, _ = bots
    asyncio.run(monitor.poll_live())
    c = TestClient(app)
    page = c.get(f"/?lang=en&scope=b{api_id}").text
    assert 'data-tab="live"' in page and "Open trades right now" in page and "SSV/USDT" in page
    part = c.get(f"/live?lang=en&scope=b{api_id}&market=long")
    assert part.status_code == 200 and "<html" not in part.text
    assert "BTC/USDT" in part.text and "SSV/USDT" not in part.text and 'data-count="1"' in part.text
