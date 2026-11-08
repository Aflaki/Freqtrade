"""Watches the Freqtrade bot itself.

* Every Freqtrade webhook message (startup, status, warnings, errors, cancels, protections, ...) is
  logged and forwarded to the admins' Telegram — like Freqtrade's own Telegram integration.
  These messages never trigger trades.
* Every bot trade signal is reported to admins together with how many portfolios copied it.
* Many bots: each Freqtrade instance is a `Bot` with its own webhook URL; its API (if set) is polled
  every minute (state, strategy, heartbeat, open trades, whitelist) and admins are alerted on changes.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import math
import re
from collections import Counter
from typing import Any

import httpx
from sqlalchemy import select

from ..db import Bot, SessionLocal, User, utcnow
from ..i18n import t
from ..security import decrypt

log = logging.getLogger("relay.botmonitor")

# Freqtrade message types that are information only (never traded on)
EVENT_TYPES = {"status", "startup", "warning", "exception", "entry_cancel", "exit_cancel",
               "protection_trigger", "protection_trigger_global", "strategy_msg", "liquidation_warning"}
CONTROL_ACTIONS = ("start", "stop", "pause", "stopentry", "reload_config")
STALL_MINUTES = 10


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def md_to_html(text: str) -> str:
    """Freqtrade writes Telegram-Markdown (*bold*, `code`, ```block```); convert to safe HTML."""
    s = esc(text)
    s = re.sub(r"```(?:\w+\n)?(.*?)```", lambda m: f"<pre>{m.group(1).strip()}</pre>", s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", s)
    return s[:3500]


def event_text(lang: str, p: dict) -> str:
    typ = str(p.get("type", ""))
    pair = esc(p.get("pair") or "")
    if typ in ("status", "startup", "warning", "exception"):
        icon = {"status": "🤖", "startup": "🚀", "warning": "⚠️", "exception": "🛑"}[typ]
        return f"{icon} <b>Freqtrade</b> — {t(lang, 'ev_' + typ)}\n{md_to_html(str(p.get('status', '')))}"
    if typ in ("entry_cancel", "exit_cancel"):
        return (f"❌ {t(lang, 'ev_' + typ)}: <b>{pair}</b> {esc(str(p.get('direction', '')).upper())}"
                f"\n{t(lang, 'tg_reason')}: {esc(p.get('reason', ''))}")
    if typ.startswith("protection_trigger"):
        return (f"🔒 {t(lang, 'ev_protection')}: <b>{pair or '*'}</b> → {esc(p.get('lock_end_time', ''))}"
                f"\n{t(lang, 'tg_reason')}: {esc(p.get('reason', ''))}")
    if typ == "strategy_msg":
        return f"💬 {t(lang, 'ev_strategy_msg')}: {esc(p.get('msg', ''))}"
    if typ == "liquidation_warning":
        return (f"🚨 {t(lang, 'ev_liquidation')}: <b>{pair}</b> {esc(str(p.get('direction', '')).upper())} "
                f"{esc(p.get('leverage', ''))}x\n{t(lang, 'tg_price')}: {esc(p.get('current_rate'))} → "
                f"liq {esc(p.get('liquidation_price'))}")
    return f"🤖 {esc(typ)}: {esc(json.dumps(p, default=str)[:500])}"


def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
        return -1
    return v


def normalize_api_url(url: str) -> str:
    """Accept '65.1.2.3:8015', 'http://host:8015/', '.../api/v1' or a FreqUI link; return 'http://host:port'."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "http://" + url
    url = re.sub(r"/api/v1.*$", "", url)
    url = re.sub(r"/(trade|dashboard|graph|logs|login|settings)(/.*)?$", "", url)
    return url.rstrip("/")


async def fetch_info(url: str, user: str, pw: str) -> dict[str, Any]:
    """Read a Freqtrade instance through its REST API.

    Only /show_config is required (it proves address + login are right). /health, /count and
    /whitelist are optional, so older Freqtrade versions still work.
    """
    async with httpx.AsyncClient(base_url=normalize_api_url(url) + "/api/v1", auth=(user, pw), timeout=10) as c:
        async def get(path: str, required: bool = False) -> Any:
            try:
                r = await c.get(path)
                r.raise_for_status()
                return r.json()
            except Exception:
                if required:
                    raise
                return {}
        cfg = await get("/show_config", required=True)
        health = await get("/health")
        count = await get("/count")
        wl = (await get("/whitelist")).get("whitelist", [])
        bal = await get("/balance")
    last = health.get("last_process")
    stalled = False
    if last:
        try:
            lp = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            lp = lp.astimezone(dt.UTC).replace(tzinfo=None) if lp.tzinfo else lp
            stalled = cfg.get("state") == "running" and utcnow() - lp > dt.timedelta(minutes=STALL_MINUTES)
        except ValueError:
            pass
    return dict(api="ok", error="", state=cfg.get("state"), bot_name=cfg.get("bot_name"),
                strategy=cfg.get("strategy"), exchange=cfg.get("exchange"), dry_run=cfg.get("dry_run"),
                trading_mode=cfg.get("trading_mode"), margin_mode=cfg.get("margin_mode"),
                stake_currency=cfg.get("stake_currency"), timeframe=cfg.get("timeframe"),
                max_open_trades=_clean(cfg.get("max_open_trades")), version=cfg.get("version"),
                open_trades=count.get("current"), last_process=last, stalled=stalled,
                starting_capital=bal.get("starting_capital"),
                whitelist=sorted({p.split(":")[0].upper() for p in wl}))


async def diagnose(url: str, user: str, pw: str) -> list[dict[str, Any]]:
    """Step-by-step connection test shown on the bot page: which step fails and why."""
    base = normalize_api_url(url)
    steps: list[dict[str, Any]] = [{"step": "url", "ok": bool(base), "detail": base or "empty"}]
    if not base:
        return steps
    async with httpx.AsyncClient(base_url=base + "/api/v1", timeout=8) as c:
        try:
            r = await c.get("/ping")
            ok = r.status_code == 200 and "pong" in r.text
            steps.append({"step": "reach", "ok": ok, "detail": f"HTTP {r.status_code} {r.text[:80]}",
                          "hint": "" if ok else "not_freqtrade"})
        except httpx.ConnectError as e:
            steps.append({"step": "reach", "ok": False, "detail": str(e)[:150], "hint": "refused"})
            return steps
        except httpx.TimeoutException:
            steps.append({"step": "reach", "ok": False, "detail": "timeout", "hint": "timeout"})
            return steps
        except Exception as e:
            steps.append({"step": "reach", "ok": False, "detail": f"{type(e).__name__}: {str(e)[:150]}", "hint": "refused"})
            return steps
        for path in ("/show_config", "/health", "/count", "/whitelist"):
            try:
                r = await c.get(path, auth=(user, pw))
                ok = r.status_code == 200
                hint = "" if ok else ("auth" if r.status_code in (401, 403) else "old_version" if path != "/show_config" else "not_freqtrade")
                detail = f"HTTP {r.status_code}"
                if ok and path == "/show_config":
                    j = r.json()
                    detail += f" — {j.get('strategy')} · {j.get('exchange')} · {j.get('state')}"
                steps.append({"step": path, "ok": ok, "detail": detail, "hint": hint, "optional": path != "/show_config"})
                if not ok and path == "/show_config":
                    break
            except Exception as e:
                steps.append({"step": path, "ok": False, "detail": f"{type(e).__name__}: {str(e)[:150]}"})
                break
    return steps


def _error_state(e: Exception) -> dict[str, Any]:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return {"api": "auth_error" if code in (401, 403) else "unreachable", "error": f"HTTP {code}"}
    return {"api": "unreachable", "error": f"{type(e).__name__}: {str(e)[:200]}"}


class BotMonitor:
    def __init__(self) -> None:
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------ admin notifications
    def notify_admins(self, kind: str, make_text) -> None:
        from ..telegram import bot as tg
        with SessionLocal() as s:
            admins = s.scalars(select(User).where(User.is_admin.is_(True), User.telegram_chat_id.is_not(None))).all()
        for a in admins:
            if a.wants(kind):
                task = asyncio.create_task(tg.send(a.telegram_chat_id, make_text(a.lang), a.id, kind))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------ webhook side
    def on_webhook(self, bot_id: int, payload: dict) -> None:
        typ = str(payload.get("type", ""))
        now = utcnow().isoformat(timespec="seconds")
        with SessionLocal() as s:
            b = s.get(Bot, bot_id)
            if b is None:
                return
            st = dict(b.state or {})
            st["last_webhook"], st["last_webhook_type"] = now, typ
            if typ == "status":
                txt = str(payload.get("status", "")).strip().lower()
                for word in ("running", "stopped", "paused", "process died", "reload"):
                    if txt.startswith(word):
                        st["webhook_state"], st["webhook_state_at"] = word, now
            if typ == "startup":
                st["last_startup"] = now
            b.state = st
            s.commit()

    def forward_event(self, bot_name: str, payload: dict) -> None:
        self.notify_admins("bot_status", lambda lang: f"[{esc(bot_name)}] " + event_text(lang, payload))

    def report_trade(self, bot_name: str, sig, pair: str | None, results: list) -> None:
        """Bot entry/exit + what happened on the users' portfolios."""
        raw = sig.raw or {}
        if str(raw.get("type", "")) not in ("entry", "entry_fill", "exit", "exit_fill"):
            return
        c = Counter(r.status for r in results)
        icons = {"opened": "✅", "added": "➕", "closed": "✅", "partial": "✂️", "skipped": "⏭", "error": "⚠️", "none": "➖"}
        copied = " · ".join(f"{icons.get(k, '')}{k} {v}" for k, v in c.items())
        side = "🟢 LONG" if sig.action in ("long", "close_long") else "🔴 SHORT"
        price = raw.get("order_rate") or raw.get("close_rate") or ""

        def text(lang: str) -> str:
            if sig.action in ("long", "short"):
                head = t(lang, "ev_bot_entry")
            elif sig.close_fraction < 0.999:
                head = t(lang, "ev_bot_partial", pct=f"{sig.close_fraction:.0%}")
            else:
                head = t(lang, "ev_bot_exit")
            extra = ""
            pr = raw.get("final_profit_ratio") or raw.get("profit_ratio")
            if sig.action.startswith("close") and pr not in (None, "", "None"):
                try:
                    extra = f" | {float(pr) * 100:+.2f}%"
                except ValueError:
                    pass
            return (f"[{esc(bot_name)}] 🤖 {head}: <b>{esc(pair or sig.symbol)}</b> {side} @ {esc(price)} "
                    f"(#{esc(raw.get('trade_id', ''))}){extra}\n"
                    f"📤 {t(lang, 'ev_copied')}: {copied or t(lang, 'ev_no_portfolios')}")
        self.notify_admins("bot_trades", text)

    # ------------------------------------------------------------ REST API side
    async def poll_bot(self, bot_id: int, alert: bool = True) -> dict:
        from ..db import get_global
        with SessionLocal() as s:
            b = s.get(Bot, bot_id)
            if b is None:
                return {}
            url, user, pw, name = b.api_url, b.api_user, decrypt(b.api_pass_enc) or "", b.name
            old = dict(b.state or {})
            allowed = list(get_global(s).allowed_pairs)
            bot_allows = b.allows
        st = dict(old)
        st["checked_at"] = utcnow().isoformat(timespec="seconds")
        if not url:
            st.update(api="not_configured", error="")
        else:
            try:
                st.update(await fetch_info(url, user, pw))
                st["missing_pairs"] = [p for p in st["whitelist"] if p not in allowed and bot_allows(p)]
                st.update(await self._sync_trades(bot_id, url, user, pw, full=not old.get("history_synced")))
            except Exception as e:
                st.update(_error_state(e))
        with SessionLocal() as s:
            b = s.get(Bot, bot_id)
            if b is not None:
                b.state = st
                s.commit()
        if alert and url:
            self._alert_changes(name, old, st)
        return st

    async def _sync_trades(self, bot_id: int, url: str, user: str, pw: str, full: bool) -> dict:
        """Closed trades from the Freqtrade API -> identical history even without webhook."""
        from . import performance as perf
        try:
            if full:
                n = await perf.import_from_api(bot_id, normalize_api_url(url), user, pw)
            else:
                n = await perf.sync_recent_from_api(bot_id, normalize_api_url(url), user, pw)
            return {"history_synced": True, "synced_at": utcnow().isoformat(timespec="seconds"), "sync_error": "",
                    **({"history_count": n} if full else {})}
        except Exception as e:
            return {"sync_error": f"{type(e).__name__}: {str(e)[:150]}"}

    async def poll_all(self, alert: bool = True) -> None:
        with SessionLocal() as s:
            ids = list(s.scalars(select(Bot.id).where(Bot.enabled.is_(True))))
        sem = asyncio.Semaphore(8)

        async def one(i: int) -> None:
            async with sem:
                try:
                    await self.poll_bot(i, alert)
                except Exception:
                    log.exception("poll bot %s failed", i)
        await asyncio.gather(*(one(i) for i in ids))

    def _alert_changes(self, name: str, old: dict, new: dict) -> None:
        p = f"[{esc(name)}] "
        if old.get("api") == "ok" and new.get("api") != "ok":
            self.notify_admins("bot_status", lambda L: p + t(L, "bot_alert_down", err=esc(new.get("error", ""))))
        elif old.get("api") not in (None, "ok", "not_configured") and new.get("api") == "ok":
            self.notify_admins("bot_status", lambda L: p + t(L, "bot_alert_up", state=esc(new.get("state"))))
        if old.get("api") == "ok" and new.get("api") == "ok" and old.get("state") != new.get("state"):
            self.notify_admins("bot_status", lambda L: p + t(L, "bot_alert_state", old=esc(old.get("state")),
                                                              new=esc(new.get("state"))))
        if new.get("stalled") and not old.get("stalled"):
            self.notify_admins("bot_status", lambda L: p + t(L, "bot_alert_stalled", min=STALL_MINUTES,
                                                              ts=esc(new.get("last_process"))))
        if new.get("missing_pairs") and new.get("missing_pairs") != old.get("missing_pairs"):
            self.notify_admins("bot_status", lambda L: p + t(L, "bot_alert_pairs",
                                                              pairs=esc(", ".join(new["missing_pairs"]))))

    async def control(self, bot_id: int, action: str) -> str:
        if action not in CONTROL_ACTIONS:
            raise ValueError("unknown action")
        with SessionLocal() as s:
            b = s.get(Bot, bot_id)
            if b is None or not b.api_url:
                raise ValueError("this bot has no Freqtrade API configured")
            url, user, pw = normalize_api_url(b.api_url), b.api_user, decrypt(b.api_pass_enc) or ""
        async with httpx.AsyncClient(base_url=url + "/api/v1", auth=(user, pw), timeout=15) as c:
            r = await c.post("/" + action)
            r.raise_for_status()
            msg = str(r.json().get("status", r.text))[:200]
        await asyncio.sleep(1)
        await self.poll_bot(bot_id, alert=False)
        return msg

    async def loop(self) -> None:
        while True:
            try:
                await self.poll_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bot monitor poll failed")
            await asyncio.sleep(60)


monitor = BotMonitor()
