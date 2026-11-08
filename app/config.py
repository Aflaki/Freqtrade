"""Load config.yaml, substitute ${ENV_VAR} placeholders, and validate basics."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} with environment values."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            v = os.getenv(name, default)
            if v is None:
                raise ValueError(f"Environment variable '{name}' is not set")
            return v
        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class Sizing:
    # fixed_quote  : `amount` is the margin in quote currency (e.g. 50 USDT). notional = amount * leverage
    # fixed_base   : `amount` is the order quantity in base currency (e.g. 0.01 BTC)
    # percent_balance : `amount` is % of free quote balance used as margin. notional = margin * leverage
    # copy_stake   : `amount` is a ratio of the margin the source (Freqtrade) used. 1.0 = same, 0.5 = half
    mode: str = "fixed_quote"
    amount: float = 10.0
    max_notional: float | None = None  # hard cap on notional per order (quote currency)


@dataclass
class Account:
    name: str
    type: str  # "ccxt" or "http"
    enabled: bool = True
    dry_run: bool = False
    # --- ccxt ---
    exchange: str | None = None
    api_key: str | None = None
    secret: str | None = None
    password: str | None = None
    market_type: str = "swap"  # "spot" | "swap" | "future"
    sandbox: bool = False
    leverage: int | None = None
    margin_mode: str | None = None  # "isolated" | "cross"
    order_type: str = "market"  # "market" | "limit"
    sizing: Sizing = field(default_factory=Sizing)
    symbol_map: dict[str, str] = field(default_factory=dict)
    allowed_symbols: list[str] | None = None
    allowed_actions: list[str] | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)
    exchange_options: dict[str, Any] = field(default_factory=dict)
    # --- http ---
    url: str | None = None
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body_template: Any = None  # dict/list/str with {placeholders}; None => forward normalized signal
    # --- common ---
    timeout: float = 15.0
    retries: int = 2


@dataclass
class Settings:
    secret: str
    source: str  # "freqtrade" | "generic"
    freqtrade_relay_on: str  # "fill" | "order"
    allowed_ips: list[str]
    dedupe_seconds: int
    field_map: dict[str, str]
    action_aliases: dict[str, str]
    symbol_cleanup: dict[str, Any]
    accounts: list[Account]


def _parse_account(raw: dict) -> Account:
    raw = dict(raw)
    sizing = Sizing(**raw.pop("sizing", {}) or {})
    acc = Account(sizing=sizing, **raw)
    if acc.type not in ("ccxt", "http"):
        raise ValueError(f"[{acc.name}] type must be 'ccxt' or 'http'")
    if acc.type == "ccxt" and not acc.exchange:
        raise ValueError(f"[{acc.name}] 'exchange' is required for ccxt accounts")
    if acc.type == "http" and not acc.url:
        raise ValueError(f"[{acc.name}] 'url' is required for http accounts")
    if acc.sizing.mode not in ("fixed_quote", "fixed_base", "percent_balance", "copy_stake"):
        raise ValueError(f"[{acc.name}] unknown sizing.mode '{acc.sizing.mode}'")
    return acc


def load_settings(path: str | None = None) -> Settings:
    load_dotenv()
    path = path or os.getenv("RELAY_CONFIG", "config.yaml")
    data = _expand_env(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    server = data.get("server", {})
    sig = data.get("signal", {})
    accounts = [_parse_account(a) for a in data.get("accounts", [])]
    names = [a.name for a in accounts]
    if len(names) != len(set(names)):
        raise ValueError("Account names must be unique")

    secret = str(server.get("secret", ""))
    if len(secret) < 12:
        raise ValueError("server.secret must be at least 12 characters")

    source = str(sig.get("source", "generic")).lower()
    if source not in ("freqtrade", "generic"):
        raise ValueError("signal.source must be 'freqtrade' or 'generic'")
    relay_on = str((sig.get("freqtrade") or {}).get("relay_on", "fill")).lower()
    if relay_on not in ("fill", "order"):
        raise ValueError("signal.freqtrade.relay_on must be 'fill' or 'order'")

    return Settings(
        secret=secret,
        source=source,
        freqtrade_relay_on=relay_on,
        allowed_ips=server.get("allowed_ips") or [],
        dedupe_seconds=int(server.get("dedupe_seconds", 30)),
        field_map=sig.get("fields", {}),
        action_aliases={k.lower(): v for k, v in (sig.get("action_aliases") or {}).items()},
        symbol_cleanup=sig.get("symbol_cleanup", {}),
        accounts=accounts,
    )
