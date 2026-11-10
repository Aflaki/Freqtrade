"""Watchdog agents on the bot servers: download/installer, the per-minute report, alerts on restarts."""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import secrets
import socket
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from ..db import Bot, ServerSample, SessionLocal, WatchdogEvent, WatchdogHost, get_global, utcnow
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


def host_labels() -> dict[str, str]:
    """hostname -> the admin's name for it (or the hostname)."""
    with SessionLocal() as s:
        return {h.hostname: (h.alias or h.hostname) for h in s.scalars(select(WatchdogHost))}


def host_label(hostname: str) -> str:
    return host_labels().get(hostname, hostname)


def _check(sent: str | None) -> None:
    if not sent or not hmac.compare_digest(str(sent), token()):
        raise HTTPException(403, "bad watchdog token")


@router.get("/watchdog/install.sh")
async def install_sh(request: Request, t: str = ""):
    _check(t)
    return Response(build_install(site_url(request), token()), media_type="text/x-shellscript",
                    headers={"Cache-Control": "no-store"})


@router.get("/watchdog/webhooks.py")
async def webhooks_py(request: Request, t: str = ""):
    """The webhook script, fetched straight onto a bot server with the watchdog key:
    curl -fsSL "<site>/watchdog/webhooks.py?t=<key>" | python3 - --apply"""
    _check(t)
    from .admin_bots import _all_bots, _webhook_template
    from .export_script import build_script
    bots, base = _all_bots()
    with SessionLocal() as s:
        site = get_global(s).site_name
    return Response(build_script(site, site_url(request), bots, _webhook_template()), media_type="text/x-python",
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


def _sample(host: str, rep: dict) -> ServerSample:
    mem, load = rep.get("mem") or {}, rep.get("load") or []

    def pct(used, total):
        return round(100.0 * used / total, 1) if total else None
    weights = [100.0 * v["used_weight"] / v["limit"] for v in (rep.get("binance") or {}).values()
               if v.get("used_weight") is not None and v.get("limit")]
    return ServerSample(host=host, load1=load[0] if load else None,
                        mem_pct=pct((mem.get("MemTotal") or 0) - (mem.get("MemAvailable") or 0), mem.get("MemTotal")),
                        swap_pct=pct((mem.get("SwapTotal") or 0) - (mem.get("SwapFree") or 0), mem.get("SwapTotal")),
                        disk_pct=(rep.get("host") or {}).get("disk_pct"),
                        weight_pct=round(max(weights), 1) if weights else None,
                        bots_running=sum(1 for b in rep.get("bots") or [] if b.get("status") == "running"))


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
    if action == "stopped":
        return (t(lang, "push_wd_stopped", bot=bot_name), f"⏸ {where} " + t(lang, "wd_stopped", detail=esc(detail)))
    return None


@router.post("/watchdog/ping")
async def ping(request: Request):
    """Connection test used by the installer (`agent.py test`)."""
    _check(request.headers.get("x-watchdog-token"))
    return JSONResponse({"ok": True})


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
        binance = {k: v for k, v in (data.get("binance") or {}).items() if isinstance(v, dict)}
        h.report = {"mem": data.get("mem") or {}, "load": data.get("load") or [], "binance": binance,
                    "host": data.get("host_info") or {},
                    "bots": [b for b in (data.get("bots") or []) if isinstance(b, dict)][:200]}
        s.add(_sample(host, h.report))
        if utcnow().minute == 0:  # once an hour: keep 7 days of samples
            s.query(ServerSample).filter(ServerSample.at < utcnow() - dt.timedelta(days=7)).delete()
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
    label = host_label(host)
    if was_silent:
        monitor.notify_admins("monitoring", lambda L: "✅ " + t(L, "wd_host_back", host=label), critical=True,
                              title=lambda L: t(L, "wd_host_back", host=label), tag=f"host-{host}")
    for ev in events:
        port = ev.get("port") if isinstance(ev.get("port"), int) else None
        name = names.get(port) or (f"port {port}" if port else str(ev.get("key", "?")))
        if _event_text("en", ev, name, host) is None:
            continue
        monitor.notify_admins("monitoring", lambda L, ev=ev, name=name: _event_text(L, ev, name, label)[1],
                              critical=ev.get("action") != "stopped",  # stopped by hand: Telegram only, no phone push
                              title=lambda L, ev=ev, name=name: _event_text(L, ev, name, label)[0], tag=f"bot-{name}")
    await handle_binance(host, binance, list(bot_ids.values()))
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
        hosts = [(h.alias or h.hostname, h.last_seen) for h in silent]
    for name, seen in hosts:
        monitor.notify_admins("monitoring", lambda L, n=name, w=seen: "🚨 " + t(L, "wd_host_silent", host=n,
                                                                                 since=w.strftime("%H:%M UTC")),
                              critical=True, title=lambda L, n=name: t(L, "push_wd_host", host=n), tag=f"host-{name}")


# ------------------------------------------------------------------ Binance IP ban: pause the server's bots, resume later
BAN_COOLDOWN = 60       # extra seconds after the ban ends before starting again
LIMITED_REPORTS = 2     # 429 in this many reports in a row -> treat like a short ban
WEIGHT_WARN = 0.8       # warn when 80 % of the per-minute request weight is used
_tasks: set[asyncio.Task] = set()


def _bg(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def ft_post(bot_id: int, action: str) -> str:
    """/api/v1/stop or /start on a bot, without polling it afterwards (a poll would call Binance)."""
    import httpx

    from ..engine.botmonitor import normalize_api_url
    from ..security import decrypt
    with SessionLocal() as s:
        b = s.get(Bot, bot_id)
        url, user, pw = normalize_api_url(b.api_url), b.api_user, decrypt(b.api_pass_enc) or ""
    async with httpx.AsyncClient(base_url=url + "/api/v1", auth=(user, pw), timeout=15) as c:
        r = await c.post("/" + action)
        r.raise_for_status()
        return str(r.json().get("status", ""))[:120]


def _mark(bot_id: int, paused: bool) -> None:
    with SessionLocal() as s:
        b = s.get(Bot, bot_id)
        if b is not None:
            st = dict(b.state or {})
            if paused:
                st["ban_paused"] = True
            else:
                st.pop("ban_paused", None)
            b.state = st
            s.commit()


def ban_paused_ids() -> set[int]:
    with SessionLocal() as s:
        return {b.id for b in s.scalars(select(Bot)) if (b.state or {}).get("ban_paused")}


def _fmt(when: dt.datetime) -> str:
    return when.strftime("%H:%M UTC")


async def handle_binance(host: str, binance: dict, host_bot_ids: list[int]) -> None:
    from ..engine.botmonitor import monitor
    from ..telegram import esc
    label = host_label(host)
    now = utcnow()
    banned = [v for v in binance.values() if v.get("status") == "banned"]
    limited = [v for v in binance.values() if v.get("status") == "limited"]
    with SessionLocal() as s:
        h = s.scalars(select(WatchdogHost).where(WatchdogHost.hostname == host)).first()
        gs = get_global(s)
        auto, ban = gs.ban_auto_pause, dict(h.ban or {})
        ban["limited_n"] = ban.get("limited_n", 0) + 1 if limited else 0
        until, reason = None, ""
        if banned:
            until = dt.datetime.fromtimestamp(max(int(v.get("banned_until") or 0) for v in banned) / 1000,
                                           dt.UTC).replace(tzinfo=None)
            reason = "banned"
        elif ban["limited_n"] >= LIMITED_REPORTS:
            until = now + dt.timedelta(seconds=max(120, max(int(v.get("retry_after") or 0) for v in limited)))
            reason = "limited"
        warn = []
        for kind, v in binance.items():
            used, limit = v.get("used_weight"), v.get("limit")
            if v.get("status") == "ok" and used and limit and used / limit >= WEIGHT_WARN:
                warn.append(f"{kind}: {used}/{limit}")
        last_warn = ban.get("warned_at")
        do_warn = bool(warn) and (not last_warn or now - dt.datetime.fromisoformat(last_warn) > dt.timedelta(minutes=30))
        if do_warn:
            ban["warned_at"] = now.isoformat(timespec="seconds")
        start = bool(until) and not ban.get("active")
        if until:
            old = ban.get("until")
            ban["until"] = max(until, dt.datetime.fromisoformat(old)).isoformat(timespec="seconds") if old and ban.get(
                "active") else until.isoformat(timespec="seconds")
        if start:
            ban.update(active=True, since=now.isoformat(timespec="seconds"), reason=reason, bots=[])
        h.ban = ban
        s.commit()
        running = []
        if start and auto:
            for b in s.scalars(select(Bot).where(Bot.id.in_(host_bot_ids or [-1]))):
                if b.enabled and b.api_url and (b.state or {}).get("state") == "running":
                    running.append(b.id)
    if do_warn:
        monitor.notify_admins("monitoring", lambda L: "⚠️ " + t(L, "ban_weight_warn", host=esc(label), w=esc(", ".join(warn))))
    if start:
        until_dt = dt.datetime.fromisoformat(ban["until"])
        resume_at = until_dt + dt.timedelta(seconds=BAN_COOLDOWN)
        key = "ban_paused" if auto else "ban_alert_only"
        monitor.notify_admins(
            "monitoring", lambda L: "🚫 " + t(L, key, host=esc(label), until=_fmt(until_dt), resume=_fmt(resume_at),
                                              n=len(running), why=t(L, "ban_why_" + reason)),
            critical=True, title=lambda L: t(L, "push_ban", host=label), tag=f"ban-{host}")
        if running:
            _bg(_pause(host, running))


async def _pause(host: str, ids: list[int]) -> None:
    stopped = []
    for bot_id in ids:
        try:
            await ft_post(bot_id, "stop")
            _mark(bot_id, True)
            stopped.append(bot_id)
        except Exception as e:
            log_err(f"stop bot {bot_id} on {host}: {e}")
    with SessionLocal() as s:
        h = s.scalars(select(WatchdogHost).where(WatchdogHost.hostname == host)).first()
        ban = dict(h.ban or {})
        ban["bots"] = sorted(set(ban.get("bots") or []) | set(stopped))
        h.ban = ban
        s.commit()


def log_err(msg: str) -> None:
    import logging
    logging.getLogger("relay.watchdog").warning(msg)


async def resume_due_hosts() -> None:
    """Every minute: a server whose ban is over (and Binance answers normally again) gets its bots started again."""
    from ..engine.botmonitor import monitor
    from ..telegram import esc
    now = utcnow()
    with SessionLocal() as s:
        gap = get_global(s).ban_resume_gap
        gap = 30 if gap is None else gap
        due = []
        for h in s.scalars(select(WatchdogHost)):
            ban = dict(h.ban or {})
            if not ban.get("active") or not ban.get("until"):
                continue
            if now < dt.datetime.fromisoformat(ban["until"]) + dt.timedelta(seconds=BAN_COOLDOWN):
                continue
            if any(v.get("status") in ("banned", "limited") for v in ((h.report or {}).get("binance") or {}).values()):
                continue  # the latest check still says no: wait for the next report
            ban.update(active=False, resumed_at=now.isoformat(timespec="seconds"), limited_n=0)
            ids, ban["bots"] = list(ban.get("bots") or []), []
            h.ban = ban
            due.append((h.hostname, ids))
        s.commit()
    labels = host_labels()
    for host, ids in due:
        monitor.notify_admins("monitoring", lambda L, h=labels.get(host, host), n=len(ids): "✅ " + t(L, "ban_resume", host=esc(h), n=n,
                                                                                    gap=gap),
                              critical=True, title=lambda L, h=labels.get(host, host): t(L, "push_ban_over", host=h), tag=f"ban-{host}")
        if ids:
            _bg(_resume(host, ids, gap))


async def _resume(host: str, ids: list[int], gap: int) -> None:
    for i, bot_id in enumerate(ids):
        if i:
            await asyncio.sleep(gap)  # one by one, like `sleep 45` in tmux.sh: a burst could trigger the next ban
        try:
            await ft_post(bot_id, "start")
        except Exception as e:
            log_err(f"start bot {bot_id} on {host}: {e}")
        _mark(bot_id, False)
