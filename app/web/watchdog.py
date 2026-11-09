"""Watchdog agents on the bot servers: download/installer, the per-minute report, alerts on restarts."""
from __future__ import annotations

import datetime as dt
import hmac
import secrets
import socket
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from ..db import Bot, SessionLocal, WatchdogEvent, WatchdogHost, get_global, utcnow
from ..engine.watchdog_agent import build_agent, build_install
from ..i18n import t

router = APIRouter()
SILENT_MINUTES = 5
_dns: dict[str, set[str]] = {}


def token() -> str:
    """The shared secret the agents send (created on first use)."""
    with SessionLocal() as s:
        gs = get_global(s)
        if not gs.watchdog_token:
            gs.watchdog_token = secrets.token_urlsafe(32)
            s.commit()
        return gs.watchdog_token


def site_url(request: Request) -> str:
    with SessionLocal() as s:
        base = get_global(s).base_url
    return (base or str(request.base_url)).rstrip("/")


def _check(sent: str | None) -> None:
    if not sent or not hmac.compare_digest(str(sent), token()):
        raise HTTPException(403, "bad watchdog token")


@router.get("/watchdog/install.sh")
async def install_sh(request: Request, t: str = ""):
    _check(t)
    return Response(build_install(site_url(request), token()), media_type="text/x-shellscript",
                    headers={"Cache-Control": "no-store"})


@router.get("/watchdog/agent.py")
async def agent_py(request: Request, t: str = ""):
    _check(t)
    return Response(build_agent(site_url(request), token()), media_type="text/x-python",
                    headers={"Cache-Control": "no-store"})


def _resolve(host: str) -> set[str]:
    if host not in _dns:
        try:
            _dns[host] = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
        except OSError:
            _dns[host] = {host}
    return _dns[host]


def bots_on_host(ips: list[str], s) -> dict[int, Bot]:
    """API port -> bot, for the bots whose REST API address points at this server."""
    ipset = set(ips)
    out: dict[int, Bot] = {}
    for b in s.scalars(select(Bot)):
        try:
            u = urlparse(b.api_url) if b.api_url else None
        except ValueError:
            continue
        if not u or not u.port or not u.hostname:
            continue
        if ipset & _resolve(u.hostname):
            out[u.port] = b
    return out


def _event_text(lang: str, ev: dict, bot_name: str, host: str) -> tuple[str, str] | None:
    """(title, message) for an event the admins should hear about; None for quiet ones."""
    from ..telegram import esc
    action, detail = ev.get("action"), str(ev.get("detail") or "")
    cause = t(lang, "wd_cause_oom") if ev.get("cause") == "oom" else ""
    where = f"[{esc(bot_name)}] ({esc(host)})"
    if action == "restarted":
        mins = max(1, round(int(ev.get("down_for") or 0) / 60))
        return (t(lang, "push_wd_restarted", bot=bot_name),
                f"♻️ {where} " + t(lang, "wd_restarted", min=mins) + (f"\n⚠️ {cause}" if cause else ""))
    if action == "restart_failed":
        return (t(lang, "push_wd_failed", bot=bot_name),
                f"❌ {where} " + t(lang, "wd_failed") + (f"\n⚠️ {cause}" if cause else "")
                + (f"\n<pre>{esc(detail[-700:])}</pre>" if detail else ""))
    if action == "gave_up":
        return (t(lang, "push_wd_gave_up", bot=bot_name), f"⛔ {where} " + t(lang, "wd_gave_up", detail=esc(detail)))
    if action == "recovered":
        return None
    return None


@router.post("/watchdog/report")
async def report(request: Request):
    _check(request.headers.get("x-watchdog-token"))
    try:
        data: dict[str, Any] = await request.json()
    except ValueError:
        raise HTTPException(400, "bad json")
    host = str(data.get("host") or "?")[:120]
    ips = [str(x)[:64] for x in (data.get("ips") or [])][:20]
    events = [e for e in (data.get("events") or []) if isinstance(e, dict)][:60]
    with SessionLocal() as s:
        h = s.scalars(select(WatchdogHost).where(WatchdogHost.hostname == host)).first()
        if h is None:
            h = WatchdogHost(hostname=host)
            s.add(h)
        was_silent = h.silent_alerted
        h.ips, h.version, h.last_seen, h.silent_alerted = ips, str(data.get("version", ""))[:20], utcnow(), False
        h.report = {"mem": data.get("mem") or {}, "load": data.get("load") or [],
                    "bots": [b for b in (data.get("bots") or []) if isinstance(b, dict)][:200]}
        by_port = bots_on_host(ips, s)
        names = {p: b.name for p, b in by_port.items()}
        bot_ids = {p: b.id for p, b in by_port.items()}
        disabled = sorted(p for p, b in by_port.items() if not b.auto_restart)
        for ev in events:
            port = ev.get("port") if isinstance(ev.get("port"), int) else None
            s.add(WatchdogEvent(host=host, port=port, bot_id=bot_ids.get(port), action=str(ev.get("action", ""))[:20],
                                detail=str(ev.get("detail") or "")[:4000]))
        s.commit()
    from ..engine.botmonitor import monitor
    if was_silent:
        monitor.notify_admins("bot_status", lambda L: "✅ " + t(L, "wd_host_back", host=host), critical=True,
                              title=lambda L: t(L, "wd_host_back", host=host), tag=f"host-{host}")
    for ev in events:
        port = ev.get("port") if isinstance(ev.get("port"), int) else None
        name = names.get(port) or (f"port {port}" if port else str(ev.get("key", "?")))
        if _event_text("en", ev, name, host) is None:
            continue
        monitor.notify_admins("bot_status", lambda L, ev=ev, name=name: _event_text(L, ev, name, host)[1], critical=True,
                              title=lambda L, ev=ev, name=name: _event_text(L, ev, name, host)[0], tag=f"bot-{name}")
    return JSONResponse({"ok": True, "interval": 60, "disabled_ports": disabled})


def check_silent_hosts() -> None:
    """Called every minute by the bot monitor: a server whose agent stopped reporting is probably down."""
    from ..engine.botmonitor import monitor
    limit = utcnow() - dt.timedelta(minutes=SILENT_MINUTES)
    with SessionLocal() as s:
        silent = list(s.scalars(select(WatchdogHost).where(WatchdogHost.last_seen < limit,
                                                           WatchdogHost.silent_alerted.is_(False))))
        for h in silent:
            h.silent_alerted = True
        s.commit()
        hosts = [(h.hostname, h.last_seen) for h in silent]
    for name, seen in hosts:
        monitor.notify_admins("bot_status", lambda L, n=name, w=seen: "🚨 " + t(L, "wd_host_silent", host=n,
                                                                                 since=w.strftime("%H:%M UTC")),
                              critical=True, title=lambda L, n=name: t(L, "push_wd_host", host=n), tag=f"host-{name}")
