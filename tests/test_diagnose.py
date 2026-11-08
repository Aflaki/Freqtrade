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
