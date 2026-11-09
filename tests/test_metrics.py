"""Metrics follow Freqtrade's definitions (profit / starting capital, balance-based max drawdown)."""
import datetime as dt
import types

import pytest

from app.engine.performance import _stats


def trade(abs_, ratio, lev=5.0):
    d = dt.datetime(2026, 1, 1)
    return types.SimpleNamespace(profit_abs=abs_, profit_ratio=ratio, stake_amount=None, leverage=lev,
                                 open_date=d, close_date=d + dt.timedelta(hours=2))


def test_freqtrade_definitions():
    ts = [trade(100, 0.10), trade(-50, -0.05), trade(-100, -0.10), trade(200, 0.20)]
    st = _stats(ts, capital=1000)
    assert st["profit_abs"] == 150 and st["return_pct"] == pytest.approx(15.0)
    assert st["avg"] == pytest.approx(3.75)                 # mean of close_profit, like FreqUI "avg profit"
    assert st["max_dd_abs"] == pytest.approx(150)            # from +100 down to -50
    assert st["max_dd_pct"] == pytest.approx(150 / 1100 * 100)  # relative to the balance at the high (1000 + 100)
    assert st["win_rate"] == 50 and st["avg_lev"] == 5


def test_without_capital_no_percentages():
    st = _stats([trade(10, 0.01)], capital=None)
    assert st["return_pct"] is None and st["max_dd_pct"] is None and st["profit_abs"] == 10


def test_amount_from_stake_when_abs_missing():
    t = trade(None, 0.25)
    t.stake_amount = 40
    assert _stats([t])["profit_abs"] == pytest.approx(10)


def test_group_capital():
    """Bots with own capital keep it; the rest of the group's capital is split by number of pairs."""
    from app.db import Bot, BotGroup, SessionLocal, init_db
    from app.engine.performance import capital_breakdown, capitals
    init_db()
    with SessionLocal() as s:
        g = BotGroup(name="CapTest", stats_capital=3000.0)
        s.add(g)
        s.flush()
        b0 = Bot(name="cap0", group_id=g.id, webhook_secret="capsecret0", state={"whitelist": ["BTC/USDT", "ETH/USDT"]})
        b1 = Bot(name="cap1", group_id=g.id, webhook_secret="capsecret1",
                 state={"whitelist": ["SOL/USDT", "XRP/USDT", "BNB/USDT", "DOGE/USDT"]},
                 pair_filter_mode="blacklist", pair_filter=["DOGE/USDT"])  # filtered pair does not count -> 3
        b2 = Bot(name="cap2", group_id=g.id, webhook_secret="capsecret2", stats_capital=500.0,
                 state={"starting_capital": 999})
        s.add_all([b0, b1, b2])
        s.commit()
        ids = [b0.id, b1.id, b2.id]
    bd = capital_breakdown()
    assert bd[ids[2]] == {"capital": 500.0, "how": "manual"}       # own capital, subtracted from the group
    assert bd[ids[0]]["capital"] == 1000.0 and bd[ids[0]]["pairs"] == 2   # 2500 * 2/5
    assert bd[ids[1]]["capital"] == 1500.0 and bd[ids[1]]["group_pairs"] == 5
    assert capitals(ids) == 3000.0 and capitals([ids[0]]) == 1000.0 and capitals(ids[:2]) == 2500.0
