"""Watches the Freqtrade bot itself.

* Every Freqtrade webhook message (startup, status, warnings, errors, cancels, protections, ...) is
  logged and forwarded to the admins' Telegram — like Freqtrade's own Telegram integration.
  These messages never trigger trades.
* Every bot trade signal is reported to admins together with how many portfolios copied it.
* If the Freqtrade REST API is configured, it is polled every minute (state, strategy, heartbeat,
  open trades, whitelist) and admins are alerted when something changes. Start/stop from the panel.
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

from ..db import SessionLocal, User, get_global, utcnow
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


class BotMonitor:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self._loaded = False
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------ persistence
    def load(self) -> dict:
        if not self._loaded:
            with SessionLocal() as s:
                self.state = dict(get_global(s).bot_state or {})
            self._loaded = True
        return self.state

    def _save(self) -> None:
        with SessionLocal() as s:
            get_global(s).bot_state = dict(self.state)
            s.commit()

    # ------------------------------------------------------------ admin notifications
    def notify_admins(self, kind: str, make_text) -> None:
        from ..telegram import bot
        with SessionLocal() as s:
            admins = s.scalars(select(User).where(User.is_admin.is_(True), User.telegram_chat_id.is_not(None))).all()
        for a in admins:
            if a.wants(kind):
                task = asyncio.create_task(bot.send(a.telegram_chat_id, make_text(a.lang), a.id, kind))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------ webhook side
    def on_webhook(self, payload: dict) -> None:
        self.load()
        typ = str(payload.get("type", ""))
        now = utcnow().isoformat(timespec="seconds")
        self.state["last_webhook"] = now
        self.state["last_webhook_type"] = typ
        if typ == "status":
            txt = str(payload.get("status", "")).strip().lower()
            for word in ("running", "stopped", "paused", "process died", "reload"):
                if txt.startswith(word):
                    self.state["webhook_state"] = word
                    self.state["webhook_state_at"] = now
        if typ == "startup":
            self.state["last_startup"] = now
        self._save()

    def forward_event(self, payload: dict) -> None:
        self.notify_admins("bot_status", lambda lang: event_text(lang, payload))

    def report_trade(self, sig, pair: str | None, results: list) -> None:
        """Bot entry/exit + what happened on the users' portfolios."""
        raw = sig.raw or {}
        typ = str(raw.get("type", ""))
        if typ not in ("entry", "entry_fill", "exit", "exit_fill"):
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
            return (f"🤖 {head}: <b>{esc(pair or sig.symbol)}</b> {side} @ {esc(price)} (#{esc(raw.get('trade_id', ''))}){extra}\n"
                    f"📤 {t(lang, 'ev_copied')}: {copied or t(lang, 'ev_no_portfolios')}")
        self.notify_admins("bot_trades", text)

    # ------------------------------------------------------------ REST API side
    def _api(self) -> tuple[str, str, str, list[str]]:
        with SessionLocal() as s:
            gs = get_global(s)
            return gs.ft_api_url.rstrip("/"), gs.ft_api_user, decrypt(gs.ft_api_pass_enc) or "", list(gs.allowed_pairs)

    async def poll(self, alert: bool = True) -> dict:
        self.load()
        old = dict(self.state)
        url, user, pw, allowed = self._api()
        st = dict(self.state)
        st["checked_at"] = utcnow().isoformat(timespec="seconds")
        if not url:
            st.update(api="not_configured", error="")
        else:
            try:
                async with httpx.AsyncClient(base_url=url + "/api/v1", auth=(user, pw), timeout=10) as c:
                    async def get(path: str) -> Any:
                        r = await c.get(path)
                        r.raise_for_status()
                        return r.json()
                    cfg = await get("/show_config")
                    health = await get("/health")
                    count = await get("/count")
                    wl = (await get("/whitelist")).get("whitelist", [])
                pairs = sorted({p.split(":")[0].upper() for p in wl})
                last = health.get("last_process")
                stalled = False
                if last:
                    try:
                        lp = dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                        lp = lp.astimezone(dt.UTC).replace(tzinfo=None) if lp.tzinfo else lp
                        stalled = cfg.get("state") == "running" and utcnow() - lp > dt.timedelta(minutes=STALL_MINUTES)
                    except ValueError:
                        pass
                st.update(api="ok", error="", state=cfg.get("state"), bot_name=cfg.get("bot_name"),
                          strategy=cfg.get("strategy"), exchange=cfg.get("exchange"), dry_run=cfg.get("dry_run"),
                          trading_mode=cfg.get("trading_mode"), margin_mode=cfg.get("margin_mode"),
                          stake_currency=cfg.get("stake_currency"), timeframe=cfg.get("timeframe"),
                          max_open_trades=_clean(cfg.get("max_open_trades")), version=cfg.get("version"),
                          open_trades=count.get("current"), last_process=last, stalled=stalled,
                          whitelist=pairs, missing_pairs=[p for p in pairs if p not in allowed],
                          extra_pairs=[p for p in allowed if p not in pairs])
            except httpx.HTTPStatusError as e:
                st.update(api="auth_error" if e.response.status_code in (401, 403) else "unreachable",
                          error=f"HTTP {e.response.status_code}")
            except Exception as e:
                st.update(api="unreachable", error=f"{type(e).__name__}: {str(e)[:200]}")
        self.state = st
        self._save()
        if alert and url:
            self._alert_changes(old, st)
        return st

    def _alert_changes(self, old: dict, new: dict) -> None:
        if old.get("api") == "ok" and new.get("api") != "ok":
            self.notify_admins("bot_status", lambda L: t(L, "bot_alert_down", err=esc(new.get("error", ""))))
        elif old.get("api") not in (None, "ok", "not_configured") and new.get("api") == "ok":
            self.notify_admins("bot_status", lambda L: t(L, "bot_alert_up", state=esc(new.get("state"))))
        if old.get("api") == "ok" and new.get("api") == "ok" and old.get("state") != new.get("state"):
            self.notify_admins("bot_status", lambda L: t(L, "bot_alert_state", old=esc(old.get("state")),
                                                          new=esc(new.get("state"))))
        if new.get("stalled") and not old.get("stalled"):
            self.notify_admins("bot_status", lambda L: t(L, "bot_alert_stalled", min=STALL_MINUTES,
                                                          ts=esc(new.get("last_process"))))
        if new.get("missing_pairs") and new.get("missing_pairs") != old.get("missing_pairs"):
            self.notify_admins("bot_status", lambda L: t(L, "bot_alert_pairs", pairs=esc(", ".join(new["missing_pairs"]))))

    async def control(self, action: str) -> str:
        if action not in CONTROL_ACTIONS:
            raise ValueError("unknown action")
        url, user, pw, _ = self._api()
        if not url:
            raise ValueError("Freqtrade API is not configured")
        async with httpx.AsyncClient(base_url=url + "/api/v1", auth=(user, pw), timeout=15) as c:
            r = await c.post("/" + action)
            r.raise_for_status()
            msg = str(r.json().get("status", r.text))[:200]
        await asyncio.sleep(1)
        await self.poll(alert=False)
        return msg

    async def loop(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bot monitor poll failed")
            await asyncio.sleep(60)


monitor = BotMonitor()
