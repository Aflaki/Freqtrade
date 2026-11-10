"""Money management, modelled on Freqtrade's stake settings.

equal_split    : stake = capital / number of pairs                     (a fixed total, split equally)
pair_weights   : stake = capital * weight / sum(weights)
custom_amounts : stake = the amount the user typed for that pair        (user builds their own basket)
wallet_pct     : stake = wallet balance now * pct / number of pairs     (compounding, like Freqtrade "unlimited"),
                 never below the user's floor (min_stake)

The admin limits how much of a wallet may be put to work (site default, per-user override): fixed amounts must stay
below that share of the balance, and wallet_pct can't be set above it. The rest is the reserve for drawdowns.
"""
from __future__ import annotations

from ..db import ExchangeAccount, GlobalSettings

# with the pair catalog every user may choose: one total split equally, or an own amount per pair
CATALOG_MODES = ("equal_split", "custom_amounts", "wallet_pct")


def effective_mode(gs: GlobalSettings, acc: ExchangeAccount) -> str:
    allowed = gs.sizing_modes or ["equal_split"]
    if acc.sizing_mode in allowed or acc.sizing_mode in CATALOG_MODES:
        return acc.sizing_mode
    return allowed[0]


def wallet_cap(gs: GlobalSettings, user=None) -> float:
    """Largest share (0..1) of the wallet this user may put to work."""
    pct = getattr(user, "max_wallet_pct", None)
    return min(1.0, max(0.01, pct / 100)) if pct else gs.tradable_balance_ratio


def effective_capital(gs: GlobalSettings, acc: ExchangeAccount, balance: float | None = None) -> float:
    mode = effective_mode(gs, acc)
    if mode == "custom_amounts":
        return sum(max(0.0, float(v or 0)) for v in (acc.pairs or {}).values())
    if mode == "wallet_pct":
        bal = balance if balance is not None else (acc.wallet_balance or 0.0)
        return max(0.0, bal) * (acc.wallet_pct or 0.0) / 100
    return max(0.0, acc.capital)


def tradable_capital(gs: GlobalSettings, acc: ExchangeAccount, balance: float | None = None) -> float:
    return effective_capital(gs, acc, balance)


def max_open_trades(gs: GlobalSettings, acc: ExchangeAccount) -> int:
    """Only old portfolios have a limit; otherwise each chosen pair gets its own share (one trade per pair)."""
    if effective_mode(gs, acc) == "equal_split" and acc.max_open_trades:
        return max(1, acc.max_open_trades)
    return max(1, len(acc.pairs or {}))


def pair_budget(gs: GlobalSettings, acc: ExchangeAccount, pair: str, balance: float | None = None) -> float:
    """Margin for a new trade in this pair. balance: the wallet balance now (wallet_pct; else the last one read)."""
    mode = effective_mode(gs, acc)
    if mode == "custom_amounts":
        stake = max(0.0, float((acc.pairs or {}).get(pair) or 0))
    elif mode == "pair_weights":
        weights = {p: max(0.0, float(w or 0)) for p, w in (acc.pairs or {}).items()}
        total = sum(weights.values())
        stake = tradable_capital(gs, acc) * weights.get(pair, 0.0) / total if total else 0.0
    else:
        stake = tradable_capital(gs, acc, balance) / max_open_trades(gs, acc)
        if mode == "wallet_pct" and acc.min_stake:
            stake = max(stake, acc.min_stake)  # the user's floor per trade
    if gs.max_stake_per_trade:
        stake = min(stake, gs.max_stake_per_trade)
    return stake


def leverage_for(gs: GlobalSettings, acc: ExchangeAccount, pair: str | None = None,
                 signal_leverage: float | None = None) -> int:
    """Leverage is the admin's choice, never the user's: the fixed leverage of the pair's offer, else the Freqtrade
    bot's own leverage for this trade, else (old portfolios) the portfolio's; capped at the admin's maximum."""
    if acc.is_spot:
        return 1
    lev = None
    if pair:
        from .offers import forced_leverage
        lev = forced_leverage(pair, (acc.pair_bot or {}).get(pair))
    if not lev and signal_leverage:
        lev = int(round(signal_leverage))
    return max(1, min(int(lev or acc.leverage or 1), gs.max_leverage))


def allocation_table(gs: GlobalSettings, acc: ExchangeAccount) -> list[dict]:
    out = []
    for p, v in (acc.pairs or {}).items():
        stake, lev = pair_budget(gs, acc, p), leverage_for(gs, acc, p)
        out.append({"pair": p, "value": v, "stake": stake, "notional": stake * lev, "leverage": lev})
    return out


def stop_price(entry: float, side: str, stoploss_pct: float, leverage: float) -> float:
    """Freqtrade semantics: stoploss = max loss as % of margin -> price distance = pct / leverage."""
    dist = stoploss_pct / 100.0 / max(1.0, leverage)
    return entry * (1 - dist) if side == "long" else entry * (1 + dist)
