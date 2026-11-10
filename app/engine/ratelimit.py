"""One request budget per exchange API for this whole server.

Every user's API key is called from this server's IP, and exchanges limit requests per IP (Binance futures:
2400 "weight" per minute, shared by all keys). ccxt's own rate limiter only spaces the calls of ONE client (one
user account), so with many accounts it can't protect the shared limit. This governor sits under every ccxt
client (its `throttle(cost)` hook gets the exact weight of each HTTP request) and:

  - counts the weight used in the last 60 s, and reads what the exchange itself says (X-MBX-USED-WEIGHT-1M);
  - gives each kind of work a ceiling, as a share of the budget (the admin sets the budget, default 80% of the
    exchange's limit):  trade 100% (orders, exits, entry checks) > check 85% (API key checks) > view 60%
    (live prices on pages) > background 40% (open-time lookups, scans);
  - work above its ceiling waits (trade, check) or is skipped and served from cache (view, background), so a
    busy minute slows the pages down instead of getting the IP banned;
  - after a 429 everything pauses for the time the exchange asks; after a 418 (IP ban) every call to that
    exchange stops until the ban ends, and the admins are alerted.
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger("relay.ratelimit")

# weight per minute per IP (Binance: weight; others: conservative request counts)
LIMITS = {"binanceusdm": 2400, "binancecoinm": 2400, "binance": 6000, "bybit": 6000, "okx": 3000, "bitget": 3000,
          "kucoinfutures": 2000, "kucoin": 2000, "gate": 3000, "mexc": 1200, "htx": 3000, "bingx": 1200}
DEFAULT_LIMIT = 1200
WEIGHT_HEADER = "x-mbx-used-weight-1m"
SHARE = {"exit": 1.0, "trade": 0.9, "check": 0.8, "view": 0.6, "background": 0.4}  # exits keep 10% to themselves
MAX_WAIT = {"exit": 120.0, "trade": 60.0, "check": 20.0, "view": 0.0, "background": 30.0}

prio: contextvars.ContextVar[str] = contextvars.ContextVar("api_prio", default="trade")


class ApiBusy(Exception):
    """The budget for this kind of work is used up right now (view/background: use the cache, try later)."""


class ApiBanned(Exception):
    """The exchange banned this server's IP; no calls until the ban ends."""


class _Group:
    def __init__(self, gid: str):
        self.gid = gid
        self.limit = LIMITS.get(gid.split("@")[0], DEFAULT_LIMIT)
        self.calls: deque[tuple[float, int, str]] = deque()  # (time, weight, kind) in the last 60 s
        self.server_used: int | None = None
        self.server_at = 0.0
        self.paused_until = 0.0
        self.banned_until = 0.0
        self.minutes: deque[tuple[int, int, int]] = deque(maxlen=60)  # (minute, our weight, peak server weight)
        self.by_kind: dict[str, int] = {}  # weight per kind, this process' life
        self.waited: dict[str, int] = {}
        self.skipped: dict[str, int] = {}
        self.errors: list[dict] = []  # last 429/418

    def _trim(self, now: float) -> None:
        while self.calls and now - self.calls[0][0] > 60:
            self.calls.popleft()

    def used(self, now: float) -> int:
        self._trim(now)
        own = sum(w for _, w, _ in self.calls)
        server = self.server_used if self.server_used is not None and int(now // 60) == int(self.server_at // 60) else 0
        return max(own, server or 0)

    def _minute(self, now: float, weight: int) -> None:
        m = int(now // 60)
        if self.minutes and self.minutes[-1][0] == m:
            _, w, srv = self.minutes[-1]
            self.minutes[-1] = (m, w + weight, srv)
        else:
            self.minutes.append((m, weight, 0))


class Governor:
    def __init__(self) -> None:
        self.groups: dict[str, _Group] = {}
        self.budget_pct = 80.0
        self._alert = None  # set by the app: called once per ban

    def group(self, gid: str) -> _Group:
        g = self.groups.get(gid)
        if g is None:
            g = self.groups[gid] = _Group(gid)
        return g

    def ceiling(self, g: _Group, kind: str) -> float:
        return g.limit * self.budget_pct / 100 * SHARE.get(kind, 1.0)

    async def acquire(self, gid: str, cost: float | None, kind: str | None = None) -> None:
        kind = kind or prio.get()
        g = self.group(gid)
        w = max(1, int(round(cost or 1)))
        start = time.monotonic()
        waited = False
        while True:
            now = time.time()
            if g.banned_until > now:
                g.skipped[kind] = g.skipped.get(kind, 0) + 1
                raise ApiBanned(f"{gid}: IP banned by the exchange until "
                                f"{dt.datetime.fromtimestamp(g.banned_until, dt.UTC):%H:%M} UTC")
            pause = g.paused_until - now
            room = g.used(now) + w <= self.ceiling(g, kind)
            if pause <= 0 and room:
                g.calls.append((now, w, kind))
                g._minute(now, w)
                g.by_kind[kind] = g.by_kind.get(kind, 0) + w
                if waited:
                    g.waited[kind] = g.waited.get(kind, 0) + 1
                return
            if time.monotonic() - start >= MAX_WAIT.get(kind, 0):
                if kind in ("view", "background", "check"):  # exits and entries never give up silently
                    g.skipped[kind] = g.skipped.get(kind, 0) + 1
                    raise ApiBusy(f"{gid}: request budget for {kind} is used up right now")
                # trading never gives up silently: go on (the exchange may answer 429, handled below)
                g.calls.append((now, w, kind))
                g._minute(now, w)
                g.by_kind[kind] = g.by_kind.get(kind, 0) + w
                return
            waited = True
            if pause > 0:
                await asyncio.sleep(min(pause, 1.0))
            else:
                oldest = g.calls[0][0] if g.calls else now
                await asyncio.sleep(max(0.05, min(1.0, 60 - (now - oldest))))

    def observe(self, gid: str, headers: Any) -> None:
        if not headers:
            return
        try:
            low = {str(k).lower(): v for k, v in dict(headers).items()}
        except Exception:
            return
        v = low.get(WEIGHT_HEADER)
        if v is None:
            return
        try:
            used = int(v)
        except (TypeError, ValueError):
            return
        g = self.group(gid)
        now = time.time()
        g.server_used, g.server_at = used, now
        if g.minutes and g.minutes[-1][0] == int(now // 60):
            m, w, srv = g.minutes[-1]
            g.minutes[-1] = (m, w, max(srv, used))

    def penalize(self, gid: str, headers: Any, banned: bool, message: str = "") -> None:
        g = self.group(gid)
        now = time.time()
        low = {}
        try:
            low = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        except Exception:
            pass
        try:
            retry = float(low.get("retry-after") or 0)
        except (TypeError, ValueError):
            retry = 0
        if banned:
            until = now + (retry or 300)
            first = g.banned_until <= now
            g.banned_until = max(g.banned_until, until)
            g.errors = (g.errors + [{"at": now, "code": 418, "until": g.banned_until, "msg": message[:200]}])[-10:]
            log.error("%s: IP banned until %s", gid, dt.datetime.fromtimestamp(g.banned_until, dt.UTC))
            if first and self._alert:
                try:
                    self._alert(gid, g.banned_until)
                except Exception:
                    log.exception("ban alert failed")
        else:
            g.paused_until = max(g.paused_until, now + (retry or 10))
            g.errors = (g.errors + [{"at": now, "code": 429, "until": g.paused_until, "msg": message[:200]}])[-10:]

    def attach(self, ex, ip: str = "") -> None:
        """Put the governor under one ccxt client (async_support). Each outgoing IP has its own budget."""
        if getattr(ex, "_governed", False) or not hasattr(ex, "throttle") or not hasattr(ex, "fetch"):
            return  # already done, or not a real ccxt client (test stand-ins)
        gid = (getattr(ex, "id", "") or type(ex).__name__.lower()) + (f"@{ip}" if ip else "")
        orig_throttle, orig_fetch = ex.throttle, ex.fetch
        gov = self

        async def throttle(cost=None):
            await gov.acquire(gid, cost)
            return await orig_throttle(cost)

        async def fetch(*a, **k):
            try:
                return await orig_fetch(*a, **k)
            except Exception as e:
                name = type(e).__name__
                if name in ("DDoSProtection", "RateLimitExceeded") or " 418 " in f" {e} " or " 429 " in f" {e} ":
                    banned = name == "DDoSProtection" or "418" in str(e) or "banned" in str(e).lower()
                    gov.penalize(gid, getattr(ex, "last_response_headers", None), banned, str(e))
                raise
            finally:
                gov.observe(gid, getattr(ex, "last_response_headers", None))

        ex.throttle, ex.fetch = throttle, fetch
        ex.enableRateLimit = True  # ccxt calls throttle() only when this is on
        ex._governed = True

    def snapshot(self) -> list[dict]:
        now = time.time()
        out = []
        for gid, g in sorted(self.groups.items()):
            used = g.used(now)
            per_kind: dict[str, int] = {}
            for _, w, k in g.calls:
                per_kind[k] = per_kind.get(k, 0) + w
            out.append({"gid": gid, "limit": g.limit, "budget": int(g.limit * self.budget_pct / 100), "used": used,
                        "server_used": g.server_used if int(now // 60) == int(g.server_at // 60) else None,
                        "per_kind": per_kind, "waited": dict(g.waited), "skipped": dict(g.skipped),
                        "ceilings": {k: int(self.ceiling(g, k)) for k in SHARE},
                        "banned_until": g.banned_until if g.banned_until > now else None,
                        "paused_until": g.paused_until if g.paused_until > now else None,
                        "minutes": [{"m": m, "own": w, "server": srv} for m, w, srv in g.minutes],
                        "errors": list(g.errors)})
        return out


governor = Governor()


class use:
    """with ratelimit.use("view"): ... — the kind of work for calls made inside (async code included)."""

    def __init__(self, kind: str):
        self.kind = kind

    def __enter__(self):
        self.tok = prio.set(self.kind)
        return self

    def __exit__(self, *exc):
        prio.reset(self.tok)
        return False
