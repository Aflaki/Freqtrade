"""Thin async wrapper around ccxt: symbol resolution, sizing precision, orders and real fills."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

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
    """(ccxt class id, defaultType) for an exchange + trading mode."""
    if trading_mode == "spot":
        return exchange, "spot"
    if exchange == "binance":
        return "binanceusdm", "future"
    if exchange == "kucoin":
        return "kucoinfutures", "swap"
    return exchange, "swap"


@dataclass
class Fill:
    order_id: str
    amount: float  # filled, exchange units
    price: float  # average fill price
    fee_quote: float  # fee converted to quote where possible
    fee_base: float  # fee charged in base currency (spot buys)
    status: str
    simulated: bool = False


class ExchangeClient:
    def __init__(self, acc: ExchangeAccount):
        self.account_id = acc.id
        self.version = acc.updated_at
        self.dry_run = acc.dry_run
        self.is_spot = acc.is_spot
        klass_id, default_type = ccxt_target(acc.exchange, acc.trading_mode)
        klass = getattr(ccxt, klass_id, None)
        if klass is None:
            raise ValueError(f"exchange '{acc.exchange}' is not supported")
        params = {"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": default_type}}
        key, secret, pw = decrypt(acc.api_key_enc), decrypt(acc.secret_enc), decrypt(acc.password_enc)
        if key:
            params["apiKey"] = key
        if secret:
            params["secret"] = secret
        if pw:
            params["password"] = pw
        self.ex = klass(params)
        if acc.sandbox:
            self.ex.set_sandbox_mode(True)
        self._markets = False
        self._leverage_done: set[tuple[str, int]] = set()

    async def close(self) -> None:
        await self.ex.close()

    async def ensure_markets(self) -> None:
        if not self._markets:
            await self.ex.load_markets()
            self._markets = True

    def resolve(self, pair: str) -> str:
        """'BTC/USDT' -> exchange symbol ('BTC/USDT' spot, 'BTC/USDT:USDT' linear perpetual)."""
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
        raise ValueError(f"{pair} در این صرافی ({'اسپات' if self.is_spot else 'فیوچرز'}) پیدا نشد")

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
        except ccxt.ArgumentsRequired:
            return 0.0
        except ccxt.InvalidOrder:  # below precision
            return 0.0

    def amount_for_notional(self, symbol: str, notional: float, price: float) -> float:
        """Exchange units (contracts or base) for a notional value in quote."""
        base_qty = notional / price
        amount = self.precise(symbol, base_qty / self.contract_size(symbol))
        if amount <= 0 or amount < self.min_amount(symbol):
            raise ValueError(f"حجم سفارش ({base_qty:.6g}) از حداقل صرافی کمتر است")
        if notional < self.min_cost(symbol):
            raise ValueError(f"ارزش سفارش ({notional:.2f}) از حداقل صرافی ({self.min_cost(symbol)}) کمتر است")
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
            raise ValueError(f"قیمت {symbol} در دسترس نیست")
        return float(hint)

    async def free_balance(self, currency: str) -> float:
        bal = await self.ex.fetch_balance()
        return float((bal.get("free") or {}).get(currency) or 0)

    async def setup_leverage(self, symbol: str, leverage: int, margin_mode: str) -> None:
        if self.is_spot or self.dry_run or (symbol, leverage) in self._leverage_done:
            return
        try:
            await self.ex.set_margin_mode(margin_mode, symbol)
        except Exception as e:  # often "already set"
            log.info("[acc %s] set_margin_mode: %s", self.account_id, e)
        try:
            await self.ex.set_leverage(leverage, symbol)
        except Exception as e:
            log.info("[acc %s] set_leverage: %s", self.account_id, e)
        self._leverage_done.add((symbol, leverage))

    async def market_order(self, symbol: str, side: str, amount: float, reduce_only: bool,
                           price_hint: float | None = None) -> Fill:
        if self.dry_run:
            px = await self.price(symbol, price_hint)
            return Fill(order_id="dry-run", amount=amount, price=px, fee_quote=0.0, fee_base=0.0,
                        status="closed", simulated=True)
        params = {"reduceOnly": True} if reduce_only and not self.is_spot else {}
        order = await self.ex.create_order(symbol, "market", side, amount, None, params)
        oid = str(order.get("id") or "")
        # Many exchanges return an incomplete order on create; poll until we see the real fill.
        for i in range(6):
            if order.get("status") == "closed" and order.get("filled") and order.get("average"):
                break
            await asyncio.sleep(0.5 * (i + 1))
            try:
                order = await self.ex.fetch_order(oid, symbol)
            except Exception as e:
                log.info("[acc %s] fetch_order %s: %s", self.account_id, oid, e)
                try:  # some exchanges only expose recent fills
                    trades = await self.ex.fetch_my_trades(symbol, limit=20)
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
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for f in fees:
            if not f or f.get("cost") is None:
                continue
            cost, cur = float(f["cost"]), f.get("currency")
            if cur == m.get("base"):
                fee_base += cost
                fee_quote += cost * avg
            elif cur in (m.get("quote"), m.get("settle")):
                fee_quote += cost
        return Fill(order_id=oid, amount=filled, price=avg, fee_quote=fee_quote, fee_base=fee_base,
                    status=str(order.get("status") or "unknown"))


class ClientPool:
    """One ccxt instance per account; rebuilt automatically when the account is edited."""

    def __init__(self) -> None:
        self._clients: dict[int, ExchangeClient] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    async def get(self, acc: ExchangeAccount) -> ExchangeClient:
        c = self._clients.get(acc.id)
        if c is not None and c.version != acc.updated_at:
            await c.close()
            c = None
        if c is None:
            c = ExchangeClient(acc)
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


pool = ClientPool()


async def test_connection(acc: ExchangeAccount, stake_currency: str) -> tuple[bool, str]:
    """Used by the 'test connection' button: loads markets and reads the balance with the user's key."""
    c = ExchangeClient(acc)
    try:
        await c.ensure_markets()
        bal = await c.ex.fetch_balance()
        free = float((bal.get("free") or {}).get(stake_currency) or 0)
        total = float((bal.get("total") or {}).get(stake_currency) or 0)
        return True, f"اتصال موفق. موجودی {stake_currency}: آزاد {free:,.2f} / کل {total:,.2f}"
    except ccxt.AuthenticationError as e:
        return False, f"کلید API نامعتبر است: {str(e)[:200]}"
    except ccxt.PermissionDenied as e:
        return False, f"کلید دسترسی کافی ندارد: {str(e)[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:250]}"
    finally:
        await c.close()
