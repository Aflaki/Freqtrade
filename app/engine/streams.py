"""Live account data over WebSocket (Binance futures), so trading and pages don't have to ask the exchange.

For every active real Binance futures portfolio we keep one user-data stream (ccxt pro: listen key + keep-alive).
The exchange pushes balance and position changes the moment they happen; requests cost nothing from the per-IP
request budget. One public stream brings the mark price of every futures market.

Readers (exchange client, live pages) take the numbers from here while the stream is connected, and fall back to
REST requests (through the request budget) when it isn't. Streams are started gradually (the exchange limits new
connections per IP) and restarted with a growing pause after a failure.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from sqlalchemy import select

from ..db import ExchangeAccount, SessionLocal

log = logging.getLogger("relay.streams")
SYNC_EVERY = 30        # seconds between checks of which portfolios need a stream
START_GAP = 0.5        # seconds between two new connections
MARK_FRESH = 15        # a mark price older than this is not used
SUPPORTED = {"binanceusdm"}


class AccountStream:
    def __init__(self, acc_id: int, version: tuple):
        self.acc_id = acc_id
        self.version = version
        self.connected = False
        self.balance: dict | None = None      # ccxt balance; for Binance futures "free" holds the WALLET balance
        self.positions: dict[str, dict] = {}  # "SYMBOL|side" -> ccxt position (REST snapshot, then stream updates)
        self.at = 0.0
        self.error = ""
        self.ex = None
        self.tasks: list[asyncio.Task] = []
        self.failures = 0


def _key(p: dict) -> str | None:
    sym = p.get("symbol")
    side = str(p.get("side") or "").lower()
    if not sym:
        return None
    if side not in ("long", "short"):
        amt = float((p.get("info") or {}).get("pa") or p.get("contracts") or 0)
        side = "short" if amt < 0 else "long"
    return f"{sym}|{side}"


class StreamManager:
    def __init__(self) -> None:
        self.accounts: dict[int, AccountStream] = {}
        self.marks: dict[str, tuple[float, float]] = {}  # symbol -> (time, mark price)
        self.mark_ok = False
        self.factory: Callable[[ExchangeAccount], Any] | None = None  # tests inject fake clients
        self._public = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ readers
    def live(self, acc_id: int) -> AccountStream | None:
        st = self.accounts.get(acc_id)
        return st if st and st.connected and st.balance is not None else None

    def wallet(self, acc_id: int, currency: str) -> float | None:
        st = self.live(acc_id)
        if st is None:
            return None
        from .exchange import wallet_of
        if not (st.balance or {}).get(currency) and not (st.balance or {}).get("total"):
            return None
        return wallet_of(st.balance, currency)  # first a REST snapshot (walletBalance), then the stream ("wb")

    def mark(self, symbol: str) -> float | None:
        hit = self.marks.get(symbol)
        return hit[1] if hit and time.time() - hit[0] < MARK_FRESH else None

    def positions(self, acc_id: int) -> list[dict] | None:
        """ccxt-style positions with the current mark price and open P/L (None = no live stream)."""
        st = self.live(acc_id)
        if st is None:
            return None
        out = []
        for p in st.positions.values():
            contracts = abs(float(p.get("contracts") or 0))
            if not contracts:
                continue
            q = dict(p)
            mk = self.mark(q.get("symbol") or "")
            if mk:
                entry = float(q.get("entryPrice") or 0)
                d = 1 if _key(q).endswith("|long") else -1
                q["markPrice"] = mk
                q["unrealizedPnl"] = (mk - entry) * contracts * float(q.get("contractSize") or 1) * d if entry else q.get("unrealizedPnl")
                q["notional"] = contracts * float(q.get("contractSize") or 1) * mk
            out.append(q)
        return out

    def free_estimate(self, acc_id: int, currency: str) -> float | None:
        """Margin still free = wallet - margin held by positions + their open losses (the stream has no 'free')."""
        wallet = self.wallet(acc_id, currency)
        pos = self.positions(acc_id)
        if wallet is None or pos is None:
            return None
        held = 0.0
        for p in pos:
            lev = float(p.get("leverage") or 0)
            notional = abs(float(p.get("notional") or 0))
            margin = float(p.get("initialMargin") or p.get("collateral") or 0) or (notional / lev if lev else notional)
            held += margin - min(0.0, float(p.get("unrealizedPnl") or 0))
        return wallet - held

    # ------------------------------------------------------------ one account
    def _client(self, acc: ExchangeAccount):
        if self.factory:
            return self.factory(acc)
        import ccxt.pro as ccxtpro

        from ..security import decrypt
        from . import netips
        from .exchange import _shared_markets
        from .ratelimit import governor
        ex = ccxtpro.binanceusdm({"apiKey": decrypt(acc.api_key_enc), "secret": decrypt(acc.secret_enc),
                                  "enableRateLimit": True,
                                  "options": {"defaultType": "future", "fetchCurrencies": False,
                                              "fetchBalanceSnapshot": True, "awaitBalanceSnapshot": True,
                                              "fetchPositionsSnapshot": True, "awaitPositionsSnapshot": True}})
        if acc.sandbox:
            ex.set_sandbox_mode(True)
        ip = netips.ip_for(acc)
        netips.bind(ex, ip)
        governor.attach(ex, ip)
        hit = _shared_markets.get(("binanceusdm", bool(acc.sandbox)))
        if hit:
            ex.set_markets(hit[1])
        return ex

    async def _watch(self, st: AccountStream, what: str) -> None:
        delay = 2.0
        while True:
            try:
                if what == "balance":
                    st.balance = await st.ex.watch_balance()
                else:
                    for p in await st.ex.watch_positions() or []:
                        k = _key(p)
                        if not k:
                            continue
                        prev = st.positions.get(k, {})
                        merged = {**prev, **{kk: vv for kk, vv in p.items() if vv is not None}}  # keep leverage etc.
                        if abs(float(merged.get("contracts") or 0)) > 0:
                            st.positions[k] = merged
                        else:
                            st.positions.pop(k, None)
                st.at, st.error, delay = time.time(), "", 2.0
                st.connected = st.balance is not None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st.connected = False
                st.error = f"{type(e).__name__}: {str(e)[:160]}"
                st.failures += 1
                log.info("[acc %s] %s stream: %s (again in %.0fs)", st.acc_id, what, st.error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def start(self, acc: ExchangeAccount, version: tuple) -> None:
        st = AccountStream(acc.id, version)
        st.ex = self._client(acc)
        st.tasks = [asyncio.create_task(self._watch(st, "balance")), asyncio.create_task(self._watch(st, "positions"))]
        self.accounts[acc.id] = st

    async def stop(self, acc_id: int) -> None:
        st = self.accounts.pop(acc_id, None)
        if st is None:
            return
        for t in st.tasks:
            t.cancel()
        try:
            await st.ex.close()
        except Exception:
            pass

    # ------------------------------------------------------------ mark prices (public)
    async def _marks(self) -> None:
        delay = 2.0
        while True:
            try:
                if self._public is None:
                    if self.factory:
                        self._public = self.factory(None)
                    else:
                        import ccxt.pro as ccxtpro
                        from .ratelimit import governor
                        self._public = ccxtpro.binanceusdm({"options": {"defaultType": "future", "fetchCurrencies": False}})
                        governor.attach(self._public)
                        from .exchange import _shared_markets
                        hit = _shared_markets.get(("binanceusdm", False))
                        if hit:
                            self._public.set_markets(hit[1])
                tickers = await self._public.watch_mark_prices()
                now = time.time()
                for sym, t in (tickers or {}).items():
                    px = t.get("markPrice") or (t.get("info") or {}).get("p") or t.get("last")
                    if px:
                        self.marks[sym] = (now, float(px))
                self.mark_ok, delay = True, 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.mark_ok = False
                log.info("mark price stream: %s (again in %.0fs)", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    # ------------------------------------------------------------ which portfolios
    async def sync(self) -> None:
        from .exchange import ccxt_target, client_version
        with SessionLocal() as s:
            accs = list(s.scalars(select(ExchangeAccount).where(ExchangeAccount.kind == "exchange",
                                                                ExchangeAccount.enabled.is_(True),
                                                                ExchangeAccount.dry_run.is_(False),
                                                                ExchangeAccount.api_key_enc.is_not(None))))
            for a in accs:
                s.expunge(a)
        want = {a.id: a for a in accs if ccxt_target(a.exchange, a.trading_mode)[0] in SUPPORTED}
        for acc_id in [i for i in self.accounts if i not in want or self.accounts[i].version != client_version(want[i])]:
            await self.stop(acc_id)
        for acc_id, a in want.items():
            if acc_id not in self.accounts:
                try:
                    await self.start(a, client_version(a))
                except Exception as e:
                    log.warning("[acc %s] stream not started: %s", acc_id, e)
                await asyncio.sleep(START_GAP)  # the exchange limits new connections per IP

    async def loop(self) -> None:
        mark_task = asyncio.create_task(self._marks())
        try:
            while True:
                try:
                    await self.sync()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("stream sync failed")
                await asyncio.sleep(SYNC_EVERY)
        finally:
            mark_task.cancel()
            for acc_id in list(self.accounts):
                await self.stop(acc_id)
            if self._public is not None:
                try:
                    await self._public.close()
                except Exception:
                    pass

    def status(self, acc: ExchangeAccount) -> dict | None:
        """How this portfolio gets its numbers: {"mode": live | connecting | api, "error", "since"}; None when a
        stream doesn't apply (paper, signals, spot, other exchanges)."""
        from .exchange import ccxt_target
        if acc.kind != "exchange" or acc.dry_run or not acc.enabled or \
                ccxt_target(acc.exchange, acc.trading_mode)[0] not in SUPPORTED:
            return None
        st = self.accounts.get(acc.id)
        if st is None:
            return {"mode": "api", "error": "", "since": None, "ip": acc.out_ip}
        if self.live(acc.id):
            return {"mode": "live", "error": "", "since": st.at, "ip": acc.out_ip}
        return {"mode": "connecting" if not st.error else "api", "error": st.error, "since": st.at or None,
                "ip": acc.out_ip}

    def summary(self) -> dict:
        sts = list(self.accounts.values())
        return {"total": len(sts), "connected": sum(1 for s in sts if s.connected), "marks": len(self.marks),
                "mark_ok": self.mark_ok, "errors": [{"acc": s.acc_id, "error": s.error} for s in sts if s.error][:10]}


manager = StreamManager()
