import asyncio
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from tests import fake_exchange as fx

import app.engine.exchange as exmod
from app.db import (Bot, BotTrade, ExchangeAccount, ExecutionLog, NotificationLog, SessionLocal, Trade, User,
                    WebhookEvent, get_global)
from app.main import app
from app.telegram import bot


@pytest.fixture(scope="module")
def sent():
    msgs = []
    real_send = bot.send

    async def fake_send(chat_id, text, user_id=None, kind="reply"):
        msgs.append((str(chat_id), text))
        bot.token = "x"
        return await real_send.__func__(type("B", (), {"enabled": False, "_client": None, "_url": None})(), chat_id,
                                        text, user_id, kind) or True  # still writes NotificationLog

    async def no_configure(token):
        bot.token, bot.username = "test", "TestRelayBot"

    bot.configure = no_configure
    bot.send = fake_send
    for name in ("binanceusdm", "binance", "bybit"):
        setattr(exmod.ccxt, name, fx.FakeExchange)
    return msgs


def csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def register(client, email, name):
    page = client.get("/register").text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    ft = re.search(r'name="ft" value="([^"]+)"', page).group(1)
    return client.post("/register", data={"csrf": tok, "ft": ft, "email": email, "name": name,
                                          "password": "Tr4de-Relay-x9", "password2": "Tr4de-Relay-x9", "terms": "on"},
                       follow_redirects=False)


def secret(bot_id=1):
    with SessionLocal() as s:
        return s.get(Bot, bot_id).webhook_secret


def ft(client, bot_id=1, **p):
    base = {"pair": "ETH/USDT:USDT", "direction": "Short", "leverage": "5.0", "order_rate": "3000.0",
            "open_rate": "3000.0", "trade_id": "11", "enter_tag": "rsi_x", "open_date": "2026-11-01 10:00:00+00:00"}
    r = client.post(f"/webhook/{secret(bot_id)}?wait=1", json={**base, **p})
    assert r.status_code == 200, r.text
    return r.json()


def uid_of(email):
    with SessionLocal() as s:
        return s.query(User).filter_by(email=email).one().id


def test_full_flow(sent):
    admin, ali, sara = TestClient(app), TestClient(app), TestClient(app)
    with admin, ali, sara:
        # ---------------- admin sets the formula from the panel
        assert register(admin, "admin@x.com", "Admin").status_code == 303
        with SessionLocal() as s:
            s.query(User).filter_by(email="admin@x.com").update({"telegram_chat_id": "999", "lang": "en"})
            s.commit()
        tok = csrf(admin, "/admin/settings")
        r = admin.post("/admin/settings", data={
            "csrf": tok, "section": "trading", "entries_enabled": "on", "tradable_pct": "90",
            "mode__equal_split": "on", "mode__pair_weights": "on", "mode__custom_amounts": "on",
            "default_max_open_trades": "3", "max_leverage": "10", "min_capital": "100", "max_stake_per_trade": "0",
            "default_stoploss_pct": "0", "stake_currency": "USDT", "ex__binance": "on", "ex__bybit": "on",
            "allowed_pairs": "BTC/USDT\nETHUSDT\nSOL/USDT\n"}, follow_redirects=False)
        assert r.status_code == 303
        admin.post("/admin/settings", data={"csrf": tok, "section": "general", "site_name": "AlphaBot",
                                            "default_lang": "fa", "base_url": "https://bot.example.com",
                                            "public_performance": "on", "signal_only_enabled": "on",
                                            "require_approval": "on", "trial_days": "30", "daily_report_hour_utc": "20",
                                            "hero_title__en": "Custom EN title"})
        with SessionLocal() as s:
            gs = get_global(s)
            assert gs.allowed_pairs == ["BTC/USDT", "ETH/USDT", "SOL/USDT"] and gs.site_name == "AlphaBot"

        # ---------------- ali: auto-trading with custom amounts per coin + exchange stop loss
        register(ali, "ali@x.com", "Ali")
        ali_id = uid_of("ali@x.com")
        with SessionLocal() as s:
            assert s.get(User, ali_id).subscription_until is not None  # 30-day trial
        tok = csrf(admin, "/admin")
        admin.post(f"/admin/users/{ali_id}", data={"csrf": tok, "action": "approve"})
        tok = csrf(ali, "/accounts/new")
        r = ali.post("/accounts/new", data={
            "csrf": tok, "kind": "exchange", "label": "Binance Main", "exchange": "binance", "api_key": "KEY1234567890",
            "secret": "SECRET1234567890", "no_withdraw": "on", "trading_mode": "futures", "leverage": "5",
            "margin_mode": "isolated", "sizing_mode": "custom_amounts",
            "pair__ETH/USDT": "on", "value__ETH/USDT": "200", "pair__BTC/USDT": "on", "value__BTC/USDT": "100", "bot__1": "on",
            "stoploss_pct": "20"}, follow_redirects=False)
        assert r.headers["location"] == "/accounts", r.headers
        with SessionLocal() as s:
            acc = s.query(ExchangeAccount).filter_by(user_id=ali_id).one()
            assert acc.pairs == {"ETH/USDT": 200.0, "BTC/USDT": 100.0} and acc.capital == 300
            acc.dry_run = False  # real-order path against the fake exchange
            s.query(User).filter_by(id=ali_id).update({"telegram_chat_id": "111", "lang": "en"})
            s.commit()

        # ---------------- sara: signals only, no API, Persian
        register(sara, "sara@x.com", "Sara")
        sara_id = uid_of("sara@x.com")
        admin.post(f"/admin/users/{sara_id}", data={"csrf": csrf(admin, "/admin"), "action": "approve"})
        tok = csrf(sara, "/accounts/new?kind=signal")
        r = sara.post("/accounts/new", data={
            "csrf": tok, "kind": "signal", "label": "My signals", "trading_mode": "futures", "leverage": "3",
            "sizing_mode": "pair_weights", "capital": "1000", "pair__ETH/USDT": "on", "value__ETH/USDT": "3", "bot__1": "on",
            "pair__SOL/USDT": "on", "value__SOL/USDT": "1"}, follow_redirects=False)
        assert r.headers["location"] == "/accounts"
        with SessionLocal() as s:
            s.query(User).filter_by(id=sara_id).update({"telegram_chat_id": "222", "lang": "fa"})
            s.commit()

        # ---------------- Freqtrade: short ETH
        res = ft(ali, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False")
        statuses = sorted(r["status"] for r in res["results"])
        assert statuses == ["opened", "opened"], res
        with SessionLocal() as s:
            t_ali = s.query(Trade).filter_by(user_id=ali_id).one()
            t_sara = s.query(Trade).filter_by(user_id=sara_id).one()
            # ali: 200 margin * 5x / 3000 = 0.333 ETH ; stop at +4% (20% / 5x)
            assert t_ali.amount == 0.333 and abs(t_ali.sl_price - 3120) < 1e-6 and t_ali.sl_order_id
            # sara: 1000 * 3/4 = 750 margin (the 90% is now only a cap on the wallet share), signal price
            assert abs(t_sara.stake - 750) < 1e-6 and t_sara.is_signal and t_sara.leverage == 5  # the bot's own 5x, not the portfolio's 3x
        ex = [e for e in fx.FakeExchange.instances if e.orders][-1]
        assert ex.stops[-1]["params"] == {"stopLossPrice": 3120.0, "reduceOnly": True}

        # ---------------- partial exit 50% -> stop re-placed for the rest
        fx.PRICES["ETH/USDT:USDT"] = 2900.0
        res = ft(ali, type="exit_fill", amount="0.5", stake_amount="300.0", sub_trade="True", is_final_exit="False",
                 order_rate="2900", exit_reason="tp1")
        assert sorted(r["status"] for r in res["results"]) == ["partial", "partial"], res
        assert len(ex.cancelled) == 1 and ex.stops[-1]["amount"] == pytest.approx(0.167)

        # ---------------- final exit
        fx.PRICES["ETH/USDT:USDT"] = 2800.0
        res = ft(ali, type="exit_fill", amount="0.5", stake_amount="300.0", sub_trade="False", is_final_exit="True",
                 order_rate="2800", exit_reason="roi", profit_ratio="0.3", final_profit_ratio="0.2833", cumulative_profit="170.0",
                 close_rate="2800", close_date="2026-11-02 12:00:00+00:00")
        assert sorted(r["status"] for r in res["results"]) == ["closed", "closed"], res
        with SessionLocal() as s:
            assert all(t.status == "closed" and t.realized_pnl > 0 for t in s.query(Trade))
            bt = s.query(BotTrade).filter_by(ft_trade_id=11).one()
            assert not bt.is_open and bt.profit_ratio == pytest.approx(0.2833) and bt.side == "short"
        assert len(ex.cancelled) == 2  # stop cancelled before the final exit

        # ---------------- public performance page (all languages, both markets)
        for lang in ("fa", "en", "de", "es"):
            page = TestClient(app).get(f"/?lang={lang}&days=all").text
            assert "ETH/USDT" in page and 'dir="' + ("rtl" if lang == "fa" else "ltr") + '"' in page
        assert "Custom EN title" in TestClient(app).get("/?lang=en").text
        page = TestClient(app).get("/?lang=en&days=all").text
        chips = page.split('class="fb-chips"', 1)[1].split("</div>", 1)[0]
        assert "Long &amp; short" in chips and "All time" in chips and "fb-dates" in chips  # visible in compact bar
        assert "+28.33%" in page and "+170.00" in page and "5.0x" in page  # exactly as Freqtrade reports, with leverage
        assert "ETH/USDT" in TestClient(app).get("/?lang=en&days=all&market=short").text
        assert "ETH/USDT" not in TestClient(app).get("/?lang=en&days=all&market=long").text.split("perf_recent")[-1]

        # ---------------- manual close from the web + logs
        res = ft(ali, type="entry_fill", pair="BTC/USDT:USDT", direction="Long", order_rate="60000",
                 open_rate="60000", amount="0.01", stake_amount="120", sub_trade="False", trade_id="12")
        assert [r["status"] for r in res["results"]] == ["opened"]  # sara did not pick BTC
        with SessionLocal() as s:
            tid = s.query(Trade).filter_by(user_id=ali_id, status="open").one().id
        assert "/trades/close-all" not in ali.get("/dashboard?scope=all").text  # no close buttons without the permission
        r = ali.post(f"/trades/{tid}/close", data={"csrf": csrf(ali, "/dashboard")}, follow_redirects=False)
        assert r.status_code == 403
        with SessionLocal() as s:
            s.get(User, ali_id).can_close = True  # the admin allows it
            s.commit()
        assert "/trades/close-all" in ali.get("/dashboard?scope=all").text
        r = ali.post(f"/trades/{tid}/close", data={"csrf": csrf(ali, "/dashboard")}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as s:
            tr = s.get(Trade, tid)
            assert tr.status == "closed" and tr.exit_reason == "manual"
            assert s.query(WebhookEvent).filter_by(status="accepted").count() == 4
            ex_logs = s.query(ExecutionLog).filter_by(user_id=ali_id).all()
            assert any(c["method"] == "create_order" for e in ex_logs for c in e.calls)
            assert any(c["method"] == "create_order(stop)" for e in ex_logs for c in e.calls)
            assert s.query(NotificationLog).filter_by(user_id=sara_id).count() >= 3

        # ---------------- subscription expired -> no new entries for that user
        with SessionLocal() as s:
            s.query(User).filter_by(id=ali_id).update({"subscription_until": dt.datetime(2020, 1, 1)})
            s.commit()
        res = ft(ali, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False", trade_id="13")
        assert [r["status"] for r in res["results"]] == ["opened"]  # only sara

        with SessionLocal() as s:
            trades_before = s.query(Trade).count()
        # ---------------- rejected / ignored webhooks are logged too
        assert TestClient(app).post("/webhook/wrong", json={"type": "entry"}).status_code == 401
        assert TestClient(app).post(f"/webhook/{secret()}", json={"type": "new_candle", "data": "x"}).json()["status"] == "ignored"
        with SessionLocal() as s:
            assert s.query(WebhookEvent).filter_by(status="rejected").count() == 1
            assert s.query(WebhookEvent).filter_by(status="ignored").count() == 1

        # ---------------- every page renders in every language
        for lang in ("fa", "en", "de", "es"):
            for c, urls in ((ali, ("/dashboard", "/accounts", "/accounts/new", "/accounts/new?kind=signal", "/trades", "/compare",
                                   "/activity", "/settings")),
                            (admin, ("/admin", f"/admin/users/{ali_id}", "/admin/settings", "/admin/performance",
                                     "/admin/logs", "/admin/logs?kind=executions", "/admin/logs?kind=notifications",
                                     "/admin/trades"))):
                c.get(f"/lang/{lang}")
                for u in urls:
                    r = c.get(u)
                    assert r.status_code == 200, (lang, u, r.text[:300])
                    assert "nav_" not in r.text and "col_" not in r.text, (lang, u)  # no untranslated keys
        assert ali.get("/admin").status_code == 403

        # ---------------- Freqtrade status / warnings / errors: logged + sent to admins, never traded
        admin.get("/lang/en")
        for p in ({"type": "startup", "status": "*Exchange:* `binance`\n*Strategy:* `MyStrat`"},
                  {"type": "status", "status": "running"},
                  {"type": "exception", "status": "*OperationalException:*\n```\nboom\n```"},
                  {"type": "protection_trigger", "pair": "ETH/USDT:USDT", "reason": "StoplossGuard",
                   "lock_end_time": "2026-11-08 12:00:00", "side": "*"}):
            r = TestClient(app).post(f"/webhook/{secret()}", json=p).json()
            assert r["status"] == "event", r
        from app.engine.botmonitor import monitor
        with SessionLocal() as s:
            assert s.get(Bot, 1).st["webhook_state"] == "running"
        with SessionLocal() as s:
            assert s.query(WebhookEvent).filter_by(status="event").count() == 4
            assert s.query(Trade).count() == trades_before  # nothing traded

        # ---------------- Freqtrade REST API monitor (fake API) + admin bot page
        import httpx
        calls = []

        def fake_api(request):
            calls.append(request.url.path)
            assert request.headers["authorization"].startswith("Basic ")
            data = {"/api/v1/show_config": {"state": "running", "strategy": "MyStrat", "exchange": "binance",
                                            "dry_run": True, "trading_mode": "futures", "max_open_trades": -1,
                                            "stake_currency": "USDT", "bot_name": "ft"},
                    "/api/v1/health": {"last_process": "2020-01-01T00:00:00+00:00"},
                    "/api/v1/count": {"current": 2, "max": 5, "total_stake": 100},
                    "/api/v1/whitelist": {"whitelist": ["BTC/USDT:USDT", "DOGE/USDT:USDT"]},
                    "/api/v1/stop": {"status": "stopping trader ..."},
                    "/api/v1/trades": {"trades": [{"trade_id": 50, "pair": "SOL/USDT:USDT", "is_short": False,
                                                   "leverage": 3.0, "open_date": "2026-11-05 10:00:00+00:00",
                                                   "close_date": "2026-11-05 14:00:00+00:00", "open_rate": 150,
                                                   "close_rate": 156, "is_open": False, "profit_ratio": 0.1185,
                                                   "close_profit": 0.1185, "close_profit_abs": 11.85,
                                                   "stake_amount": 100, "exit_reason": "roi"}]}}.get(request.url.path)
            return httpx.Response(200, json=data) if data else httpx.Response(404)

        real_client = httpx.AsyncClient
        import app.engine.botmonitor as bm
        bm.httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(fake_api), **kw)
        try:
            admin.post("/admin/bots/1", data={"csrf": csrf(admin, "/admin/bots/1"), "action": "save", "name": "Main",
                                              "api_url": "65.109.188.248:8015", "api_user": "u", "api_pass": "p",
                                              "enabled": "on", "copy_enabled": "on", "public": "on", "sort": "1"})
            with SessionLocal() as s:
                assert s.get(Bot, 1).api_url == "http://65.109.188.248:8015"
            with SessionLocal() as s:  # saving already polled silently; start from a blank state to see alerts
                s.get(Bot, 1).state = {}
                s.commit()
            st = asyncio.run(monitor.poll_bot(1))
            assert st["api"] == "ok" and st["state"] == "running" and st["stalled"] is True, st
            assert st["missing_pairs"] == ["DOGE/USDT"] and st["max_open_trades"] == -1
            assert st["history_synced"] and "/api/v1/trades" in calls  # closed trades pulled from the API
            with SessionLocal() as s:
                bt = s.query(BotTrade).filter_by(bot_id=1, ft_trade_id=50).one()
                assert (bt.leverage, bt.profit_ratio, bt.profit_abs, bt.source) == (3.0, 0.1185, 11.85, "api")
            page = admin.get("/admin/bot").text
            assert "MyStrat" in page and "Main" in page
            page = admin.get("/admin/bots/1").text
            assert "MyStrat" in page and "DOGE/USDT" in page
            r = admin.post("/admin/bots/1", data={"csrf": csrf(admin, "/admin/bots/1"), "action": "stop"},
                           follow_redirects=False)
            assert r.status_code == 303 and "/api/v1/stop" in calls
        finally:
            bm.httpx.AsyncClient = real_client
        for lang in ("fa", "en", "de", "es"):
            admin.get(f"/lang/{lang}")
            assert admin.get("/admin/bot").status_code == 200
            assert admin.get("/admin/bots/1").status_code == 200

        # ---------------- a second Freqtrade bot (bulk add): own webhook URL, own trade ids
        admin.get("/lang/en")
        r = admin.post("/admin/bot", data={"csrf": csrf(admin, "/admin/bot"), "action": "bulk",
                                           "lines": "Scalper | | |\n# comment line\n"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as s:
            bot2 = s.query(Bot).filter_by(name="Scalper").one().id
            n_trades = s.query(Trade).count()
        res = ft(admin, bot_id=bot2, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False",
                 trade_id="11")  # same Freqtrade trade id as bot 1 used
        assert res["bot"] == "Scalper" and res["results"] == []  # no portfolio follows bot 2
        with SessionLocal() as s:
            assert s.query(BotTrade).filter_by(ft_trade_id=11).count() == 2  # one per bot
            assert s.query(Trade).count() == n_trades
            ev = s.query(WebhookEvent).order_by(WebhookEvent.id.desc()).first()
            assert ev.bot_id == bot2
        assert "Scalper" in TestClient(app).get("/?lang=en").text  # bot cards on the home page
        assert TestClient(app).get(f"/?lang=en&bot={bot2}").status_code == 200

        # ---------------- groups: create "V1", move both bots into it (bulk)
        admin.post("/admin/bot", data={"csrf": csrf(admin, "/admin/bot"), "action": "group_add", "group_name": "V1"})
        from app.db import BotGroup
        with SessionLocal() as s:
            gid = s.query(BotGroup).filter_by(name="V1").one().id
        admin.post("/admin/bot", data={"csrf": csrf(admin, "/admin/bot"), "action": "move", "target_group": str(gid),
                                       "sel__1": "on", f"sel__{bot2}": "on"})
        with SessionLocal() as s:
            assert {b.group_id for b in s.query(Bot)} == {gid}
        page = TestClient(app).get(f"/?lang=en&group={gid}&days=all").text
        assert "📦 V1" in page and "ETH/USDT" in page

        # ---------------- blacklist ETH on bot 1: hidden from its results, new ETH entries not copied
        tok = csrf(admin, "/admin/bots/1")
        admin.post("/admin/bots/1", data={"csrf": tok, "action": "save", "name": "Main", "api_url": "", "enabled": "on",
                                          "copy_enabled": "on", "public": "on", "sort": "1", "group_id": str(gid),
                                          "pair_filter_mode": "blacklist", "pair_filter": "ethusdt"})
        with SessionLocal() as s:
            b1 = s.get(Bot, 1)
            assert (b1.pair_filter_mode, b1.pair_filter) == ("blacklist", ["ETH/USDT"])
        from app.engine import performance as perf
        assert "ETH/USDT" not in perf.pair_stats("all", None, [1])
        res = ft(admin, type="entry_fill", amount="1.0", stake_amount="600.0", sub_trade="False", trade_id="20")
        assert res["results"] == []  # filtered: sara follows ETH from bot 1 but gets nothing

        # ---------------- per-pair bot choice: sara takes SOL only from bot 2
        with SessionLocal() as s:
            acc = s.query(ExchangeAccount).filter_by(user_id=sara_id).one()
            acc.bots, acc.pair_bot = [1, bot2], {"SOL/USDT": bot2}
            s.commit()
        sol = dict(pair="SOL/USDT:USDT", direction="Long", order_rate="150", open_rate="150", amount="1",
                   stake_amount="50", sub_trade="False")
        assert ft(admin, bot_id=1, type="entry_fill", trade_id="21", **sol)["results"] == []
        res = ft(admin, bot_id=bot2, type="entry_fill", trade_id="22", **sol)
        assert [r["status"] for r in res["results"]] == ["opened"]
        with SessionLocal() as s:
            assert s.query(Trade).filter_by(user_id=sara_id, pair="SOL/USDT", status="open").one().bot_id == bot2

        # ---------------- comparison page + matrix, every language
        for lang in ("fa", "en", "de", "es"):
            r = TestClient(app).get(f"/compare?lang={lang}&days=all")
            assert r.status_code == 200 and "nav_" not in r.text and "compare_" not in r.text
            r = TestClient(app).get(f"/compare?lang={lang}&group={gid}&pair=ETH/USDT&days=all")
            assert r.status_code == 200 and "Scalper" in r.text

        # ---------------- telegram commands in the user's language
        asyncio.run(bot.handle("222", "/profit"))
        asyncio.run(bot.handle("111", "/lang de"))
        asyncio.run(bot.handle("111", "/daily 3"))
        with SessionLocal() as s:
            assert s.get(User, ali_id).lang == "de"

    admin_msgs = [t for c, t in sent if c == "999"]
    assert any("🚀" in t and "<code>binance</code>" in t for t in admin_msgs)      # startup forwarded, markdown converted
    assert any("🛑" in t and "<pre>boom</pre>" in t for t in admin_msgs)          # exception
    assert any("Protection lock" in t for t in admin_msgs)
    assert any("Bot entry" in t and "Copied to portfolios" in t for t in admin_msgs)  # trade + copy summary
    assert any("[Scalper]" in t and "no portfolio holds this pair" in t for t in admin_msgs)
    assert any("not allowed on the site" in t and "DOGE/USDT" in t for t in admin_msgs)
    assert any("has not processed" in t for t in admin_msgs)                     # stalled heartbeat alert
    ali_msgs = [t for c, t in sent if c == "111"]
    sara_msgs = [t for c, t in sent if c == "222"]
    assert any("Position opened" in t for t in ali_msgs) and any("Stop loss" in t for t in ali_msgs)
    assert any("Partial exit" in t for t in ali_msgs)
    assert any("سیگنال ورود" in t for t in sara_msgs) and any("سیگنال خروج" in t for t in sara_msgs)
    assert any("مبلغ پیشنهادی" in t for t in sara_msgs)
    assert any("G&V der letzten 3 Tage" in t for t in ali_msgs)
    print("\n---- ali (en)\n" + "\n\n".join(ali_msgs[:4]) + "\n---- sara (fa)\n" + "\n\n".join(sara_msgs[:4]))
