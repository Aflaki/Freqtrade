import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from tests import fake_exchange as fx

import app.engine.exchange as exmod
from app.db import ExchangeAccount, SessionLocal, Trade, User
from app.main import app
from app.telegram import bot

SECRET = "webhook-secret-123456"


@pytest.fixture(scope="module")
def sent():
    msgs = []

    async def fake_send(chat_id, text):
        msgs.append((str(chat_id), text))
        return True

    bot.token = "test"  # enables notify()
    bot.username = "TestRelayBot"
    bot.send = fake_send
    bot.start = lambda: asyncio.sleep(0)
    for name in ("binanceusdm", "binance", "bybit"):
        setattr(exmod.ccxt, name, fx.FakeExchange)
    return msgs


def csrf(client, url):
    html = client.get(url).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def register(client, email, name):
    tok = csrf(client, "/register")
    return client.post("/register", data={"csrf": tok, "email": email, "name": name, "password": "password123",
                                          "password2": "password123", "terms": "on"}, follow_redirects=False)


def ft(client, **p):
    base = {"pair": "ETH/USDT:USDT", "direction": "Short", "leverage": "5.0", "order_rate": "3000.0",
            "open_rate": "3000.0", "trade_id": "11", "enter_tag": "rsi_x"}
    r = client.post(f"/webhook/{SECRET}?wait=1", json={**base, **p})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow(sent):
    admin = TestClient(app)
    user = TestClient(app)
    with admin, user:
        # ---- first user becomes admin
        assert register(admin, "admin@x.com", "Admin").status_code == 303
        tok = csrf(admin, "/admin/settings")
        r = admin.post("/admin/settings", data={
            "csrf": tok, "entries_enabled": "on", "require_approval": "on", "sizing_formula": "equal_split",
            "tradable_pct": "90", "default_max_open_trades": "3", "max_leverage": "10", "min_capital": "100",
            "max_stake_per_trade": "0", "stake_currency": "USDT", "ex__binance": "on", "ex__bybit": "on",
            "allowed_pairs": "BTC/USDT\nETHUSDT\n"}, follow_redirects=False)
        assert r.status_code == 303

        # ---- second user is pending until approved
        register(user, "ali@x.com", "Ali")
        assert "در انتظار تأیید" in user.get("/").text
        with SessionLocal() as s:
            uid = s.query(User).filter_by(email="ali@x.com").one().id
        tok = csrf(admin, "/admin")
        admin.post(f"/admin/users/{uid}", data={"csrf": tok, "action": "approve"})

        # ---- add a futures account (real-order path against the fake exchange)
        tok = csrf(user, "/accounts/new")
        r = user.post("/accounts/new", data={
            "csrf": tok, "label": "Binance Main", "exchange": "binance", "api_key": "KEY1234567890",
            "secret": "SECRET1234567890", "no_withdraw": "on", "trading_mode": "futures", "leverage": "5",
            "margin_mode": "isolated", "capital": "1000", "pair__BTC/USDT": "on", "pair__ETH/USDT": "on"},
            follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/accounts"
        with SessionLocal() as s:
            acc = s.query(ExchangeAccount).one()
            assert acc.api_key_enc != "KEY1234567890"  # encrypted at rest
            assert acc.pairs == {"BTC/USDT": 1.0, "ETH/USDT": 1.0}
            s.query(User).filter_by(id=uid).update({"telegram_chat_id": "555"})
            s.commit()
        page = user.get("/accounts").text
        assert "KEY1…7890" in page and "300.00" in page  # 1000 * 90% / 3 trades

        # ---- Freqtrade opens a short on ETH
        res = ft(user, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False")
        assert res["results"][0]["status"] == "opened", res
        with SessionLocal() as s:
            t = s.query(Trade).one()
            assert (t.side, t.symbol, t.amount, t.entry_price) == ("short", "ETH/USDT:USDT", 0.5, 3000.0)
            assert abs(t.stake - 300) < 1e-6 and abs(t.scale - 0.5) < 1e-6
        ex = fx.FakeExchange.instances[-1]
        assert ex.leverage["ETH/USDT:USDT"] == 5 and ex.orders[-1]["side"] == "sell"

        # duplicate delivery is ignored
        assert ft(user, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False")["status"] == "duplicate"

        # ---- DCA: Freqtrade adds 0.5 ETH (300 margin) -> we add half of that
        res = ft(user, type="entry_fill", amount="0.5", stake_amount="900.0", sub_trade="True", order_rate="2900.0")
        assert res["results"][0]["status"] == "added", res
        with SessionLocal() as s:
            # FT order margin 0.5*2900/5=290 -> ours 145 -> 145*5/3000 (live price) = 0.241
            assert abs(s.query(Trade).one().amount - 0.741) < 1e-9

        # ---- partial exit 1/3 (Freqtrade closed 0.5 of 1.5)
        fx.PRICES["ETH/USDT:USDT"] = 2800.0
        res = ft(user, type="exit_fill", amount="0.5", stake_amount="580.0", sub_trade="True",
                 is_final_exit="False", open_rate="2966.6667", exit_reason="partial_tp")
        assert res["results"][0]["status"] == "partial", res
        assert ex.orders[-1]["params"] == {"reduceOnly": True} and ex.orders[-1]["side"] == "buy"

        # ---- final exit
        fx.PRICES["ETH/USDT:USDT"] = 2700.0
        res = ft(user, type="exit_fill", amount="1.0", stake_amount="580.0", sub_trade="False",
                 is_final_exit="True", exit_reason="roi")
        assert res["results"][0]["status"] == "closed", res
        with SessionLocal() as s:
            t = s.query(Trade).one()
            assert t.status == "closed" and t.amount == 0 and t.realized_pnl > 0 and t.exit_reason == "roi"

        # ---- spot account: fee taken in base is respected on exit
        tok = csrf(user, "/accounts/new")
        user.post("/accounts/new", data={
            "csrf": tok, "label": "Bybit Spot", "exchange": "bybit", "api_key": "K2xxxxxxxxxx", "secret": "S2xxxxxxxxxx",
            "no_withdraw": "on", "trading_mode": "spot", "capital": "500", "pair__BTC/USDT": "on"})
        res = ft(user, type="entry_fill", pair="BTC/USDT", direction="Long", leverage="1.0", order_rate="60000",
                 open_rate="60000", amount="0.01", stake_amount="600", sub_trade="False", trade_id="12")
        assert res["results"][0]["status"] == "opened", res
        res = ft(user, type="exit_fill", pair="BTC/USDT", direction="Long", leverage="1.0", order_rate="61000",
                 open_rate="60000", amount="0.01", stake_amount="600", sub_trade="False", is_final_exit="True",
                 trade_id="12", exit_reason="exit_signal")
        assert res["results"][0]["status"] == "closed", res

        # ---- spot short is ignored, unknown pair is ignored, status messages are acked
        assert ft(user, type="entry_fill", pair="DOGE/USDT:USDT", amount="10", stake_amount="10")["results"] == []
        assert user.post(f"/webhook/{SECRET}", json={"type": "status", "status": "running"}).json()["status"] == "ignored"
        assert user.post("/webhook/wrong", json={}).status_code == 401

        # ---- pages render
        for url in ("/", "/accounts", "/trades", "/settings"):
            assert user.get(url).status_code == 200, url
        for url in ("/admin", "/admin/settings", "/admin/trades"):
            assert admin.get(url).status_code == 200, url
        assert user.get("/admin").status_code == 403

        # ---- telegram commands
        asyncio.run(bot.handle("555", "/profit"))
        asyncio.run(bot.handle("555", "/pause"))
        with SessionLocal() as s:
            assert s.get(User, uid).trading_enabled is False
        # paused user gets no new entries
        res = ft(user, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False", trade_id="13")
        assert res["results"] == []

    texts = [t for c, t in sent if c == "555"]
    assert any("پوزیشن باز شد" in t for t in texts)
    assert any("افزایش پوزیشن" in t for t in texts)
    assert any("خروج بخشی (" in t for t in texts)
    assert any("پوزیشن بسته شد" in t for t in texts)
    assert any("سود و زیان" in t for t in texts)
    print("\n\n".join(texts))
