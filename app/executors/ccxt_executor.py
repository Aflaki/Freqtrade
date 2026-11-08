"""Places orders on any exchange supported by ccxt (Binance, Bybit, OKX, Bitget, KuCoin, ...)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import ccxt.async_support as ccxt

from ..config import Account
from ..signal import Signal
from .base import ExecResult

log = logging.getLogger("relay.ccxt")


class CcxtExecutor:
    def __init__(self, account: Account):
        self.acc = account
        self._lock = asyncio.Lock()  # one order at a time per account
        self._markets_loaded = False
        self._leverage_set: set[str] = set()
        self._sig_stake: float | None = None

        klass = getattr(ccxt, account.exchange, None)
        if klass is None:
            raise ValueError(f"[{account.name}] exchange '{account.exchange}' is not supported by ccxt")
        opts = {"defaultType": account.market_type, **account.exchange_options}
        params: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": int(account.timeout * 1000),
            "options": opts,
        }
        if account.api_key:
            params["apiKey"] = account.api_key
        if account.secret:
            params["secret"] = account.secret
        if account.password:
            params["password"] = account.password
        self.ex = klass(params)
        if account.sandbox:
            self.ex.set_sandbox_mode(True)

    @property
    def is_spot(self) -> bool:
        return self.acc.market_type == "spot"

    async def close(self) -> None:
        await self.ex.close()

    # ------------------------------------------------------------------ helpers
    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await self.ex.load_markets()
            self._markets_loaded = True

    def _resolve_symbol(self, sym: str, pair: str | None = None) -> str:
        if sym in self.acc.symbol_map:
            return self.acc.symbol_map[sym]
        want_spot = self.is_spot
        if pair and pair in self.ex.markets and bool(self.ex.markets[pair].get("spot")) == want_spot:
            return pair
        candidates = []
        for m in self.ex.markets.values():
            if not m.get("active", True):
                continue
            if bool(m.get("spot")) != want_spot:
                continue
            if not want_spot and m.get("type") not in (self.acc.market_type, None):
                continue
            mid = str(m.get("id", "")).upper().replace("-", "").replace("_", "")
            base_quote = f"{m.get('base', '')}{m.get('quote', '')}".upper()
            if sym in (mid, base_quote):
                candidates.append(m)
        if not candidates:
            raise ValueError(f"symbol '{sym}' not found on {self.acc.exchange} ({self.acc.market_type}); add it to symbol_map")
        # prefer linear (USDT-margined) contracts
        candidates.sort(key=lambda m: (not m.get("linear", True), m["symbol"]))
        return candidates[0]["symbol"]

    async def _price(self, symbol: str, signal_price: float | None) -> float:
        if signal_price:
            return signal_price
        t = await self.ex.fetch_ticker(symbol)
        p = t.get("last") or t.get("close")
        if not p:
            raise ValueError(f"could not fetch price for {symbol}")
        return float(p)

    async def _setup_leverage(self, symbol: str) -> None:
        if self.is_spot or symbol in self._leverage_set:
            return
        if self.acc.margin_mode:
            try:
                await self.ex.set_margin_mode(self.acc.margin_mode, symbol)
            except Exception as e:  # many exchanges error if already set
                log.info("[%s] set_margin_mode: %s", self.acc.name, e)
        if self.acc.leverage:
            try:
                await self.ex.set_leverage(self.acc.leverage, symbol)
            except Exception as e:
                log.info("[%s] set_leverage: %s", self.acc.name, e)
        self._leverage_set.add(symbol)

    async def _quote_balance(self, market: dict) -> float:
        bal = await self.ex.fetch_balance()
        quote = market.get("settle") or market["quote"]
        return float((bal.get("free") or {}).get(quote) or 0)

    async def _order_amount(self, market: dict, price: float, mult: float) -> tuple[float, float]:
        """Returns (amount_in_exchange_units, notional_in_quote)."""
        s = self.acc.sizing
        lev = 1 if self.is_spot else (self.acc.leverage or 1)
        if s.mode == "copy_stake":
            if not self._sig_stake:
                raise ValueError("copy_stake needs the signal's stake (Freqtrade stake_amount)")
            base_qty = self._sig_stake * s.amount * mult * lev / price
        elif s.mode == "fixed_base":
            base_qty = s.amount * mult
        elif s.mode == "fixed_quote":
            base_qty = s.amount * mult * lev / price
        else:  # percent_balance
            free = await self._quote_balance(market)
            base_qty = free * (s.amount / 100.0) * mult * lev / price

        notional = base_qty * price
        if s.max_notional and notional > s.max_notional:
            base_qty = s.max_notional / price
            notional = s.max_notional

        # contracts: convert base quantity to number of contracts
        contract_size = market.get("contractSize") or 1
        amount = base_qty / contract_size if market.get("contract") else base_qty
        amount = float(self.ex.amount_to_precision(market["symbol"], amount))

        min_amt = ((market.get("limits") or {}).get("amount") or {}).get("min")
        if amount <= 0 or (min_amt and amount < min_amt):
            raise ValueError(f"order amount {amount} is below exchange minimum {min_amt}")
        min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min")
        if min_cost and notional < min_cost:
            raise ValueError(f"order value {notional:.4f} is below exchange minimum cost {min_cost}")
        return amount, notional

    async def _open_positions(self, symbol: str) -> list[dict]:
        positions = await self.ex.fetch_positions([symbol])
        return [p for p in positions if p.get("symbol") == symbol and abs(float(p.get("contracts") or 0)) > 0]

    async def _place(self, symbol: str, side: str, amount: float, price: float | None, reduce_only: bool) -> dict:
        params = dict(self.acc.extra_params)
        if reduce_only and not self.is_spot:
            params["reduceOnly"] = True
        order_type = self.acc.order_type
        px = None
        if order_type == "limit":
            if not price:
                raise ValueError("limit orders need a price (in signal or ticker)")
            px = float(self.ex.price_to_precision(symbol, price))
        if self.acc.dry_run:
            return {"dry_run": True, "symbol": symbol, "side": side, "type": order_type,
                    "amount": amount, "price": px, "params": params}
        order = await self.ex.create_order(symbol, order_type, side, amount, px, params)
        return {"id": order.get("id"), "status": order.get("status"), "symbol": symbol, "side": side,
                "type": order_type, "amount": amount, "price": px or order.get("average")}

    # ------------------------------------------------------------------ main
    async def execute(self, sig: Signal) -> ExecResult:
        name = self.acc.name
        if self.acc.allowed_actions and sig.action not in self.acc.allowed_actions:
            return ExecResult(name, ok=True, skipped=True, message=f"action '{sig.action}' not allowed")
        if self.acc.allowed_symbols and sig.symbol not in self.acc.allowed_symbols:
            return ExecResult(name, ok=True, skipped=True, message=f"symbol '{sig.symbol}' not allowed")

        async with self._lock:
            await self._ensure_markets()
            symbol = self._resolve_symbol(sig.symbol, sig.pair)
            self._sig_stake = sig.stake
            market = self.ex.market(symbol)
            orders: list[dict] = []

            if sig.action in ("long", "short"):
                side = "buy" if sig.action == "long" else "sell"
                if self.is_spot and side == "sell":
                    # spot "short" = sell what we hold
                    return await self._spot_close(sig, symbol, market)
                await self._setup_leverage(symbol)
                price = await self._price(symbol, sig.price)
                amount, notional = await self._order_amount(market, price, sig.size_multiplier)
                orders.append(await self._place(symbol, side, amount, price, reduce_only=False))
                return ExecResult(name, ok=True, message=f"{side} {amount} {symbol} (~{notional:.2f} {market['quote']})",
                                  details={"orders": orders})

            # ---- close actions
            if self.is_spot:
                return await self._spot_close(sig, symbol, market)

            if self.acc.dry_run and not self.acc.api_key:
                return ExecResult(name, ok=True, message=f"dry-run: would {sig.action} {sig.close_fraction:.0%} on {symbol}")

            positions = await self._open_positions(symbol)
            for p in positions:
                pside = p.get("side")  # "long" / "short"
                if sig.action == "close_long" and pside != "long":
                    continue
                if sig.action == "close_short" and pside != "short":
                    continue
                amount = abs(float(p["contracts"]))
                if sig.close_fraction < 1.0:
                    amount = float(self.ex.amount_to_precision(symbol, amount * sig.close_fraction))
                    min_amt = ((market.get("limits") or {}).get("amount") or {}).get("min")
                    if amount <= 0 or (min_amt and amount < min_amt):
                        log.info("[%s] partial close %.4f too small, skipping", self.acc.name, sig.close_fraction)
                        continue
                side = "sell" if pside == "long" else "buy"
                orders.append(await self._place(symbol, side, amount, sig.price, reduce_only=True))
            if not orders:
                return ExecResult(name, ok=True, skipped=True, message=f"no open position to {sig.action} on {symbol}")
            pct = "" if sig.close_fraction >= 1.0 else f" ({sig.close_fraction:.0%})"
            return ExecResult(name, ok=True, message=f"closed {len(orders)} position(s) on {symbol}{pct}",
                              details={"orders": orders})

    async def _spot_close(self, sig: Signal, symbol: str, market: dict) -> ExecResult:
        if self.acc.dry_run and not self.acc.api_key:
            return ExecResult(self.acc.name, ok=True, message=f"dry-run: would sell all {market['base']} on {symbol}")
        bal = await self.ex.fetch_balance()
        qty = float((bal.get("free") or {}).get(market["base"]) or 0) * min(1.0, sig.close_fraction)
        qty = float(self.ex.amount_to_precision(symbol, qty)) if qty else 0.0
        min_amt = ((market.get("limits") or {}).get("amount") or {}).get("min")
        if qty <= 0 or (min_amt and qty < min_amt):
            return ExecResult(self.acc.name, ok=True, skipped=True, message=f"no {market['base']} balance to sell")
        order = await self._place(symbol, "sell", qty, sig.price, reduce_only=False)
        return ExecResult(self.acc.name, ok=True, message=f"sold {qty} {market['base']}", details={"orders": [order]})
