"""Minimal in-memory stand-in for a ccxt async exchange."""
import itertools

PRICES = {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0, "BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0,
          "BTC/EUR": 55000.0}  # spot BTC also trades against EUR; ETH doesn't
_ids = itertools.count(1)


def _mk(base, spot, quote="USDT"):
    sym = f"{base}/{quote}" if spot else f"{base}/{quote}:{quote}"
    return {"id": f"{base}{quote}", "symbol": sym, "base": base, "quote": quote, "settle": None if spot else quote,
            "type": "spot" if spot else "swap", "spot": spot, "swap": not spot, "contract": not spot,
            "linear": None if spot else True, "contractSize": None if spot else 1.0, "active": True,
            "precision": {"amount": 0.001}, "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}}


class FakeExchange:
    instances = []
    spot = False
    has = {"fetchPositions": True}
    positions = []  # ccxt-style positions already on the account (shared, set by tests)
    fills = []  # ccxt-style own trades (fetch_my_trades), set by tests

    def __init__(self, params):
        self.params = params
        self.orders = []
        self.leverage = {}
        self.markets = {}
        self.stops = []
        self.cancelled = []
        self.balance = {"USDT": 10_000.0, "EUR": 5_000.0, "BTC": 0.0, "ETH": 0.0}
        FakeExchange.instances.append(self)

    def set_sandbox_mode(self, on):
        self.sandbox = on

    async def load_markets(self):
        spot = self.params["options"]["defaultType"] == "spot"
        self.markets = {m["symbol"]: m for m in (_mk("BTC", spot), _mk("ETH", spot))
                        + ((_mk("BTC", True, "EUR"),) if spot else ())}
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

    def price_to_precision(self, symbol, price):
        return f"{price:.2f}"

    async def cancel_order(self, oid, symbol, params=None):
        self.cancelled.append(oid)
        return {"id": oid, "status": "canceled"}

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1000) / 1000:.3f}"

    async def fetch_ticker(self, symbol):
        return {"last": PRICES[symbol]}

    async def fetch_balance(self):
        return {"free": dict(self.balance), "total": dict(self.balance)}

    async def set_margin_mode(self, mode, symbol):
        self.margin_mode = mode

    async def set_leverage(self, lev, symbol):
        self.leverage[symbol] = lev

    async def create_order(self, symbol, type_, side, amount, price, params):
        oid = str(next(_ids))
        if params.get("stopLossPrice"):
            self.stops.append({"id": oid, "symbol": symbol, "side": side, "amount": amount, "params": params})
            return {"id": oid, "status": "open"}
        px = PRICES[symbol]
        m = self.markets[symbol]
        if params.get("reduceOnly"):  # closes (part of) a position that is on the account
            for p in FakeExchange.positions:
                if p["symbol"] == symbol and (p["side"] == "long") == (side == "sell"):
                    p["contracts"] = max(0.0, p["contracts"] - amount)
            FakeExchange.positions[:] = [p for p in FakeExchange.positions if p["contracts"] > 0]
        fee = {"cost": amount * 0.001, "currency": m["base"]} if m["spot"] and side == "buy" else \
              {"cost": amount * px * 0.0005, "currency": "USDT"}
        if m["spot"]:
            if side == "buy":
                self.balance[m["base"]] += amount - fee["cost"]
            else:
                self.balance[m["base"]] -= amount
        self.orders.append({"id": oid, "symbol": symbol, "side": side, "amount": amount, "params": params,
                            "price": px, "fee": fee})
        # like many exchanges: create returns an incomplete order; details arrive via fetch_order
        return {"id": oid, "status": "open", "filled": None, "average": None}

    async def fetch_order(self, oid, symbol):
        o = next(o for o in self.orders if o["id"] == oid)
        return {"id": oid, "status": "closed", "filled": o["amount"], "average": o["price"], "fee": o["fee"]}

    async def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        end = (params or {}).get("endTime", 1e18)
        return [dict(f) for f in FakeExchange.fills
                if f["symbol"] == symbol and (since is None or f["timestamp"] >= since) and f["timestamp"] <= end]

    async def fetch_positions(self, symbols=None):
        return [dict(p) for p in FakeExchange.positions if not symbols or p["symbol"] in symbols]

    async def close(self):
        pass
