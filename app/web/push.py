"""Phone alerts: Web Push subscriptions (service worker at /sw.js) and the alert-bot test."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from ..db import PushSubscription, SessionLocal, User
from ..notify import alerts, webpush
from ..security import flash, form_with_csrf, require_user
from .common import tr

router = APIRouter()

SW = """// Signal Relay service worker: shows push notifications, opens the page on tap.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {title: 'Signal Relay', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Signal Relay', {
    body: d.body || '', tag: d.tag || undefined, renotify: !!d.tag, data: {url: d.url || '/'},
    icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png'}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    return self.clients.openWindow(url);
  }));
});
"""


@router.get("/sw.js")
async def service_worker():
    return Response(SW, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


async def _json_with_csrf(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "bad json")
    sent = str((data or {}).get("csrf", ""))
    if not sent or not hmac.compare_digest(sent, request.session.get("csrf", "")):
        raise HTTPException(400, "CSRF token invalid - reload the page")
    return data


@router.get("/push/key")
async def push_key(request: Request):
    require_user(request)
    return {"key": webpush.public_key()}


@router.post("/push/subscribe")
async def push_subscribe(request: Request):
    user = require_user(request)
    data = await _json_with_csrf(request)
    sub = data.get("subscription") or {}
    endpoint, keys = str(sub.get("endpoint", "")), sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "invalid subscription")
    with SessionLocal() as s:
        x = s.scalars(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
        if x is None:
            x = PushSubscription(endpoint=endpoint)
            s.add(x)
        x.user_id, x.p256dh, x.auth = user.id, str(keys["p256dh"])[:200], str(keys["auth"])[:100]
        x.device, x.last_error = str(data.get("device", ""))[:120], ""
        s.commit()
    return JSONResponse({"ok": True})


@router.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    user = require_user(request)
    data = await _json_with_csrf(request)
    with SessionLocal() as s:
        for x in s.scalars(select(PushSubscription).where(PushSubscription.user_id == user.id,
                                                           PushSubscription.endpoint == str(data.get("endpoint", "")))):
            s.delete(x)
        s.commit()
    return JSONResponse({"ok": True})


@router.post("/push/remove/{sub_id}")
async def push_remove(sub_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        x = s.get(PushSubscription, sub_id)
        if x is not None and x.user_id == user.id:
            s.delete(x)
            s.commit()
    return RedirectResponse("/settings#phone", 303)


@router.post("/push/test")
async def push_test(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
    res = await alerts.phone(u, tr(request, "push_test_title"), tr(request, "push_test_body"), url="/settings#phone",
                             tag="test")
    parts = []
    if res["push"]:
        parts.append(tr(request, "push_test_ok", n=res["push"]))
    if res["alert_bot"]:
        parts.append(tr(request, "alertbot_test_ok"))
    if parts:
        flash(request, " · ".join(parts))
    else:
        flash(request, tr(request, "push_test_none"), "error")
    return RedirectResponse("/settings#phone", 303)
