"""The admin's money-management formula, modelled on Freqtrade's stake settings."""
from __future__ import annotations

from ..db import ExchangeAccount, GlobalSettings


def tradable_capital(gs: GlobalSettings, acc: ExchangeAccount) -> float:
    """Freqtrade: available_capital * tradable_balance_ratio."""
    return max(0.0, acc.capital) * gs.tradable_balance_ratio


def max_open_trades(gs: GlobalSettings, acc: ExchangeAccount) -> int:
    if gs.sizing_formula == "pair_weights":
        return max(1, len(acc.pairs or {}))
    return max(1, acc.max_open_trades or gs.default_max_open_trades)


def pair_budget(gs: GlobalSettings, acc: ExchangeAccount, pair: str) -> float:
    """Margin (stake currency) for a NEW trade on `pair`."""
    budget = tradable_capital(gs, acc)
    if gs.sizing_formula == "pair_weights":
        weights = {p: max(0.0, float(w or 0)) for p, w in (acc.pairs or {}).items()}
        total = sum(weights.values())
        stake = budget * weights.get(pair, 0.0) / total if total else 0.0
    else:  # equal_split == Freqtrade stake_amount: "unlimited"
        stake = budget / max_open_trades(gs, acc)
    if gs.max_stake_per_trade:
        stake = min(stake, gs.max_stake_per_trade)
    return stake


def allocation_table(gs: GlobalSettings, acc: ExchangeAccount) -> list[dict]:
    """Preview shown to the user: how their capital is split."""
    lev = 1 if acc.is_spot else max(1, min(acc.leverage, gs.max_leverage))
    rows = []
    for pair in (acc.pairs or {}):
        stake = pair_budget(gs, acc, pair)
        rows.append({"pair": pair, "weight": acc.pairs[pair], "stake": stake, "notional": stake * lev})
    return rows
