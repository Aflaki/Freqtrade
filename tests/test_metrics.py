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
    """Group capital is used for the whole group; a lone bot gets an equal share unless it has its own."""
    from app.db import Bot, BotGroup, SessionLocal, init_db
    from app.engine.performance import capitals
    init_db()
    with SessionLocal() as s:
        g = BotGroup(name="CapTest", stats_capital=3000.0)
        s.add(g)
        s.flush()
        bots = [Bot(name=f"cap{i}", group_id=g.id, webhook_secret=f"capsecret{i}") for i in range(3)]
        s.add_all(bots)
        s.commit()
        ids = [b.id for b in bots]
        s.get(Bot, ids[2]).stats_capital = 500.0
        s.commit()
    assert capitals(ids) == 3000.0                 # the whole group -> group capital
    assert capitals([ids[0]]) == 1000.0            # one bot, no own capital -> 3000 / 3
    assert capitals([ids[2]]) == 500.0             # own capital wins
    assert capitals([ids[0], ids[1]]) == 2000.0    # part of the group -> shares
