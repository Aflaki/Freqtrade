"""FastAPI app: receives webhooks and relays them to all configured exchanges / APIs."""
from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .config import load_settings
from .dispatcher import Dispatcher
from .signal import DEFAULT_FIELDS, SignalError, parse_freqtrade, parse_signal

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("relay")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.dispatcher = Dispatcher(settings)
    log.info("loaded %d active account(s): %s", len(app.state.dispatcher.executors),
             ", ".join(app.state.dispatcher.executors) or "-")
    yield
    await app.state.dispatcher.close()


app = FastAPI(title="Signal Relay", lifespan=lifespan)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def _check_auth(request: Request, path_token: str | None, payload: dict) -> None:
    s = request.app.state.settings
    if s.allowed_ips and _client_ip(request) not in s.allowed_ips:
        raise HTTPException(403, "ip not allowed")
    pass_field = {**DEFAULT_FIELDS, **s.field_map}["passphrase"]
    supplied = path_token or request.headers.get("x-webhook-secret") or str(payload.get(pass_field, ""))
    if not hmac.compare_digest(supplied.encode(), s.secret.encode()):
        raise HTTPException(401, "invalid secret")


async def _read_payload(request: Request) -> dict:
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ctype:
        # Freqtrade's default webhook format is "form"
        return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "body must be JSON or form-encoded")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")
    return data


async def _handle(request: Request, background: BackgroundTasks, token: str | None):
    payload = await _read_payload(request)
    _check_auth(request, token, payload)
    s = request.app.state.settings
    disp: Dispatcher = request.app.state.dispatcher
    try:
        if s.source == "freqtrade":
            sig = parse_freqtrade(payload, s.freqtrade_relay_on)
            if sig is None:
                # status/cancel/etc. -> acknowledge with 200 so Freqtrade doesn't retry
                return {"status": "ignored", "type": payload.get("type")}
        else:
            sig = parse_signal(payload, s.field_map, s.action_aliases, s.symbol_cleanup)
    except SignalError as e:
        raise HTTPException(422, str(e))

    if disp.is_duplicate(sig):
        log.info("duplicate signal %s ignored", sig.id)
        return {"status": "duplicate", "id": sig.id}

    only = request.query_params.get("accounts")
    only_list = [a.strip() for a in only.split(",")] if only else None
    log.info("signal %s: %s %s price=%s x%s close_frac=%s stake=%s", sig.id, sig.action, sig.symbol,
             sig.price, sig.size_multiplier, sig.close_fraction, sig.stake)

    # ?wait=1 -> respond with per-account results (useful for testing).
    # Default: respond immediately so the signal sender never times out.
    if request.query_params.get("wait") in ("1", "true"):
        results = await disp.dispatch(sig, only_list)
        return {"status": "done", "signal": sig.to_dict(), "results": [r.to_dict() for r in results]}
    background.add_task(disp.dispatch, sig, only_list)
    return {"status": "accepted", "id": sig.id}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    return await _handle(request, background, None)


@app.post("/webhook/{token}")
async def webhook_with_token(token: str, request: Request, background: BackgroundTasks):
    return await _handle(request, background, token)


@app.get("/health")
async def health(request: Request):
    return {"status": "ok", "accounts": list(request.app.state.dispatcher.executors)}
