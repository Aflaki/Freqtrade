"""Fans one signal out to every enabled account in parallel, with timeout + retries."""
from __future__ import annotations

import asyncio
import logging
import time

import ccxt.async_support as ccxt

from .config import Account, Settings
from .executors import CcxtExecutor, ExecResult, HttpExecutor
from .signal import Signal

log = logging.getLogger("relay.dispatch")

# errors that are worth retrying (network hiccups, rate limits); everything else fails fast
RETRYABLE = (ccxt.NetworkError, asyncio.TimeoutError, RuntimeError)
NON_RETRYABLE = (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.AuthenticationError, ccxt.BadSymbol)


class Dispatcher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.executors: dict[str, CcxtExecutor | HttpExecutor] = {}
        for acc in settings.accounts:
            if not acc.enabled:
                continue
            self.executors[acc.name] = CcxtExecutor(acc) if acc.type == "ccxt" else HttpExecutor(acc)
        self._seen: dict[str, float] = {}

    async def close(self) -> None:
        await asyncio.gather(*(e.close() for e in self.executors.values()), return_exceptions=True)

    def is_duplicate(self, sig: Signal) -> bool:
        now = time.monotonic()
        ttl = self.settings.dedupe_seconds
        self._seen = {k: t for k, t in self._seen.items() if now - t < ttl}
        if sig.id in self._seen:
            return True
        self._seen[sig.id] = now
        return False

    async def _run_one(self, name: str, ex, sig: Signal) -> ExecResult:
        acc: Account = ex.acc
        attempts = acc.retries + 1
        for i in range(1, attempts + 1):
            try:
                res = await asyncio.wait_for(ex.execute(sig), timeout=acc.timeout + 5)
                log.info("[%s] %s %s -> %s", name, sig.action, sig.symbol, res.message)
                return res
            except NON_RETRYABLE as e:
                log.error("[%s] failed: %s", name, e)
                return ExecResult(name, ok=False, message=f"{type(e).__name__}: {e}")
            except RETRYABLE as e:
                log.warning("[%s] attempt %d/%d failed: %s", name, i, attempts, e)
                if i == attempts:
                    return ExecResult(name, ok=False, message=f"{type(e).__name__}: {e}")
                await asyncio.sleep(min(2 ** (i - 1), 5))
            except Exception as e:
                log.exception("[%s] error", name)
                return ExecResult(name, ok=False, message=f"{type(e).__name__}: {e}")
        return ExecResult(name, ok=False, message="unreachable")

    async def dispatch(self, sig: Signal, only: list[str] | None = None) -> list[ExecResult]:
        targets = {n: e for n, e in self.executors.items() if not only or n in only}
        results = await asyncio.gather(*(self._run_one(n, e, sig) for n, e in targets.items()))
        return list(results)
