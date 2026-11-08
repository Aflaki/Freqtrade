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
