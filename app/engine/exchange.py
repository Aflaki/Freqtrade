"""ccxt wrapper with call logging, real-fill reading, exchange-side stop loss, and a simulator for signal-only users."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import ccxt.async_support as ccxt

from ..db import ExchangeAccount
from ..security import decrypt

log = logging.getLogger("relay.exchange")

EXCHANGES = {
    "binance": "Binance", "bybit": "Bybit", "okx": "OKX", "bitget": "Bitget",
    "kucoin": "KuCoin", "gate": "Gate.io", "mexc": "MEXC", "htx": "HTX", "bingx": "BingX",
}
NEEDS_PASSWORD = {"okx", "bitget", "kucoin"}


def ccxt_target(exchange: str, trading_mode: str) -> tuple[str, str]:
    if trading_mode == "spot":
        return exchange, "spot"
    if exchange == "binance":
        return "binanceusdm", "future"
    if exchange == "kucoin":
        return "kucoinfutures", "swap"
    return exchange, "swap"


def _compact(obj: Any, limit: int = 6000) -> Any:
    """JSON-safe, size-capped copy of an exchange response for the logs."""
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = json.dumps(str(obj))
    if len(s) > limit:
        return {"truncated": s[:limit]}
    return json.loads(s)


def client_version(acc: ExchangeAccount) -> tuple:
    """What a pooled client depends on: a new key or exchange setting builds a new client; bookkeeping fields
    (wallet balance, positions seen) don't."""
    return (acc.kind, acc.exchange, acc.trading_mode, acc.api_key_enc, acc.secret_enc, acc.password_enc,
            acc.sandbox, acc.dry_run)


def wallet_of(bal: dict, currency: str) -> float:
    """Wallet balance (realized profits and losses only, no open P/L) from a ccxt fetch_balance answer.
    Binance futures' "total" includes open P/L, so its raw walletBalance is used when present."""
    info = bal.get("info") if isinstance(bal, dict) else None
    rows = info.get("assets") if isinstance(info, dict) else info if isinstance(info, list) else None
    for r in rows or []:
        if isinstance(r, dict) and r.get("asset") == currency:
            for k in ("walletBalance", "crossWalletBalance", "balance"):
                if r.get(k) not in (None, ""):
                    try:
                        return float(r[k])
                    except (TypeError, ValueError):
                        pass
    return float((bal.get("total") or {}).get(currency) or 0)


@dataclass
class Fill:
    order_id: str
    amount: float
    price: float
    fee_quote: float
    fee_base: float
    status: str
    simulated: bool = False


class ExchangeClient:
    simulated = False

    def __init__(self, acc: ExchangeAccount):
        self.account_id = acc.id
        self.version = client_version(acc)
        self.dry_run = acc.dry_run
        self.is_spot = acc.is_spot
        self.calls: list[dict] = []
        klass_id, default_type = ccxt_target(acc.exchange, acc.trading_mode)
        klass = getattr(ccxt, klass_id, None)
        if klass is None:
            raise ValueError(f"exchange '{acc.exchange}' is not supported")
        params = {"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": default_type}}
        for field, name in ((acc.api_key_enc, "apiKey"), (acc.secret_enc, "secret"), (acc.password_enc, "password")):
            v = decrypt(field)
            if v:
                params[name] = v
        self.ex = klass(params)
        if acc.sandbox:
            self.ex.set_sandbox_mode(True)
        self._markets = False
        self._leverage_done: set[tuple[str, int]] = set()

    # ---------------------------------------------------------------- logging
    def drain_calls(self) -> list[dict]:
        out, self.calls = self.calls, []
        return out

    async def _call(self, method: str, request: dict, coro) -> Any:
        t0 = time.monotonic()
        entry: dict[str, Any] = {"method": method, "request": request}
        try:
            res = await coro
            entry["response"] = _compact(res)
            return res
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:500]}"
            raise
        finally:
            entry["ms"] = int((time.monotonic() - t0) * 1000)
            self.calls.append(entry)

    async def close(self) -> None:
        await self.ex.close()

    async def ensure_markets(self) -> None:
        if not self._markets:
            await self.ex.load_markets()
            self._markets = True

    # ---------------------------------------------------------------- markets
    def resolve(self, pair: str) -> str:
        base, quote = pair.upper().split("/")
        best = None
        for m in self.ex.markets.values():
            if m.get("base") != base or m.get("quote") != quote or m.get("active") is False:
                continue
            if self.is_spot and m.get("spot"):
                return m["symbol"]
            if not self.is_spot and m.get("swap") and m.get("linear", True):
                if m.get("settle") == quote:
                    return m["symbol"]
                best = best or m["symbol"]
        if best:
            return best
        raise ValueError(f"{pair} not available on this exchange ({'spot' if self.is_spot else 'futures'})")

    def market(self, symbol: str) -> dict:
        return self.ex.market(symbol)

    def contract_size(self, symbol: str) -> float:
        m = self.market(symbol)
        return float(m.get("contractSize") or 1) if m.get("contract") else 1.0

    def min_amount(self, symbol: str) -> float:
        return float(((self.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0)

    def min_cost(self, symbol: str) -> float:
        return float(((self.market(symbol).get("limits") or {}).get("cost") or {}).get("min") or 0)

    def precise(self, symbol: str, amount: float) -> float:
        try:
            return float(self.ex.amount_to_precision(symbol, amount))
        except (ccxt.ArgumentsRequired, ccxt.InvalidOrder):
            return 0.0

    def amount_for_notional(self, symbol: str, notional: float, price: float) -> float:
        base_qty = notional / price
        amount = self.precise(symbol, base_qty / self.contract_size(symbol))
        if amount <= 0 or amount < self.min_amount(symbol):
            raise ValueError(f"order size {base_qty:.6g} is below the exchange minimum")
        if notional < self.min_cost(symbol):
            raise ValueError(f"order value {notional:.2f} is below the exchange minimum {self.min_cost(symbol)}")
        return amount

    async def price(self, symbol: str, hint: float | None = None) -> float:
        try:
            t = await self.ex.fetch_ticker(symbol)
            p = t.get("last") or t.get("close")
            if p:
                return float(p)
        except Exception as e:
            if not hint:
                raise
            log.info("ticker failed for %s (%s), using signal price", symbol, e)
        if not hint:
            raise ValueError(f"no price for {symbol}")
        return float(hint)

    async def positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Open positions on the exchange (futures). Raises if the exchange can't list them."""
        if not getattr(self.ex, "has", {}).get("fetchPositions"):
            raise ValueError("this exchange can't list positions")
        return await self._call("fetch_positions", {"symbols": symbols}, self.ex.fetch_positions(symbols))

    async def wallet_balance(self, currency: str) -> float:
        bal = await self._call("fetch_balance", {}, self.ex.fetch_balance())
        return wallet_of(bal, currency)

    async def free_balance(self, currency: str) -> float:
        bal = await self._call("fetch_balance", {}, self.ex.fetch_balance())
        return float((bal.get("free") or {}).get(currency) or 0)

    async def setup_leverage(self, symbol: str, leverage: int, margin_mode: str) -> None:
        if self.is_spot or self.dry_run or (symbol, leverage) in self._leverage_done:
            return
        try:
            await self._call("set_margin_mode", {"symbol": symbol, "mode": margin_mode},
                             self.ex.set_margin_mode(margin_mode, symbol))
        except Exception as e:  # often "already set"
            log.info("[acc %s] set_margin_mode: %s", self.account_id, e)
        try:
            await self._call("set_leverage", {"symbol": symbol, "leverage": leverage},
                             self.ex.set_leverage(leverage, symbol))
        except Exception as e:
            log.info("[acc %s] set_leverage: %s", self.account_id, e)
        self._leverage_done.add((symbol, leverage))

    # ---------------------------------------------------------------- orders
    async def market_order(self, symbol: str, side: str, amount: float, reduce_only: bool,
                           price_hint: float | None = None) -> Fill:
        if self.dry_run:
            px = await self.price(symbol, price_hint)
            self.calls.append({"method": "create_order(dry-run)", "request": {
                "symbol": symbol, "side": side, "amount": amount, "reduceOnly": reduce_only}, "response": {"price": px}})
            return Fill("dry-run", amount, px, 0.0, 0.0, "closed", simulated=True)
        params = {"reduceOnly": True} if reduce_only and not self.is_spot else {}
        req = {"symbol": symbol, "type": "market", "side": side, "amount": amount, "params": params}
        order = await self._call("create_order", req, self.ex.create_order(symbol, "market", side, amount, None, params))
        oid = str(order.get("id") or "")
        for i in range(6):  # poll until the real fill is visible
            if order.get("status") == "closed" and order.get("filled") and order.get("average"):
                break
            await asyncio.sleep(0.5 * (i + 1))
            try:
                order = await self._call("fetch_order", {"id": oid, "symbol": symbol}, self.ex.fetch_order(oid, symbol))
            except Exception:
                try:
                    trades = await self._call("fetch_my_trades", {"symbol": symbol},
                                              self.ex.fetch_my_trades(symbol, limit=20))
                    mine = [t for t in trades if str(t.get("order")) == oid]
                    if mine:
                        qty = sum(float(t["amount"]) for t in mine)
                        cost = sum(float(t["amount"]) * float(t["price"]) for t in mine)
                        order = {**order, "filled": qty, "average": cost / qty, "status": "closed",
                                 "fees": [t.get("fee") for t in mine if t.get("fee")]}
                        break
                except Exception:
                    pass
        filled = float(order.get("filled") or 0) or amount
        avg = float(order.get("average") or order.get("price") or 0) or await self.price(symbol, price_hint)
        m = self.market(symbol)
        fee_quote = fee_base = 0.0
        for f in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            if not f or f.get("cost") is None:
                continue
            cost, cur = float(f["cost"]), f.get("currency")
            if cur == m.get("base"):
                fee_base += cost
                fee_quote += cost * avg
            elif cur in (m.get("quote"), m.get("settle")):
                fee_quote += cost
        return Fill(oid, filled, avg, fee_quote, fee_base, str(order.get("status") or "unknown"))

    async def place_stop(self, symbol: str, side: str, amount: float, stop_price: float) -> str | None:
        """Exchange-side protective stop (futures). Returns order id."""
        if self.dry_run or self.is_spot:
            return None
        px = float(self.ex.price_to_precision(symbol, stop_price))
        params = {"stopLossPrice": px, "reduceOnly": True}
        order = await self._call("create_order(stop)", {"symbol": symbol, "side": side, "amount": amount,
                                                         "params": params},
                                 self.ex.create_order(symbol, "market", side, amount, None, params))
        return str(order.get("id") or "") or None

    async def cancel(self, order_id: str | None, symbol: str) -> None:
        if not order_id or self.dry_run:
            return
        try:
            await self._call("cancel_order", {"id": order_id, "symbol": symbol},
                             self.ex.cancel_order(order_id, symbol, {"stop": True, "trigger": True}))
        except Exception:
            try:
                await self._call("cancel_order", {"id": order_id, "symbol": symbol},
                                 self.ex.cancel_order(order_id, symbol))
            except Exception as e:  # already triggered / gone
                log.info("[acc %s] cancel %s: %s", self.account_id, order_id, e)


class SimClient:
    """Signal-only portfolios: no exchange, fills at the signal price. Same interface as ExchangeClient."""
    simulated = True
    dry_run = True
    _public: Any = None

    def __init__(self, acc: ExchangeAccount, stake_currency: str = "USDT"):
        self.account_id = acc.id
        self.version = client_version(acc)
        self.is_spot = acc.is_spot
        self.calls: list[dict] = []

    def drain_calls(self) -> list[dict]:
        out, self.calls = self.calls, []
        return out

    async def close(self) -> None:
        pass

    async def ensure_markets(self) -> None:
        pass

    def resolve(self, pair: str) -> str:
        return pair if self.is_spot else f"{pair}:{pair.split('/')[1]}"

    def market(self, symbol: str) -> dict:
        base, rest = symbol.split("/")
        return {"base": base, "quote": rest.split(":")[0], "symbol": symbol}

    def contract_size(self, symbol: str) -> float:
        return 1.0

    def min_amount(self, symbol: str) -> float:
        return 0.0

    def precise(self, symbol: str, amount: float) -> float:
        return round(amount, 8)

    def amount_for_notional(self, symbol: str, notional: float, price: float) -> float:
        return round(notional / price, 8)

    async def price(self, symbol: str, hint: float | None = None) -> float:
        if hint:
            return float(hint)
        if SimClient._public is None:
            SimClient._public = ccxt.binance({"enableRateLimit": True})
        t = await SimClient._public.fetch_ticker(symbol.split(":")[0])
        return float(t["last"])

    async def free_balance(self, currency: str) -> float:
        return float("inf")

    async def setup_leverage(self, *a) -> None:
        pass

    async def market_order(self, symbol: str, side: str, amount: float, reduce_only: bool,
                           price_hint: float | None = None) -> Fill:
        px = await self.price(symbol, price_hint)
        self.calls.append({"method": "signal", "request": {"symbol": symbol, "side": side, "amount": amount},
                           "response": {"price": px}})
        return Fill("signal", amount, px, 0.0, 0.0, "closed", simulated=True)

    async def place_stop(self, *a) -> None:
        return None

    async def cancel(self, *a) -> None:
        pass


class ClientPool:
    def __init__(self) -> None:
        self._clients: dict[int, ExchangeClient | SimClient] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    async def get(self, acc: ExchangeAccount) -> ExchangeClient | SimClient:
        c = self._clients.get(acc.id)
        if c is not None and c.version != client_version(acc):
            await c.close()
            c = None
        if c is None:
            c = SimClient(acc) if acc.is_signal else ExchangeClient(acc)
            self._clients[acc.id] = c
        await c.ensure_markets()
        return c

    async def drop(self, account_id: int) -> None:
        c = self._clients.pop(account_id, None)
        if c:
            await c.close()

    async def close_all(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)
        self._clients.clear()
        if SimClient._public is not None:
            await SimClient._public.close()
            SimClient._public = None


pool = ClientPool()


async def test_connection(acc: ExchangeAccount, stake_currency: str) -> tuple[bool, str]:
    c = ExchangeClient(acc)
    try:
        await c.ensure_markets()
        bal = await c.ex.fetch_balance()
        free = float((bal.get("free") or {}).get(stake_currency) or 0)
        total = float((bal.get("total") or {}).get(stake_currency) or 0)
        return True, f"OK — {stake_currency}: {free:,.2f} / {total:,.2f}"
    except ccxt.AuthenticationError as e:
        return False, f"AuthenticationError: {str(e)[:200]}"
    except ccxt.PermissionDenied as e:
        return False, f"PermissionDenied: {str(e)[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:250]}"
    finally:
        await c.close()
