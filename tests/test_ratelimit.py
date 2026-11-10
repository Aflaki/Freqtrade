"""The shared request budget: exact weights from a real ccxt client, the exchange's own count, priorities,
429 pauses, 418 bans (with one alert), and the admin card."""
import asyncio
import time

import ccxt.async_support as ccxt
import pytest
from ccxt.async_support.binanceusdm import binanceusdm as RealBinanceUSDM  # other tests swap ccxt.binanceusdm

from app.engine import ratelimit
from app.engine.ratelimit import ApiBanned, ApiBusy, Governor


def client(gov, answers):
    """A real binanceusdm client whose HTTP layer is replaced: each request pops the next answer."""
    ex = RealBinanceUSDM({"apiKey": "k", "secret": "s", "enableRateLimit": True})
    ex.rateLimit = 1  # no ccxt-side spacing in tests

    async def fake_fetch(url, method="GET", headers=None, body=None):
        status, hdrs, data = answers.pop(0)
        ex.last_response_headers = hdrs
        if status == 429:
            raise ccxt.RateLimitExceeded("binanceusdm 429 Too Many Requests")
        if status == 418:
            raise ccxt.DDoSProtection("binanceusdm 418 I'm a teapot: IP banned")
        return data
    ex.fetch = fake_fetch
    gov.attach(ex)
    return ex


def test_weights_server_count_and_bans():
    gov = Governor()
    alerts = []
    gov._alert = lambda gid, until: alerts.append(gid)
    ex = client(gov, [(200, {"X-MBX-USED-WEIGHT-1M": "1234"}, []), (429, {"Retry-After": "7"}, None),
                      (418, {"Retry-After": "120"}, None)])

    async def go():
        await ex.fapiprivate_get_usertrades({"symbol": "BTCUSDT"})  # Binance counts 5 for this endpoint
        g = gov.group("binanceusdm")
        assert sum(w for _, w, _ in g.calls) == 5 and g.server_used == 1234 and g.used(time.time()) == 1234
        with pytest.raises(ccxt.RateLimitExceeded):
            await ex.fapiprivate_get_usertrades({"symbol": "BTCUSDT"})
        assert g.paused_until > time.time() + 5  # waits as long as the exchange asked
        g.paused_until = 0
        with pytest.raises(ccxt.DDoSProtection):
            await ex.fapiprivate_get_usertrades({"symbol": "BTCUSDT"})
        with pytest.raises(ApiBanned):  # nothing more is sent until the ban ends
            await ex.fapiprivate_get_usertrades({"symbol": "BTCUSDT"})
        assert alerts == ["binanceusdm"]  # alerted once
        await ex.close()
    asyncio.run(go())


def test_priorities():
    gov = Governor()
    gov.budget_pct = 100
    g = gov.group("binanceusdm")
    g.limit = 100

    async def go():
        await gov.acquire("binanceusdm", 55, "trade")  # 55 used: above the view ceiling (60), below trade (100)
        with pytest.raises(ApiBusy):
            await gov.acquire("binanceusdm", 10, "view")  # pages show the cache instead
        await gov.acquire("binanceusdm", 10, "trade")  # trading still goes
        with ratelimit.use("background"):
            with pytest.raises(ApiBusy):
                old = ratelimit.MAX_WAIT["background"]
                ratelimit.MAX_WAIT["background"] = 0.2
                try:
                    await gov.acquire("binanceusdm", 5)  # waits briefly, then gives up (tries again later)
                finally:
                    ratelimit.MAX_WAIT["background"] = old
        snap = gov.snapshot()[0]
        assert snap["per_kind"] == {"trade": 65} and snap["skipped"] == {"view": 1, "background": 1}
        assert snap["ceilings"]["view"] == 60
    asyncio.run(go())


def test_admin_card(monkeypatch):
    from tests.test_watchdog import _admin_client
    from app.db import SessionLocal, User, get_global, init_db
    init_db()
    admin, uid = _admin_client()
    try:
        page = admin.get("/admin/servers").text
        assert 'id="api"' in page and "Exchange API load" in page
        import re
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        admin.post("/admin/servers/api-budget", data={"csrf": csrf, "pct": "70"})
        with SessionLocal() as s:
            assert get_global(s).api_budget_pct == 70
        assert ratelimit.governor.budget_pct == 70
        assert isinstance(admin.get("/admin/servers/api.json").json(), list)
    finally:
        ratelimit.governor.budget_pct = 80
        with SessionLocal() as s:
            get_global(s).api_budget_pct = 80
            s.query(User).filter_by(id=uid).delete()
            s.commit()
