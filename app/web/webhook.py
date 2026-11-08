"""Freqtrade -> POST /webhook/<secret>. Every request is stored, including rejected ones."""
from __future__ import annotations

import hmac
import json
import logging
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from sqlalchemy import select

from ..db import Bot, SessionLocal, WebhookEvent, get_global, utcnow
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


def _find_bot(token: str) -> tuple[int, str, bool] | None:
    """Each bot has its own secret in its webhook URL -> tells us which bot sent the message."""
    if not token:
        return None
    with SessionLocal() as s:
        for b in s.scalars(select(Bot).where(Bot.enabled.is_(True))):
            if hmac.compare_digest(token.encode(), b.webhook_secret.encode()):
                return b.id, b.name, b.copy_enabled
    return None


async def _handle(request: Request, background: BackgroundTasks, token: str | None):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    body = await request.body()
    raw = body.decode("utf-8", "replace")[:20000]
    with SessionLocal() as s:
        gs = get_global(s)
        allowed_ips, source, relay_on, ttl = gs.allowed_ips, gs.signal_source, gs.relay_on, gs.dedupe_seconds

    def reject(code: int, why: str, payload: dict | None = None, bot_id: int | None = None):
        _store(ip=ip, raw=raw if code != 401 else "", payload={} if code == 401 else (payload or {}),
               status="rejected", error=why, bot_id=bot_id, finished_at=utcnow())
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
    found = _find_bot(token or request.headers.get("x-webhook-secret") or str(payload.get("passphrase", "")))
    if found is None:
        return reject(401, "invalid secret")
    bot_id, bot_name, copy_enabled = found
    payload.pop("passphrase", None)
    msg_type = str(payload.get("type", ""))[:30]
    base = dict(ip=ip, raw=raw, payload=payload, msg_type=msg_type, bot_id=bot_id)

    try:
        if source == "freqtrade":
            perf.record_from_webhook(payload, bot_id)  # public track record of this bot
            monitor.on_webhook(bot_id, payload)
            if msg_type in EVENT_TYPES:  # bot status / warnings / errors: log + tell admins, never trade
                _store(**base, status="event", pair=str(payload.get("pair") or "")[:40], finished_at=utcnow())
                monitor.forward_event(bot_name, payload)
                return {"status": "event", "type": msg_type, "bot": bot_name}
            sig = parse_freqtrade(payload, relay_on)
            if sig is None:
                _store(**base, status="ignored", finished_at=utcnow())
                return {"status": "ignored", "type": payload.get("type"), "bot": bot_name}
        else:
            sig = parse_signal(payload, {}, {}, {})
    except SignalError as e:
        return reject(422, str(e), payload, bot_id)

    sig.id = f"{bot_id}:{sig.id}"
    if is_duplicate(sig, ttl):
        _store(**base, status="duplicate", action=sig.action, pair=sig.pair or sig.symbol, finished_at=utcnow())
        return {"status": "duplicate", "id": sig.id}
    if not copy_enabled:  # recorded for the track record, but not copied to users
        _store(**base, status="ignored", action=sig.action, pair=sig.pair or sig.symbol,
               summary="copy disabled for this bot", finished_at=utcnow())
        return {"status": "ignored", "reason": "copy disabled", "bot": bot_name}
    event_id = _store(**base, status="accepted", action=sig.action, pair=sig.pair or sig.symbol)
    log.info("signal #%s [%s] %s %s frac=%s", event_id, bot_name, sig.action, sig.pair or sig.symbol, sig.close_fraction)
    if request.query_params.get("wait") in ("1", "true"):
        results = await dispatch(sig, event_id, bot_id, bot_name)
        return {"status": "done", "event": event_id, "bot": bot_name, "results": [r.__dict__ for r in results]}
    background.add_task(dispatch, sig, event_id, bot_id, bot_name)
    return {"status": "accepted", "event": event_id, "bot": bot_name}


@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    return await _handle(request, background, None)


@router.post("/webhook/{token}")
async def webhook_token(token: str, request: Request, background: BackgroundTasks):
    return await _handle(request, background, token)
