"""One shared Telegram bot: per-user notifications (in the user's language), logged, plus Freqtrade-like commands.

The bot token is set from the admin panel; changing it restarts the bot without restarting the app.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
from typing import Any

import httpx
from sqlalchemy import func, select

from .db import ExchangeAccount, NotificationLog, SessionLocal, Trade, User, get_global, utcnow
from .i18n import LANGS, t

log = logging.getLogger("relay.telegram")


def esc(v: Any) -> str:
    return html.escape(str(v))


def fmt_num(v: float | None) -> str:
    if v is None:
        return "-"
    v = float(v)
    return f"{v:,.6g}" if abs(v) < 1 else f"{v:,.4f}" if abs(v) < 100 else f"{v:,.2f}"


class TelegramBot:
    def __init__(self) -> None:
        self.token = ""
        self.username: str | None = None
        self.last_error = ""
        self._client = httpx.AsyncClient(timeout=40)
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def configure(self, token: str) -> None:
        """(Re)start with a new token. Called on startup and when the admin saves a token."""
        await self._stop_poll()
        self.token, self.username, self.last_error = token.strip(), None, ""
        if not self.token:
            return
        try:
            r = await self._client.get(self._url("getMe"))
            data = r.json()
            if not data.get("ok"):
                self.last_error = data.get("description", "invalid token")
                return
            self.username = data["result"]["username"]
            await self._client.post(self._url("deleteWebhook"))
            self._task = asyncio.create_task(self._poll())
            log.info("telegram bot @%s ready", self.username)
        except Exception as e:
            self.last_error = str(e)[:200]
            log.error("telegram start failed: %s", e)

    async def _stop_poll(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def stop(self) -> None:
        await self._stop_poll()
        await self._client.aclose()

    # ------------------------------------------------------------ sending (always logged)
    async def send(self, chat_id: str | int, text: str, user_id: int | None = None, kind: str = "reply") -> bool:
        if not chat_id:
            return False
        ok, err = False, ""
        if not self.enabled:
            err = "telegram not configured"
        else:
            for attempt in range(3):
                try:
                    r = await self._client.post(self._url("sendMessage"), json={
                        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
                    if r.status_code == 429:
                        await asyncio.sleep(float(r.json().get("parameters", {}).get("retry_after", 2)))
                        continue
                    ok = r.status_code < 400
                    err = "" if ok else r.text[:300]
                    break
                except httpx.HTTPError as e:
                    err = str(e)[:300]
                    await asyncio.sleep(1 + attempt)
        try:
            with SessionLocal() as s:
                s.add(NotificationLog(user_id=user_id, chat_id=str(chat_id), kind=kind, text=text[:4000], ok=ok, error=err))
                s.commit()
        except Exception:
            log.exception("could not log notification")
        return ok

    def notify(self, user: User, kind: str, text: str) -> None:
        """Fire-and-forget; never blocks or breaks trading."""
        if not (user.telegram_chat_id and user.wants(kind)):
            return
        task = asyncio.create_task(self.send(user.telegram_chat_id, text, user.id, kind))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------ polling
    async def _poll(self) -> None:
        offset = 0
        while True:
            try:
                r = await self._client.get(self._url("getUpdates"),
                                           params={"timeout": 30, "offset": offset, "allowed_updates": '["message"]'})
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    if msg.get("text") and msg.get("chat", {}).get("type") == "private":
                        try:
                            await self.handle(str(msg["chat"]["id"]), msg["text"].strip(),
                                              (msg.get("from") or {}).get("username", ""),
                                              (msg.get("from") or {}).get("language_code", ""))
                        except Exception:
                            log.exception("telegram command failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll error: %s", e)
                await asyncio.sleep(5)

    # ------------------------------------------------------------ commands
    async def handle(self, chat_id: str, text: str, username: str = "", tg_lang: str = "") -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start" and arg:
            return await self._link(chat_id, arg.strip(), username)
        with SessionLocal() as s:
            user = s.scalars(select(User).where(User.telegram_chat_id == chat_id)).first()
            default_lang = get_global(s).default_lang
        if user is None:
            lang = tg_lang[:2] if tg_lang[:2] in LANGS else default_lang
            return await self.send(chat_id, t(lang, "tg_not_linked"))
        handlers = {"/status": self._status, "/profit": self._profit, "/daily": self._daily,
                    "/performance": self._performance, "/balance": self._balance, "/pause": self._pause,
                    "/stop": self._pause, "/resume": self._resume, "/forceexit": self._forceexit, "/lang": self._lang}
        h = handlers.get(cmd)
        if h is None:
            return await self.send(chat_id, t(user.lang, "tg_help"), user.id)
        await h(chat_id, user, arg.strip())

    async def _link(self, chat_id: str, token: str, username: str) -> None:
        with SessionLocal() as s:
            user = s.scalars(select(User).where(User.telegram_link_token == token)).first()
            if user is None:
                return await self.send(chat_id, t(get_global(s).default_lang, "tg_link_bad"))
            for other in s.scalars(select(User).where(User.telegram_chat_id == chat_id, User.id != user.id)):
                other.telegram_chat_id = None
            user.telegram_chat_id, user.telegram_link_token = chat_id, None
            user.telegram_username = (username or "")[:64]
            s.commit()
            name, lang, uid = user.name or user.email, user.lang, user.id
        await self.send(chat_id, t(lang, "tg_linked", name=esc(name)) + "\n\n" + t(lang, "tg_help"), uid)

    async def _status(self, chat_id: str, user: User, arg: str) -> None:
        from .engine.exchange import pool
        L = user.lang
        with SessionLocal() as s:
            trades = s.scalars(select(Trade).where(Trade.user_id == user.id, Trade.status == "open")).all()
            accs = {a.id: a for a in s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id))}
        if not trades:
            return await self.send(chat_id, t(L, "tg_no_open"), user.id)
        lines = [f"<b>{t(L, 'tg_open_title')}</b>"]
        for tr in trades:
            acc = accs.get(tr.account_id)
            cur = None
            if acc:
                try:
                    cur = await (await pool.get(acc)).price(tr.symbol)
                except Exception:
                    pass
            pnl = ""
            if cur:
                upnl = (cur - tr.entry_price) * (1 if tr.side == "long" else -1) * tr.amount * tr.contract_size
                pnl = f"\n{t(L, 'tg_floating')}: <b>{upnl:+,.2f}</b> ({(upnl / tr.stake * 100) if tr.stake else 0:+.2f}%)"
            lines.append(f"\n#{tr.id} <b>{esc(tr.pair)}</b> {tr.side.upper()} — {esc(acc.label or acc.exchange or '') if acc else ''}\n"
                         f"{t(L, 'tg_entry')}: {fmt_num(tr.entry_price)} | {t(L, 'tg_price')}: {fmt_num(cur)} | "
                         f"{t(L, 'tg_margin')}: {tr.stake:,.2f}{pnl}")
        await self.send(chat_id, "\n".join(lines), user.id)

    def _pnl_rows(self, user_id: int, since: dt.datetime | None = None):
        with SessionLocal() as s:
            q = select(Trade).where(Trade.user_id == user_id, Trade.status == "closed")
            if since:
                q = q.where(Trade.closed_at >= since)
            return s.scalars(q).all(), get_global(s).stake_currency

    async def _profit(self, chat_id: str, user: User, arg: str) -> None:
        rows, cur = self._pnl_rows(user.id)
        if not rows:
            return await self.send(chat_id, t(user.lang, "tg_no_closed"), user.id)
        pnl = sum(r.realized_pnl for r in rows)
        stake = sum(r.stake for r in rows)
        wins = sum(1 for r in rows if r.realized_pnl > 0)
        await self.send(chat_id, t(user.lang, "tg_profit", n=len(rows), wins=wins, losses=len(rows) - wins,
                                   wr=f"{wins / len(rows) * 100:.0f}", pnl=f"{pnl:+,.2f}", cur=cur,
                                   pct=f"{(pnl / stake * 100) if stake else 0:+.2f}"), user.id)

    async def _daily(self, chat_id: str, user: User, arg: str) -> None:
        days = max(1, min(30, int(arg))) if arg.isdigit() else 7
        today = utcnow().date()
        rows, cur = self._pnl_rows(user.id, dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time()))
        lines = [f"<b>{t(user.lang, 'tg_daily_title', n=days)}</b>"]
        for i in range(days):
            d = today - dt.timedelta(days=i)
            day = [r for r in rows if r.closed_at and r.closed_at.date() == d]
            lines.append(f"{d.isoformat()}: {sum(r.realized_pnl for r in day):+,.2f} {cur} ({len(day)})")
        await self.send(chat_id, "\n".join(lines), user.id)

    async def _performance(self, chat_id: str, user: User, arg: str) -> None:
        rows, cur = self._pnl_rows(user.id)
        if not rows:
            return await self.send(chat_id, t(user.lang, "tg_no_closed"), user.id)
        agg: dict[str, list] = {}
        for r in rows:
            a = agg.setdefault(r.pair, [0.0, 0])
            a[0] += r.realized_pnl
            a[1] += 1
        lines = [f"<b>{t(user.lang, 'tg_perf_title')}</b>"]
        for pair, (p, n) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
            lines.append(f"{esc(pair)}: {p:+,.2f} {cur} ({n})")
        await self.send(chat_id, "\n".join(lines), user.id)

    async def _balance(self, chat_id: str, user: User, arg: str) -> None:
        from .engine.exchange import pool
        L = user.lang
        with SessionLocal() as s:
            accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)).all()
            cur = get_global(s).stake_currency
        if not accs:
            return await self.send(chat_id, t(L, "tg_no_accounts"), user.id)
        lines = [f"<b>{t(L, 'tg_balance_title')}</b>"]
        for a in accs:
            if a.is_signal:
                txt = t(L, "tg_signal_account")
            elif a.dry_run:
                txt = t(L, "tg_paper")
            else:
                try:
                    txt = f"{await (await pool.get(a)).free_balance(cur):,.2f} {cur}"
                except Exception as e:
                    txt = esc(str(e)[:80])
            lines.append(f"{esc(a.label or a.exchange or '-')}: {txt} — {t(L, 'tg_capital')}: {a.capital:,.2f}")
        await self.send(chat_id, "\n".join(lines), user.id)

    async def _set_trading(self, chat_id: str, user: User, on: bool) -> None:
        with SessionLocal() as s:
            s.get(User, user.id).trading_enabled = on
            s.commit()
        await self.send(chat_id, t(user.lang, "tg_resumed" if on else "tg_paused"), user.id)

    async def _pause(self, chat_id: str, user: User, arg: str) -> None:
        await self._set_trading(chat_id, user, False)

    async def _resume(self, chat_id: str, user: User, arg: str) -> None:
        await self._set_trading(chat_id, user, True)

    async def _forceexit(self, chat_id: str, user: User, arg: str) -> None:
        from .engine.trader import force_exit
        if not arg.lstrip("#").isdigit():
            return await self.send(chat_id, t(user.lang, "tg_forceexit_usage"), user.id)
        out = await force_exit(int(arg.lstrip("#")), user.id)
        if out is None:
            await self.send(chat_id, t(user.lang, "tg_forceexit_notfound"), user.id)

    async def _lang(self, chat_id: str, user: User, arg: str) -> None:
        code = arg.lower()[:2]
        if code not in LANGS:
            return await self.send(chat_id, t(user.lang, "tg_lang_usage", langs=" ".join(LANGS)), user.id)
        with SessionLocal() as s:
            s.get(User, user.id).lang = code
            s.commit()
        await self.send(chat_id, t(code, "tg_lang_set"), user.id)

    # ------------------------------------------------------------ scheduled jobs
    async def daily_jobs(self) -> None:
        """Daily report + subscription reminders, once per day at the admin-configured UTC hour."""
        while True:
            try:
                await asyncio.sleep(60)
                now = utcnow()
                with SessionLocal() as s:
                    gs = get_global(s)
                    if now.hour != gs.daily_report_hour_utc or gs.last_daily_report == now.date().isoformat():
                        continue
                    gs.last_daily_report = now.date().isoformat()
                    s.commit()
                    users = s.scalars(select(User).where(User.status == "active",
                                                         User.telegram_chat_id.is_not(None))).all()
                    cur = gs.stake_currency
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                for u in users:
                    if u.subscription_until and 0 <= (u.subscription_until - now).days <= 3:
                        await self.send(u.telegram_chat_id, t(u.lang, "tg_sub_expiring",
                                                              date=u.subscription_until.date().isoformat()), u.id, "subscription")
                    if not u.wants("daily_report"):
                        continue
                    with SessionLocal() as s:
                        n_open = s.scalar(select(func.count(Trade.id)).where(Trade.user_id == u.id, Trade.status == "open"))
                        closed = s.scalars(select(Trade).where(Trade.user_id == u.id, Trade.closed_at >= start)).all()
                    if not closed and not n_open:
                        continue
                    await self.send(u.telegram_chat_id, t(u.lang, "tg_daily_report", n=len(closed), open=n_open,
                                                          pnl=f"{sum(c.realized_pnl for c in closed):+,.2f}", cur=cur),
                                    u.id, "daily_report")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily job failed")


bot = TelegramBot()
