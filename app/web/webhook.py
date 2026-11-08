"""Freqtrade -> POST /webhook/<WEBHOOK_SECRET>"""
from __future__ import annotations

import hmac
import json
import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ..engine.dispatcher import dispatch, is_duplicate
from ..settings import config
from ..signal import SignalError, parse_freqtrade, parse_signal

router = APIRouter()
log = logging.getLogger("relay.webhook")


async def _payload(request: Request) -> dict:
    body = await request.body()
    if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "body must be JSON or form-encoded")
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")
    return data


async def _handle(request: Request, background: BackgroundTasks, token: str | None):
    if config.allowed_ips:
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))
        if ip not in config.allowed_ips:
            raise HTTPException(403, "ip not allowed")
    payload = await _payload(request)
    supplied = token or request.headers.get("x-webhook-secret") or str(payload.get("passphrase", ""))
    if not hmac.compare_digest(supplied.encode(), config.webhook_secret.encode()):
        raise HTTPException(401, "invalid secret")
    try:
        if config.signal_source == "freqtrade":
            sig = parse_freqtrade(payload, config.freqtrade_relay_on)
            if sig is None:
                return {"status": "ignored", "type": payload.get("type")}
        else:
            sig = parse_signal(payload, {}, {}, {})
    except SignalError as e:
        raise HTTPException(422, str(e))
    if is_duplicate(sig, config.dedupe_seconds):
        return {"status": "duplicate", "id": sig.id}
    log.info("signal %s %s %s frac=%s stake=%s", sig.id, sig.action, sig.pair or sig.symbol,
             sig.close_fraction, sig.stake)
    if request.query_params.get("wait") in ("1", "true"):
        results = await dispatch(sig)
        return {"status": "done", "results": [r.__dict__ for r in results]}
    background.add_task(dispatch, sig)
    return {"status": "accepted", "id": sig.id}


@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    return await _handle(request, background, None)


@router.post("/webhook/{token}")
async def webhook_token(token: str, request: Request, background: BackgroundTasks):
    return await _handle(request, background, token)
