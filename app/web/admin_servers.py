"""Admin → Servers: monitoring and control of the Freqtrade servers and their bots in one place."""
from __future__ import annotations

import asyncio
import datetime as dt
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..db import Bot, NotificationLog, ServerSample, SessionLocal, WatchdogEvent, WatchdogHost, get_global, utcnow
from ..engine.botmonitor import STALL_MINUTES, monitor
from ..security import flash, form_with_csrf, require_admin
from .common import render, tr
from .watchdog import SILENT_MINUTES, _bg, _mark, bots_on_host, ft_post, site_url, token

router = APIRouter(prefix="/admin/servers")
HISTORY_HOURS = 24
POINTS = 144  # one point per 10 minutes


def _age(ts: str | None) -> float | None:
    """Minutes since an ISO timestamp (Freqtrade's last_process etc.)."""
    if not ts:
        return None
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    d = d.astimezone(dt.UTC).replace(tzinfo=None) if d.tzinfo else d
    return max(0.0, (utcnow() - d).total_seconds() / 60)


def _bot_row(b: Bot, agent: dict | None) -> dict:
    st = b.state or {}
    live = monitor.live.get(b.id) or {}
    trades = live.get("trades") or []
    if st.get("ban_paused"):
        health = "paused"
    elif agent and agent.get("status") == "stopped":
        health = "stopped"
    elif agent and agent.get("status") in ("down", "gave_up", "restarting"):
        health = agent["status"]
    elif not b.api_url:
        health = "no_api"
    elif st.get("api") != "ok":
        health = "unreachable"
    elif st.get("stalled"):
        health = "stalled"
    elif st.get("state") != "running":
        health = "ft_" + str(st.get("state") or "unknown")
    else:
        health = "ok"
    return {"b": b, "st": st, "agent": agent or {}, "health": health, "open": len(trades),
            "max_open": st.get("max_open_trades"), "upnl": sum(t.get("profit_abs") or 0 for t in trades),
            "heartbeat": _age(st.get("last_process")),
            "uptime_h": (utcnow().timestamp() - agent["since"]) / 3600 if agent and agent.get("since") else None}


def _series(samples: list[ServerSample], attr: str) -> list[float | None]:
    """Down-sample to POINTS buckets over the last HISTORY_HOURS (max of each bucket: peaks matter)."""
    start = utcnow() - dt.timedelta(hours=HISTORY_HOURS)
    span = HISTORY_HOURS * 3600 / POINTS
    out: list[float | None] = [None] * POINTS
    for x in samples:
        v = getattr(x, attr)
        if v is None:
            continue
        i = int((x.at - start).total_seconds() // span)
        if 0 <= i < POINTS:
            out[i] = v if out[i] is None else max(out[i], v)
    return out


def _view(admin_id: int) -> dict:
    since = utcnow() - dt.timedelta(hours=HISTORY_HOURS)
    with SessionLocal() as s:
        bots = list(s.scalars(select(Bot).order_by(Bot.sort, Bot.id)))
        hosts = list(s.scalars(select(WatchdogHost).order_by(WatchdogHost.hostname)))
        servers, placed = [], set()
        for h in hosts:
            rep = h.report or {}
            by_port = bots_on_host(h.ips or [], s)
            agent = {b.get("port"): b for b in rep.get("bots") or [] if b.get("port")}
            rows = [_bot_row(b, agent.get(port)) for port, b in sorted(by_port.items(), key=lambda kv: kv[1].sort)]
            placed |= {b.id for b in by_port.values()}
            samples = list(s.scalars(select(ServerSample).where(ServerSample.host == h.hostname, ServerSample.at >= since)
                                     .order_by(ServerSample.at)))
            mem = rep.get("mem") or {}
            info = rep.get("host") or {}
            servers.append({
                "h": h, "rows": rows, "rep": rep, "info": info, "ban": h.ban or {}, "binance": rep.get("binance") or {},
                "online": bool(h.last_seen and utcnow() - h.last_seen < dt.timedelta(minutes=SILENT_MINUTES)),
                "mem_pct": round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1) if mem.get("MemTotal") else None,
                "swap_pct": round(100 * (1 - mem.get("SwapFree", 0) / mem["SwapTotal"]), 1) if mem.get("SwapTotal") else None,
                "load_pct": round(100 * (rep.get("load") or [0])[0] / (info.get("cores") or 1)) if rep.get("load") else None,
                "rss_total": sum(r["agent"].get("rss_mb") or 0 for r in rows),
                "hist": {k: _series(samples, k) for k in ("mem_pct", "load1", "weight_pct")},
                "unknown": [b for b in rep.get("bots") or [] if b.get("port") not in by_port],
            })
        others: dict[str, list] = {}
        for b in bots:
            if b.id in placed or not b.enabled:
                continue
            host = (urlparse(b.api_url).hostname if b.api_url else "") or "—"
            others.setdefault(host, []).append(_bot_row(b, None))
        events = list(s.scalars(select(WatchdogEvent).where(WatchdogEvent.created_at >= since)
                                .order_by(WatchdogEvent.id.desc()).limit(60)))
        alerts = list(s.scalars(select(NotificationLog).where(NotificationLog.user_id == admin_id,
                                                              NotificationLog.kind == "bot_status",
                                                              NotificationLog.created_at >= since)
                                .order_by(NotificationLog.id.desc()).limit(60)))
        gs = get_global(s)
    timeline = sorted([("wd", e.created_at, e) for e in events] + [("alert", a.created_at, a) for a in alerts],
                      key=lambda x: x[1], reverse=True)[:60]
    all_rows = [r for sv in servers for r in sv["rows"]] + [r for rs in others.values() for r in rs]
    count = {}
    for r in all_rows:
        count[r["health"]] = count.get(r["health"], 0) + 1
    kpi = {"servers": len(servers), "online": sum(1 for sv in servers if sv["online"]), "bots": len(all_rows),
           "ok": count.get("ok", 0), "problems": sum(v for k, v in count.items() if k not in ("ok", "paused", "stopped")),
           "paused": count.get("paused", 0), "stopped": count.get("stopped", 0),
           "open": sum(r["open"] for r in all_rows), "upnl": sum(r["upnl"] for r in all_rows),
           "events": len(events), "bans": sum(1 for sv in servers if sv["ban"].get("active"))}
    names = {b.id: b.name for b in bots}
    return {"servers": servers, "others": others, "timeline": timeline, "kpi": kpi, "names": names, "gs": gs,
            "stall_min": STALL_MINUTES}


@router.get("")
async def page(request: Request):
    admin = require_admin(request)
    v = _view(admin.id)
    cmd = f'curl -fsSL "{site_url(request)}/watchdog/install.sh?t={token()}" | sudo sh'
    return render(request, "admin/servers.html", tab="servers", install_cmd=cmd, **v)


async def _start_slowly(ids: list[int], gap: int) -> None:
    for i, bot_id in enumerate(ids):
        if i:
            await asyncio.sleep(gap)
        try:
            await ft_post(bot_id, "start")
        except Exception:  # shown on the next refresh as unreachable / stopped
            pass
        _mark(bot_id, False)


@router.post("/action")
async def action(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    act, host, bot_id = str(f.get("action", "")), str(f.get("host", "")), str(f.get("bot_id", ""))
    if bot_id.isdigit():  # one bot
        if act not in ("start", "stop", "reload_config", "stopentry"):
            raise HTTPException(400, "bad action")
        try:
            msg = await monitor.control(int(bot_id), act)
            if act == "start":
                _mark(int(bot_id), False)
            flash(request, f"{msg}")
        except Exception as e:
            flash(request, f"{type(e).__name__}: {str(e)[:200]}", "error")
        return RedirectResponse("/admin/servers#bot-" + bot_id, 303)
    with SessionLocal() as s:  # every bot of one server
        h = s.scalars(select(WatchdogHost).where(WatchdogHost.hostname == host)).first()
        if h is None:
            raise HTTPException(404)
        ids = [b.id for b in bots_on_host(h.ips or [], s).values() if b.enabled and b.api_url]
        gap = get_global(s).ban_resume_gap or 30
    if act == "stop_all":
        bad = 0
        for i in ids:
            try:
                await ft_post(i, "stop")
            except Exception:
                bad += 1
        flash(request, tr(request, "srv_done_stop", n=len(ids) - bad) + (f" ({bad} ✗)" if bad else ""))
    elif act == "start_all":
        _bg(_start_slowly(ids, gap))
        flash(request, tr(request, "srv_done_start", n=len(ids), gap=gap))
    elif act == "reload_all":
        for i in ids:
            try:
                await ft_post(i, "reload_config")
            except Exception:
                pass
        flash(request, tr(request, "msg_done"))
    else:
        raise HTTPException(400, "bad action")
    return RedirectResponse("/admin/servers#host-" + host, 303)
