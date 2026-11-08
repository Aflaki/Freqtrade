"""Freqtrade -> POST /webhook/<secret>. Every request is stored, including rejected ones."""
from __future__ import annotations

import hmac
import json
import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from ..db import SessionLocal, WebhookEvent, get_global, utcnow
from ..engine import performance as perf
from ..engine.botmonitor import EVENT_TYPES, monitor
from ..engine.dispatcher import dispatch, is_duplicate
from ..signal import SignalError, parse_freqtrade, parse_signal

router = APIRouter()
log = logging.getLogger("relay.webhook")


def _store(**kw) -> int:
    with SessionLocal() as s:
        e = WebhookEvent(**kw)
        s.add(e)
        s.commit()
        return e.id


async def _handle(request: Request, background: BackgroundTasks, token: str | None):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    body = await request.body()
    raw = body.decode("utf-8", "replace")[:20000]
    with SessionLocal() as s:
        gs = get_global(s)
        secret, allowed_ips, source, relay_on, ttl = (gs.webhook_secret, gs.allowed_ips, gs.signal_source,
                                                      gs.relay_on, gs.dedupe_seconds)

    def reject(code: int, why: str, payload: dict | None = None):
        _store(ip=ip, raw=raw if code != 401 else "", payload={} if code == 401 else (payload or {}),
               status="rejected", error=why, finished_at=utcnow())
        return JSONResponse({"detail": why}, status_code=code)

    if allowed_ips and ip not in allowed_ips:
        return reject(403, "ip not allowed")
    if "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
        payload = dict(parse_qsl(raw, keep_blank_values=True))
    else:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            return reject(400, "body must be a JSON object or form-encoded")
    supplied = token or request.headers.get("x-webhook-secret") or str(payload.get("passphrase", ""))
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        return reject(401, "invalid secret")
    payload.pop("passphrase", None)
    msg_type = str(payload.get("type", ""))[:30]

    try:
        if source == "freqtrade":
            perf.record_from_webhook(payload, None)  # public track record, independent of users
            monitor.on_webhook(payload)
            if msg_type in EVENT_TYPES:  # bot status / warnings / errors: log + tell admins, never trade
                _store(ip=ip, raw=raw, payload=payload, msg_type=msg_type, status="event",
                       pair=str(payload.get("pair") or "")[:40], finished_at=utcnow())
                monitor.forward_event(payload)
                return {"status": "event", "type": msg_type}
            sig = parse_freqtrade(payload, relay_on)
            if sig is None:
                _store(ip=ip, raw=raw, payload=payload, msg_type=msg_type, status="ignored", finished_at=utcnow())
                return {"status": "ignored", "type": payload.get("type")}
        else:
            sig = parse_signal(payload, {}, {}, {})
    except SignalError as e:
        return reject(422, str(e), payload)

    if is_duplicate(sig, ttl):
        _store(ip=ip, raw=raw, payload=payload, msg_type=msg_type, status="duplicate", action=sig.action,
               pair=sig.pair or sig.symbol, finished_at=utcnow())
        return {"status": "duplicate", "id": sig.id}
    event_id = _store(ip=ip, raw=raw, payload=payload, msg_type=msg_type, status="accepted", action=sig.action,
                      pair=sig.pair or sig.symbol)
    log.info("signal #%s %s %s frac=%s", event_id, sig.action, sig.pair or sig.symbol, sig.close_fraction)
    if request.query_params.get("wait") in ("1", "true"):
        results = await dispatch(sig, event_id)
        return {"status": "done", "event": event_id, "results": [r.__dict__ for r in results]}
    background.add_task(dispatch, sig, event_id)
    return {"status": "accepted", "event": event_id}


@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    return await _handle(request, background, None)


@router.post("/webhook/{token}")
async def webhook_token(token: str, request: Request, background: BackgroundTasks):
    return await _handle(request, background, token)
