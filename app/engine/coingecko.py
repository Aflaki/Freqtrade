"""One CoinGecko client for the whole app: paced and retried, so the free limits are never exceeded.

Without a key CoinGecko allows only ~5 calls/minute per IP (shared by everything on this server); with the free
"Demo" key (Admin -> Performance data) ~30/minute. Calls wait for their turn instead of failing with 429.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..db import SessionLocal, get_global
from ..security import decrypt

BASE = "https://api.coingecko.com/api/v3"
GAP_KEYLESS = 13.0  # seconds between calls without a key (5/min)
GAP_DEMO = 2.1      # with a demo key (~30/min)
_lock = asyncio.Lock()
_last = 0.0


def _key() -> str:
    with SessionLocal() as s:
        return decrypt(get_global(s).coingecko_key_enc) or ""


async def get(path: str, params: dict | None = None, retries: int = 3) -> Any:
    global _last
    key = _key()
    gap = GAP_DEMO if key else GAP_KEYLESS
    headers = {"accept": "application/json", **({"x-cg-demo-api-key": key} if key else {})}
    for attempt in range(retries + 1):
        async with _lock:  # one call at a time, spaced out
            wait = _last + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(timeout=30, headers=headers) as c:
                    r = await c.get(BASE + path, params=params or {})
            finally:
                _last = time.monotonic()
        if r.status_code == 429 and attempt < retries:  # rate limited: wait as told (or a minute) and try again
            try:
                delay = float(r.headers.get("retry-after") or 0)
            except ValueError:
                delay = 0
            await asyncio.sleep(max(delay, 61 if not key else 15))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()
