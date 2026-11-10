"""Market widgets (Fear & Greed, altcoin season, market cap, futures stats) and coin/exchange icons."""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal, get_global, init_db
from app.engine import coingecko, marketdata
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
STATE = {"alt_fail": False}


def handler(request: httpx.Request) -> httpx.Response:
    u = str(request.url)
    if "alternative.me/fng" in u:
        return httpx.Response(200, json={"data": [{"value": "23", "value_classification": "Extreme Fear"},
                                                  {"value": "31", "value_classification": "Fear"}]})
    if "altcoin-season-index" in u:
        if STATE["alt_fail"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": {"altcoin_index": 47, "timestamp": "x"}, "status": {"error_code": "0"}})
    if u.endswith("/global"):
        return httpx.Response(200, json={"data": {"total_market_cap": {"usd": 3.21e12}, "total_volume": {"usd": 1.1e11},
                                                  "market_cap_change_percentage_24h_usd": -1.37,
                                                  "market_cap_percentage": {"btc": 58.4, "eth": 11.9}}})
    if "/fapi/v1/ticker/24hr" in u:
        return httpx.Response(200, json={"lastPrice": "101234.5", "priceChangePercent": "2.31"})
    if "/fapi/v1/premiumIndex" in u:
        return httpx.Response(200, json={"lastFundingRate": "0.00010000"})
    if "globalLongShortAccountRatio" in u:
        return httpx.Response(200, json=[{"longAccount": "0.6120", "shortAccount": "0.3880", "longShortRatio": "1.5773"}])
    if "/coins/markets" in u:
        page = request.url.params.get("page")
        rows = [{"symbol": "btc", "image": "https://img.example/coins/images/1/large/bitcoin.png"},
                {"symbol": "eth", "image": "https://img.example/coins/images/279/large/ethereum.png"}] if page == "1" else []
        return httpx.Response(200, json=rows)
    if "/exchanges" in u:
        return httpx.Response(200, json=[{"id": "binance", "image": "https://img.example/markets/binance.png"}])
    if "img.example" in u:
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    return httpx.Response(404)


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    init_db()
    real = httpx.AsyncClient
    for mod in (marketdata.httpx, coingecko.httpx):
        monkeypatch.setattr(mod, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(coingecko, "GAP_KEYLESS", 0)
    monkeypatch.setattr(marketdata, "ICON_DIR", tmp_path / "icons")
    monkeypatch.setattr(marketdata, "MAP_FILE", tmp_path / "icons" / "map.json")
    STATE["alt_fail"] = False
    yield
    with SessionLocal() as s:
        get_global(s).market_widgets = None
        s.commit()


def test_widgets_refresh_and_view(mocked):
    d = asyncio.run(marketdata.refresh_widgets())
    assert d["errors"] == {}
    assert d["fng"] == {"value": 23, "label": "Extreme Fear", "prev": 31, "source": "alternative.me"}
    assert d["alt"]["value"] == 47 and d["global"]["btc_dom"] == 58.4 and d["global"]["total_usd"] == 3.21e12
    assert d["fut"]["BTC"]["price"] == 101234.5 and abs(d["fut"]["BTC"]["funding"] - 0.01) < 1e-9
    assert abs(d["fut"]["long_pct"] - 61.2) < 1e-9
    v = marketdata.view()
    assert v["fng_band"] == "extreme_fear" and v["gauge"][0] < 60  # needle on the fear (left) side
    # a source fails later: its last good value stays, the error is recorded
    STATE["alt_fail"] = True
    d = asyncio.run(marketdata.refresh_widgets())
    assert d["alt"]["value"] == 47 and "alt" in d["errors"]


def test_altcoin_index_answer_shapes():
    f = marketdata._find_index
    assert f({"data": {"altcoin_index": 61}}) == 61
    assert f({"data": [{"timestamp": 1, "altcoin_index": "12"}, {"timestamp": 2, "altcoin_index": "33"}]}) == 33
    assert f({"data": {"points": [{"value": 70}]}}) == 70
    assert f({"data": {"nothing": "here"}}) is None


def test_home_page_shows_widgets(mocked):
    asyncio.run(marketdata.refresh_widgets())
    html = TestClient(app).get("/?lang=en").text
    assert "Fear &amp; Greed Index" in html and "Extreme fear" in html and "$3.21T" in html and "58.4%" in html
    assert "Altcoin Season Index" in html and "Long/short account ratio" in html and "alternative.me" in html


def test_icons(mocked):
    asyncio.run(marketdata.refresh_icon_map(pages=1))
    c = TestClient(app)
    r = c.get("/icon/coin/BTC")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert (marketdata.ICON_DIR / "coin" / "BTC.png").exists()  # cached on our server
    assert c.get("/icon/coin/btcusdt").content == PNG  # BTCUSDT -> BTC
    r = c.get("/icon/coin/NOPECOIN")
    assert r.headers["content-type"].startswith("image/svg+xml") and b"NOP" in r.content  # letter badge
    assert c.get("/icon/ex/binance").content == PNG
    assert c.get("/icon/other/x").status_code == 404
    assert '<img class="cicon" src="/icon/coin/ETH"' in str(__import__("app.web.common", fromlist=["x"])._coin_icon("ETH/USDT:USDT"))
