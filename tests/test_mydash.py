"""The user's dashboard: the bots' performance view, built from the user's own trades, per portfolio / bot / side."""
import datetime as dt

import pytest

from tests.test_catalog import login

from app.db import Bot, ExchangeAccount, SessionLocal, Trade, User, init_db, utcnow
from app.engine import myperf


@pytest.fixture
def mine():
    init_db()
    c, csrf = login("dash-u@x.com")
    with SessionLocal() as s:
        u = s.query(User).filter_by(email="dash-u@x.com").one()
        b1, b2 = Bot(name="DashA", webhook_secret="dash-a"), Bot(name="DashB", webhook_secret="dash-b")
        s.add_all([b1, b2])
        s.commit()
        a1 = ExchangeAccount(user_id=u.id, kind="exchange", label="dash-main", exchange="binance", capital=1000,
                             pairs={"BTC/USDT": 1, "ETH/USDT": 1}, bots=[b1.id, b2.id], pair_bot={"BTC/USDT": b1.id, "ETH/USDT": b2.id})
        a2 = ExchangeAccount(user_id=u.id, kind="signal", label="dash-sig", capital=500, pairs={"BTC/USDT": 1}, bots=[b1.id])
        s.add_all([a1, a2])
        s.commit()
        now = utcnow()

        def closed(acc, bot, pair, side, pnl, stake=100, days=1):
            s.add(Trade(user_id=u.id, account_id=acc.id, bot_id=bot.id, pair=pair, symbol=pair + ":USDT", side=side,
                        status="closed", stake=stake, entry_price=100, exit_value=105, closed_amount=1, leverage=5,
                        realized_pnl=pnl, opened_at=now - dt.timedelta(days=days, hours=5),
                        closed_at=now - dt.timedelta(days=days)))
        closed(a1, b1, "BTC/USDT", "long", 20)
        closed(a1, b2, "ETH/USDT", "short", -5)
        closed(a2, b1, "BTC/USDT", "long", 10, days=200)
        s.add(Trade(user_id=u.id, account_id=a1.id, bot_id=b1.id, pair="BTC/USDT", symbol="BTC/USDT:USDT", side="long",
                    status="open", stake=100, entry_price=60000, amount=0.01, leverage=5))
        s.commit()
        ids = (u.id, a1.id, a2.id, b1.id, b2.id)
    yield c, ids
    with SessionLocal() as s:
        s.query(Trade).filter(Trade.user_id == ids[0]).delete()
        s.query(ExchangeAccount).filter(ExchangeAccount.user_id == ids[0]).delete()
        s.query(Bot).filter(Bot.id.in_(ids[3:])).delete(synchronize_session=False)
        s.commit()


def test_numbers_and_scopes(mine):
    c, (uid, a1, a2, b1, b2) = mine
    r = myperf.report(uid, "", "all", None)
    assert r["data"]["trades"] == 3 and r["data"]["profit_abs"] == 25 and r["data"]["capital"] == 1500
    assert r["data"]["return_pct"] == pytest.approx(25 / 1500 * 100)
    assert set(r["pairs"]) == {"BTC/USDT", "ETH/USDT"} and r["pairs"]["BTC/USDT"]["trades"] == 2
    assert r["acc_cards"][a1]["profit_abs"] == 15 and r["bot_cards"][b1]["trades"] == 2
    assert myperf.report(uid, f"a{a2}", "all", None)["data"]["trades"] == 1  # one portfolio
    assert myperf.report(uid, f"b{b2}", "all", None)["data"]["profit_abs"] == -5  # one bot
    assert myperf.report(uid, "", "short", None)["data"]["trades"] == 1  # one side
    assert myperf.report(uid, "", "all", 90)["data"]["trades"] == 2  # last 90 days
    assert myperf.report(uid, "a999999", "all", None)["scope"] == "all"  # someone else's id: ignored


def test_page(mine):
    c, (uid, a1, a2, b1, b2) = mine
    html = c.get("/dashboard").text  # closed cards: the summaries, no tables yet
    assert 'data-key="all"' in html and f'data-key="a{a1}"' in html and f'data-key="a{a2}"' in html
    assert 'data-tab="live"' not in html and 'id="softzone"' not in html
    html = c.get("/dashboard?scope=all").text  # the "all portfolios" card open: its filters and tabs inside it
    for tab in ('data-tab="live"', 'data-tab="overview"', 'data-tab="pairs"', 'data-tab="trades"', 'data-tab="portfolios"'):
        assert tab in html
    assert 'class="dcard open" data-key="all"' in html
    assert "dash-main" in html and "DashA" in html and "+25.00" in html and "My result" in html
    assert f'value="a{a1}"' in html and f'value="b{b2}"' in html  # pick a portfolio or a bot
    html = c.get(f"/dashboard?scope=b{b2}").text
    assert "-5.00" in html and 'id="openpos"' not in html  # the open BTC trade belongs to bot A
    html = c.get(f"/dashboard?scope=b{b1}").text
    assert 'id="openpos"' in html and "L.get(" not in html  # live table, and its refresh script is plain JS


def test_whole_account_and_a_card_per_portfolio(mine):
    """The top row is always the whole account; below, one card per portfolio with its own numbers, that opens
    its filters and tables inside it."""
    c, (uid, a1, a2, b1, b2) = mine
    html = c.get("/dashboard").text
    assert "Whole account" in html and "+25.00" in html.split('id="dcards"')[0]
    card1 = html.split(f'data-key="a{a1}"', 1)[1].split('<section', 1)[0]
    assert "dash-main" in card1 and "+15.00" in card1 and "1,000" in card1 and f'data-cu="{a1}"' in card1
    card2 = html.split(f'data-key="a{a2}"', 1)[1].split('<section', 1)[0]
    assert "+10.00" in card2 and "500" in card2
    html = c.get(f"/dashboard?scope=a{a1}").text  # this card open, the others closed
    assert f'class="dcard open" data-key="a{a1}"' in html and html.count('id="softzone"') == 1
    zone = html.split('id="softzone"', 1)[1]
    assert 'data-tab="trades"' in zone and "+15.00" in zone
