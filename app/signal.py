"""Normalize any incoming webhook payload into a single Signal shape."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# Canonical actions understood by the executors
ACTIONS = {"long", "short", "close_long", "close_short", "close"}

DEFAULT_ALIASES = {
    "buy": "long", "long": "long", "enter_long": "long", "open_long": "long",
    "sell": "short", "short": "short", "enter_short": "short", "open_short": "short",
    "close_long": "close_long", "exit_long": "close_long",
    "close_short": "close_short", "exit_short": "close_short",
    "close": "close", "exit": "close", "flat": "close", "close_all": "close",
}

DEFAULT_FIELDS = {
    "action": "action",
    "symbol": "symbol",
    "price": "price",
    "id": "id",
    "size_multiplier": "size_multiplier",
    "passphrase": "passphrase",
}


class SignalError(ValueError):
    pass


@dataclass
class Signal:
    action: str
    symbol: str  # cleaned, e.g. "BTCUSDT"
    price: float | None = None
    id: str | None = None
    size_multiplier: float = 1.0
    close_fraction: float = 1.0  # for close actions: 1.0 = full close, 0.25 = close 25%
    stake: float | None = None  # margin (quote currency) the source used for this order
    pair: str | None = None  # original ccxt-style pair, e.g. "BTC/USDT:USDT"
    ft_trade_id: int | None = None
    enter_tag: str = ""
    exit_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get(payload: dict, path: str | None) -> Any:
    """Supports dotted paths, e.g. 'data.order.side'."""
    if not path:
        return None
    cur: Any = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _clean_symbol(sym: str, cleanup: dict) -> str:
    s = str(sym).strip().upper()
    if "/" in s and ":" in s.split("/", 1)[1]:
        # ccxt futures pair "BTC/USDT:USDT" -> drop settle currency
        s = s.rsplit(":", 1)[0]
    if ":" in s and cleanup.get("strip_exchange_prefix", True):
        # exchange prefix like "BINANCE:BTCUSDT"
        s = s.split(":", 1)[1]
    for suffix in cleanup.get("strip_suffixes", [".P", "PERP"]):
        if s.endswith(suffix.upper()) and len(s) > len(suffix):
            s = s[: -len(suffix)]
    return s.replace("/", "").replace("-", "").replace("_", "")


def parse_signal(payload: dict, field_map: dict, aliases: dict, cleanup: dict) -> Signal:
    fields = {**DEFAULT_FIELDS, **(field_map or {})}
    alias_map = {**DEFAULT_ALIASES, **(aliases or {})}

    raw_action = _get(payload, fields["action"])
    raw_symbol = _get(payload, fields["symbol"])
    if raw_action is None or raw_symbol is None:
        raise SignalError(f"payload must contain '{fields['action']}' and '{fields['symbol']}'")

    action = alias_map.get(str(raw_action).strip().lower())
    if action not in ACTIONS:
        raise SignalError(f"unknown action '{raw_action}'")

    price = _get(payload, fields["price"])
    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = None

    mult = _get(payload, fields["size_multiplier"])
    try:
        mult = float(mult) if mult not in (None, "") else 1.0
    except (TypeError, ValueError):
        mult = 1.0
    if mult <= 0:
        raise SignalError("size_multiplier must be > 0")

    sig_id = _get(payload, fields["id"])
    if sig_id is None:
        # derive a stable id from the payload so duplicate deliveries are deduped
        clean = {k: v for k, v in payload.items() if k != fields["passphrase"]}
        sig_id = hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()[:16]

    raw = {k: v for k, v in payload.items() if k != fields["passphrase"]}
    return Signal(
        action=action,
        symbol=_clean_symbol(raw_symbol, cleanup or {}),
        price=price,
        id=str(sig_id),
        size_multiplier=mult,
        raw=raw,
    )


# ---------------------------------------------------------------- Freqtrade
FT_TYPES = {"fill": ("entry_fill", "exit_fill"), "order": ("entry", "exit")}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str:
    v = "" if v is None else str(v)
    return "" if v == "None" else v[:80]


def canonical_pair(sig: Signal, allowed_pairs: list[str]) -> str | None:
    """Map a signal to a canonical spot-style pair like 'BTC/USDT'."""
    if sig.pair and "/" in sig.pair:
        return sig.pair.split(":", 1)[0].upper()
    for p in allowed_pairs:
        if p.replace("/", "").upper() == sig.symbol:
            return p.upper()
    return None


def parse_freqtrade(payload: dict, relay_on: str = "fill") -> Signal | None:
    """Turn a Freqtrade webhook message into a Signal. Returns None for message types we don't relay."""
    msg_type = str(payload.get("type", "")).strip().lower()
    entry_t, exit_t = FT_TYPES.get(relay_on, FT_TYPES["fill"])
    if msg_type not in (entry_t, exit_t):
        return None

    pair = payload.get("pair")
    direction = str(payload.get("direction", "")).strip().lower()
    if not pair or direction not in ("long", "short"):
        raise SignalError("freqtrade message needs 'pair' and 'direction' (Long/Short)")

    amount = _f(payload.get("amount"))
    rate = _f(payload.get("order_rate")) or _f(payload.get("limit"))
    open_rate = _f(payload.get("open_rate")) or rate
    lev = _f(payload.get("leverage")) or 1.0
    stake = _f(payload.get("stake_amount"))

    if msg_type == entry_t:
        action = direction
        # margin used by THIS order (correct for DCA / position adjustment too)
        order_stake = amount * rate / lev if amount and rate else stake
        close_fraction = 1.0
    else:
        action = f"close_{direction}"
        order_stake = None
        close_fraction = 1.0
        is_final = str(payload.get("is_final_exit", "true")).lower() in ("true", "1")
        sub_trade = str(payload.get("sub_trade", "false")).lower() in ("true", "1")
        if sub_trade and not (msg_type == "exit_fill" and is_final) and amount and stake and open_rate:
            pos_amount = stake * lev / open_rate  # base amount covered by stake_amount
            if msg_type == "exit_fill":
                # trade already reduced: stake_amount = what is still open
                close_fraction = amount / (amount + pos_amount)
            else:
                # order just placed: stake_amount = whole position
                close_fraction = amount / pos_amount
            close_fraction = max(0.0, min(1.0, close_fraction))

    sig_id = "ft-" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
    raw = {k: v for k, v in payload.items() if k != "passphrase"}
    return Signal(
        action=action,
        symbol=_clean_symbol(pair, {}),
        price=rate,
        id=sig_id,
        close_fraction=round(close_fraction, 6),
        stake=order_stake,
        pair=str(pair),
        ft_trade_id=int(_f(payload.get("trade_id"))) if _f(payload.get("trade_id")) is not None else None,
        enter_tag=_s(payload.get("enter_tag")),
        exit_reason=_s(payload.get("exit_reason")),
        raw=raw,
    )
