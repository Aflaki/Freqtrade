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
VERIFY_EVERY = 120     # seconds: the stream's positions and balance are checked against a REST read this often
ECHO_WAIT = 10         # seconds: after our own order the stream must report that position change within this
VERIFY_FIRST = 5       # seconds after the start before the first check
VERIFY_GRACE = 90      # a stream not verified for VERIFY_EVERY + this is not trusted (readers fall back to REST)


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
        self.pos_ready = False  # the positions snapshot has arrived
        self.verified_at = 0.0  # last time a REST read agreed with (or replaced) the stream's state
        self.drifts = 0         # times the stream had drifted from the exchange (it is then reconnected)
        self.last_drift = ""
        self.started = time.time()
        self.pos_events = 0       # position updates pushed by the exchange since the connection was made
        self.expect: dict[str, float] = {}  # symbol -> deadline: our own order, not yet echoed by the stream
        self.echo_misses = 0
        self.last_pos_event = 0.0


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
        now = time.time()
        ok = st and st.connected and st.balance is not None and st.pos_ready and \
            now - st.verified_at < VERIFY_EVERY + VERIFY_GRACE and \
            not any(d < now for d in st.expect.values())  # our order wasn't echoed in time: not trusted
        return st if ok else None

    def expect(self, acc_id: int, symbol: str) -> None:
        """We just traded `symbol` on this account: the exchange must push that position change on the stream
        within ECHO_WAIT s, or the stream is taken as frozen and checked against REST at once."""
        st = self.accounts.get(acc_id)
        if st is not None and st.connected and st.pos_ready:
            st.expect.setdefault(symbol, time.time() + ECHO_WAIT)

    def unexpect(self, acc_id: int, symbol: str) -> None:
        """The order was refused: nothing to echo."""
        st = self.accounts.get(acc_id)
        if st is not None:
            st.expect.pop(symbol, None)

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
                                              "fetchBalanceSnapshot": False, "awaitBalanceSnapshot": False,
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
                    if st.balance is None:  # the balance as it is now: the stream itself only reports changes
                        st.balance = await st.ex.fetch_balance()
                    else:
                        st.balance = await st.ex.watch_balance()
                else:
                    got = await st.ex.watch_positions() or []
                    if st.pos_ready:
                        st.pos_events += 1
                        st.last_pos_event = time.time()
                    for p in got:
                        st.expect.pop(p.get("symbol"), None)  # the echo of our own order (closed ones too)
                        k = _key(p)
                        if not k:
                            continue
                        prev = st.positions.get(k, {})
                        merged = {**prev, **{kk: vv for kk, vv in p.items() if vv is not None}}  # keep leverage etc.
                        if abs(float(merged.get("contracts") or 0)) > 0:
                            st.positions[k] = merged
                        else:
                            st.positions.pop(k, None)
                    st.pos_ready = True  # the first answer is the full snapshot
                    if st.balance is None:  # reconnected here first: read the balance again too
                        st.balance = await st.ex.fetch_balance()
                st.at, st.error, delay = time.time(), "", 2.0
                st.connected = st.balance is not None and st.pos_ready
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # whatever changed while disconnected is unknown: start again from fresh snapshots
                st.connected, st.pos_ready, st.balance = False, False, None
                st.positions.clear()
                cache = getattr(st.ex, "positions", None)
                if isinstance(cache, dict):
                    cache.clear()  # makes ccxt load a positions snapshot again
                st.error = f"{type(e).__name__}: {str(e)[:160]}"
                st.failures += 1
                log.info("[acc %s] %s stream: %s (again in %.0fs)", st.acc_id, what, st.error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def _verify(self, st: AccountStream) -> None:
        """Every VERIFY_EVERY s: read positions and balance over REST and compare. A user-data stream can stop
        delivering events without disconnecting; then its state is replaced by the REST read and the
        connection is rebuilt (by sync)."""
        from .ratelimit import prio
        await asyncio.sleep(VERIFY_FIRST)
        while True:
            missed = []
            if st.connected and st.pos_ready:
                now = time.time()
                missed = sorted(k for k, d in st.expect.items() if d < now)
                for k in missed:
                    st.expect.pop(k, None)
                if missed:
                    st.echo_misses += 1
                    log.warning("[acc %s] our order on %s wasn't echoed by the stream within %d s: checking now",
                                st.acc_id, ", ".join(m.split(":")[0] for m in missed), ECHO_WAIT)
                tok = prio.set("background")
                try:
                    raw = await st.ex.fetch_positions()
                    bal = await st.ex.fetch_balance()
                    real = {}
                    for p in raw or []:
                        k = _key(p)
                        if k and abs(float(p.get("contracts") or 0)) > 0:
                            real[k] = p
                    have = {k: abs(float(p.get("contracts") or 0)) for k, p in st.positions.items()}
                    want = {k: abs(float(p.get("contracts") or 0)) for k, p in real.items()}
                    diff = sorted(k for k in set(have) | set(want)
                                  if abs(have.get(k, 0) - want.get(k, 0)) > 1e-9 * max(1.0, want.get(k, 0)))
                    st.positions = {k: {**st.positions.get(k, {}), **{kk: vv for kk, vv in p.items() if vv is not None}}
                                    for k, p in real.items()}
                    st.balance = bal
                    st.verified_at = time.time()
                    if diff:
                        st.drifts += 1
                        st.last_drift = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC: " + \
                            ", ".join(f"{k.split('|')[0].split(':')[0]} {k.split('|')[1]}" for k in diff[:12]) + \
                            (f" (+{len(diff) - 12})" if len(diff) > 12 else "")
                        now = time.time()
                        log.warning("[acc %s] stream had drifted from the exchange (%d positions: %s); connected "
                                    "%.0f min ago, %d position updates received, last one %s; replaced by REST and "
                                    "reconnecting", st.acc_id, len(diff), st.last_drift, (now - st.started) / 60,
                                    st.pos_events, f"{(now - st.last_pos_event) / 60:.0f} min ago"
                                    if st.last_pos_event else "never")
                        st.version = ("drift", time.time())  # sync() rebuilds the connection
                    elif missed:
                        log.info("[acc %s] stream state matches the exchange after all (late echo)", st.acc_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.info("[acc %s] stream check skipped: %s", st.acc_id, e)
                finally:
                    prio.reset(tok)
            until = time.time() + (VERIFY_EVERY if st.verified_at else VERIFY_FIRST or 0.01)
            while time.time() < until:  # wake early when one of our orders isn't echoed in time
                await asyncio.sleep(min(1.0, max(0.01, until - time.time())))
                now = time.time()
                if any(d < now for d in st.expect.values()):
                    break

    async def start(self, acc: ExchangeAccount, version: tuple) -> None:
        st = AccountStream(acc.id, version)
        st.ex = self._client(acc)
        st.tasks = [asyncio.create_task(self._watch(st, "balance")), asyncio.create_task(self._watch(st, "positions")),
                    asyncio.create_task(self._verify(st))]
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
            return {"mode": "live", "error": "", "since": st.at, "ip": acc.out_ip, "drifts": st.drifts,
                    "last_drift": st.last_drift}
        waiting = [w for w, ok in (("balance", st.balance is not None), ("positions", st.pos_ready),
                                   ("check", time.time() - st.verified_at < VERIFY_EVERY + VERIFY_GRACE)) if not ok]
        return {"mode": "connecting" if not st.error else "api",
                "error": st.error or ("waiting for: " + ", ".join(waiting) if waiting else ""),
                "since": st.at or None, "ip": acc.out_ip}

    def summary(self) -> dict:
        sts = list(self.accounts.values())
        return {"total": len(sts), "connected": sum(1 for s in sts if s.connected), "marks": len(self.marks),
                "mark_ok": self.mark_ok, "errors": [{"acc": s.acc_id, "error": s.error} for s in sts if s.error][:10],
                "drifts": sum(s.drifts for s in sts),
                "drift_list": [{"acc": s.acc_id, "n": s.drifts, "last": s.last_drift} for s in sts if s.drifts][:10]}


manager = StreamManager()
