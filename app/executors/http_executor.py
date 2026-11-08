"""Forwards the signal to any custom HTTP API (your own bot, an exchange without ccxt support, etc.)."""
from __future__ import annotations

import json
import string
from typing import Any

import httpx

from ..config import Account
from ..signal import Signal
from .base import ExecResult


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render(tpl: Any, ctx: dict) -> Any:
    """Fill {placeholders} recursively. A string that is exactly one placeholder keeps the value's type."""
    if isinstance(tpl, str):
        fields = [f for _, f, _, _ in string.Formatter().parse(tpl) if f]
        if len(fields) == 1 and tpl == "{" + fields[0] + "}" and fields[0] in ctx:
            return ctx[fields[0]]
        return tpl.format_map(_SafeDict(ctx))
    if isinstance(tpl, list):
        return [_render(v, ctx) for v in tpl]
    if isinstance(tpl, dict):
        return {k: _render(v, ctx) for k, v in tpl.items()}
    return tpl


class HttpExecutor:
    def __init__(self, account: Account):
        self.acc = account
        self.client = httpx.AsyncClient(timeout=account.timeout)

    async def close(self) -> None:
        await self.client.aclose()

    def _context(self, sig: Signal) -> dict:
        s = self.acc.sizing
        ctx = {
            "action": sig.action,
            "side": "buy" if sig.action in ("long", "close_short") else "sell",
            "symbol": self.acc.symbol_map.get(sig.symbol, sig.symbol),
            "price": sig.price,
            "id": sig.id,
            "amount": (sig.stake or 0) * s.amount if s.mode == "copy_stake" else s.amount * sig.size_multiplier,
            "close_fraction": sig.close_fraction,
            "stake": sig.stake,
            "pair": sig.pair,
            "sizing_mode": s.mode,
            "leverage": self.acc.leverage,
            "account": self.acc.name,
            "signal": sig.to_dict(),
        }
        # expose raw payload fields too, as {raw_<field>}
        for k, v in sig.raw.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                ctx[f"raw_{k}"] = v
        return ctx

    async def execute(self, sig: Signal) -> ExecResult:
        name = self.acc.name
        if self.acc.allowed_actions and sig.action not in self.acc.allowed_actions:
            return ExecResult(name, ok=True, skipped=True, message=f"action '{sig.action}' not allowed")
        if self.acc.allowed_symbols and sig.symbol not in self.acc.allowed_symbols:
            return ExecResult(name, ok=True, skipped=True, message=f"symbol '{sig.symbol}' not allowed")

        ctx = self._context(sig)
        body = _render(self.acc.body_template, ctx) if self.acc.body_template is not None else ctx
        headers = _render(self.acc.headers, ctx)
        if self.acc.dry_run:
            return ExecResult(name, ok=True, message="dry-run", details={"url": self.acc.url, "body": body})

        kwargs: dict[str, Any] = {"headers": headers}
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body)
        r = await self.client.request(self.acc.method.upper(), self.acc.url, **kwargs)
        try:
            resp: Any = r.json()
        except json.JSONDecodeError:
            resp = r.text[:500]
        if r.status_code >= 400:
            # raise so the dispatcher's retry logic kicks in for 5xx; 4xx is reported as failure
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {resp}")
            return ExecResult(name, ok=False, message=f"HTTP {r.status_code}", details={"response": resp})
        return ExecResult(name, ok=True, message=f"HTTP {r.status_code}", details={"response": resp})
