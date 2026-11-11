import asyncio

import httpx

import app.engine.botmonitor as bm


def _patch(handler):
    real = httpx.AsyncClient
    bm.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    return real


def _run(handler, coro_fn):
    real = _patch(handler)
    try:
        return asyncio.run(coro_fn())
    finally:
        bm.httpx.AsyncClient = real


def freqtrade(old_version=False, password="secret"):
    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/api/v1/ping":
            return httpx.Response(200, json={"status": "pong"})
        if request.headers.get("authorization") != httpx.BasicAuth("FreqTrader", password)._auth_header:
            return httpx.Response(401, json={"detail": "Unauthorized"})
        if path == "/api/v1/show_config":
            return httpx.Response(200, json={"state": "running", "strategy": "S", "exchange": "binance"})
        if old_version and path in ("/api/v1/health", "/api/v1/whitelist"):
            return httpx.Response(404)
        return httpx.Response(200, json={"current": 1, "whitelist": ["BTC/USDT:USDT"], "last_process": None})
    return handler


def test_ok_and_old_version():
    url = "65.109.188.248:8015/api/v1"  # pasted with path, without scheme
    steps = _run(freqtrade(), lambda: bm.diagnose(url, "FreqTrader", "secret"))
    assert all(s["ok"] for s in steps), steps
    info = _run(freqtrade(old_version=True), lambda: bm.fetch_info(url, "FreqTrader", "secret"))
    assert info["api"] == "ok" and info["strategy"] == "S" and info["whitelist"] == []  # optional parts may be missing
    steps = _run(freqtrade(old_version=True), lambda: bm.diagnose(url, "FreqTrader", "secret"))
    assert [s["hint"] for s in steps if not s["ok"]] == ["old_version", "old_version"]


def test_wrong_password_and_refused():
    steps = _run(freqtrade(), lambda: bm.diagnose("http://h:8015", "FreqTrader", "1234"))
    assert steps[-1]["step"] == "/show_config" and steps[-1]["hint"] == "auth"

    def refused(request):
        raise httpx.ConnectError("Connection refused")
    steps = _run(refused, lambda: bm.diagnose("http://h:8015", "u", "p"))
    assert steps[-1]["step"] == "reach" and steps[-1]["hint"] == "refused"


def test_polling_asks_only_what_changes():
    """Every request is work inside the bot (and /balance makes it call the exchange): the minute poll only checks
    that it is alive; configuration every 10 min, balance every 30 min, closed trades every 5 min."""
    import datetime as dt

    from app.db import Bot, BotTrade, SessionLocal, init_db, utcnow
    from app.security import encrypt
    init_db()
    with SessionLocal() as s:
        b = Bot(name="PollMe", webhook_secret="poll-me", api_url="http://h:8015", api_user="FreqTrader",
                api_pass_enc=encrypt("secret"))
        s.add(b)
        s.commit()
        bot_id = b.id
    calls = []
    inner = freqtrade()

    def handler(request):
        calls.append(request.url.path.replace("/api/v1", ""))
        if request.url.path == "/api/v1/trades":
            return httpx.Response(200, json={"trades": []})
        return inner(request)
    try:
        def poll(**kw):
            calls.clear()
            _run(handler, lambda: bm.monitor.poll_bot(bot_id, alert=False, **kw))
            return sorted(calls)
        assert poll() == sorted(["/show_config", "/health", "/count", "/whitelist", "/balance", "/trades"])  # first time
        assert poll() == ["/count", "/health"]  # a minute later: only "alive?"

        def age(**ago):
            with SessionLocal() as s:
                st = dict(s.get(Bot, bot_id).state)
                for k, sec in ago.items():
                    st[k] = (utcnow() - dt.timedelta(seconds=sec)).isoformat(timespec="seconds")
                s.get(Bot, bot_id).state = st
                s.commit()
        age(config_at=bm.CONFIG_EVERY + 5)
        assert poll() == sorted(["/show_config", "/health", "/count", "/whitelist"])  # config, but not the balance
        age(synced_at=bm.TRADES_EVERY + 5)
        assert poll() == ["/count", "/health", "/trades"]
        assert "/balance" in poll(force=True)  # after a start/stop from the panel: everything
        with SessionLocal() as s:
            st = s.get(Bot, bot_id).state
            assert st["api"] == "ok" and st["strategy"] == "S" and st["open_trades"] == 1  # kept between light polls
    finally:
        with SessionLocal() as s:
            s.query(BotTrade).filter_by(bot_id=bot_id).delete()
            s.query(Bot).filter_by(id=bot_id).delete()
            s.commit()
