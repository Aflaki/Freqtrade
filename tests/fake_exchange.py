"""Minimal in-memory stand-in for a ccxt async exchange."""
import itertools

PRICES = {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0, "BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0}
_ids = itertools.count(1)


def _mk(base, spot):
    sym = f"{base}/USDT" if spot else f"{base}/USDT:USDT"
    return {"id": f"{base}USDT", "symbol": sym, "base": base, "quote": "USDT", "settle": None if spot else "USDT",
            "type": "spot" if spot else "swap", "spot": spot, "swap": not spot, "contract": not spot,
            "linear": None if spot else True, "contractSize": None if spot else 1.0, "active": True,
            "precision": {"amount": 0.001}, "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}}


class FakeExchange:
    instances = []
    spot = False

    def __init__(self, params):
        self.params = params
        self.orders = []
        self.leverage = {}
        self.markets = {}
        self.balance = {"USDT": 10_000.0, "BTC": 0.0, "ETH": 0.0}
        FakeExchange.instances.append(self)

    def set_sandbox_mode(self, on):
        self.sandbox = on

    async def load_markets(self):
        spot = self.params["options"]["defaultType"] == "spot"
        self.markets = {m["symbol"]: m for m in (_mk("BTC", spot), _mk("ETH", spot))}
        return self.markets

    def market(self, symbol):
        return self.markets[symbol]

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
        px = PRICES[symbol]
        m = self.markets[symbol]
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

    async def close(self):
        pass
