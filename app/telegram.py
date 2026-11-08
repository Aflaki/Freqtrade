"""One shared Telegram bot: per-user notifications + Freqtrade-like commands.

Users link their chat by opening  t.me/<bot>?start=<token>  from the website.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import httpx
from sqlalchemy import func, select

from .db import ExchangeAccount, SessionLocal, Trade, User, get_global
from .settings import config

log = logging.getLogger("relay.telegram")

HELP = (
    "<b>دستورات</b>\n"
    "/status — پوزیشن‌های باز\n"
    "/profit — سود و زیان بسته‌شده\n"
    "/balance — موجودی صرافی‌ها\n"
    "/pause — توقف ورودهای جدید (خروج‌ها انجام می‌شوند)\n"
    "/resume — ادامه معاملات\n"
    "/help — راهنما"
)


def esc(v: Any) -> str:
    return html.escape(str(v))


def fmt_num(v: float | None, digits: int = 6) -> str:
    if v is None:
        return "-"
    return f"{v:,.{digits}g}" if abs(v) < 1 else f"{v:,.2f}" if abs(v) >= 100 else f"{v:,.4f}"


class TelegramBot:
    def __init__(self, token: str):
        self.token = token
        self.username: str | None = None
        self._client = httpx.AsyncClient(timeout=40)
        self._task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def send(self, chat_id: str | int, text: str) -> bool:
        if not self.enabled or not chat_id:
            return False
        for attempt in range(3):
            try:
                r = await self._client.post(self._url("sendMessage"), json={
                    "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
                if r.status_code == 429:
                    await asyncio.sleep(float(r.json().get("parameters", {}).get("retry_after", 2)))
                    continue
                if r.status_code >= 400:
                    log.warning("telegram send failed %s: %s", r.status_code, r.text[:200])
                return r.status_code < 400
            except httpx.HTTPError as e:
                log.warning("telegram send error: %s", e)
                await asyncio.sleep(1 + attempt)
        return False

    def notify(self, user: User, kind: str, text: str) -> None:
        """Fire-and-forget: never blocks or breaks trading."""
        if not (self.enabled and user.telegram_chat_id and user.wants(kind)):
            return
        t = asyncio.create_task(self.send(user.telegram_chat_id, text))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------ polling
    async def start(self) -> None:
        if not self.enabled:
            log.info("TELEGRAM_BOT_TOKEN not set: telegram disabled")
            return
        try:
            r = await self._client.get(self._url("getMe"))
            self.username = r.json()["result"]["username"]
            await self._client.post(self._url("deleteWebhook"))
            log.info("telegram bot @%s ready", self.username)
        except Exception as e:
            log.error("telegram getMe failed: %s", e)
            return
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._client.aclose()

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
                            await self.handle(str(msg["chat"]["id"]), msg["text"].strip())
                        except Exception:
                            log.exception("telegram command failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram poll error: %s", e)
                await asyncio.sleep(5)

    # ------------------------------------------------------------ commands
    async def handle(self, chat_id: str, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/start" and arg:
            return await self._link(chat_id, arg.strip())
        with SessionLocal() as s:
            user = s.scalars(select(User).where(User.telegram_chat_id == chat_id)).first()
        if user is None:
            return await self.send(chat_id, "این تلگرام به حسابی وصل نیست. از داخل سایت روی «اتصال تلگرام» بزنید.")
        handlers = {"/status": self._status, "/profit": self._profit, "/balance": self._balance,
                    "/pause": self._pause, "/stop": self._pause, "/resume": self._resume}
        h = handlers.get(cmd)
        if h is None:
            return await self.send(chat_id, HELP)
        await h(chat_id, user)

    async def _link(self, chat_id: str, token: str) -> None:
        with SessionLocal() as s:
            user = s.scalars(select(User).where(User.telegram_link_token == token)).first()
            if user is None:
                return await self.send(chat_id, "لینک نامعتبر یا منقضی است. دوباره از سایت امتحان کنید.")
            # one chat per user
            for other in s.scalars(select(User).where(User.telegram_chat_id == chat_id, User.id != user.id)):
                other.telegram_chat_id = None
            user.telegram_chat_id = chat_id
            user.telegram_link_token = None
            s.commit()
            name = user.name or user.email
        await self.send(chat_id, f"✅ تلگرام به حساب <b>{esc(name)}</b> وصل شد.\n\n{HELP}")

    async def _status(self, chat_id: str, user: User) -> None:
        from .engine.exchange import pool
        with SessionLocal() as s:
            trades = s.scalars(select(Trade).where(Trade.user_id == user.id, Trade.status == "open")).all()
            accs = {a.id: a for a in s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id))}
        if not trades:
            return await self.send(chat_id, "پوزیشن بازی ندارید.")
        lines = ["<b>پوزیشن‌های باز</b>"]
        for t in trades:
            acc = accs.get(t.account_id)
            cur = None
            if acc:
                try:
                    cur = await (await pool.get(acc)).price(t.symbol)
                except Exception:
                    pass
            pnl = ""
            if cur:
                diff = (cur - t.entry_price) * (1 if t.side == "long" else -1)
                upnl = diff * t.amount * t.contract_size
                pct = upnl / t.stake * 100 if t.stake else 0
                pnl = f"\nسود/زیان شناور: <b>{upnl:+,.2f}</b> ({pct:+.2f}%)"
            lines.append(
                f"\n#{t.id} <b>{esc(t.pair)}</b> {'🟢 Long' if t.side == 'long' else '🔴 Short'} — "
                f"{esc(acc.label or acc.exchange) if acc else ''}\n"
                f"ورود: {fmt_num(t.entry_price)} | فعلی: {fmt_num(cur)} | مارجین: {t.stake:,.2f}{pnl}")
        await self.send(chat_id, "\n".join(lines))

    async def _profit(self, chat_id: str, user: User) -> None:
        with SessionLocal() as s:
            row = s.execute(select(func.count(Trade.id), func.coalesce(func.sum(Trade.realized_pnl), 0),
                                   func.coalesce(func.sum(Trade.stake), 0))
                            .where(Trade.user_id == user.id, Trade.status == "closed")).one()
            wins = s.scalar(select(func.count(Trade.id)).where(
                Trade.user_id == user.id, Trade.status == "closed", Trade.realized_pnl > 0)) or 0
            cur = get_global(s).stake_currency
        n, pnl, stake = row
        if not n:
            return await self.send(chat_id, "هنوز معامله بسته‌شده‌ای ندارید.")
        await self.send(chat_id, (
            f"<b>سود و زیان</b>\nمعاملات بسته‌شده: {n}\nبرد: {wins} | باخت: {n - wins} "
            f"({wins / n * 100:.0f}% برد)\nمجموع: <b>{pnl:+,.2f} {cur}</b> ({(pnl / stake * 100) if stake else 0:+.2f}% از مارجین)"))

    async def _balance(self, chat_id: str, user: User) -> None:
        from .engine.exchange import pool
        with SessionLocal() as s:
            accs = s.scalars(select(ExchangeAccount).where(ExchangeAccount.user_id == user.id)).all()
            cur = get_global(s).stake_currency
        if not accs:
            return await self.send(chat_id, "هنوز صرافی‌ای اضافه نکرده‌اید.")
        lines = ["<b>موجودی</b>"]
        for a in accs:
            try:
                bal = await (await pool.get(a)).free_balance(cur) if not a.dry_run else None
                txt = f"{bal:,.2f} {cur} آزاد" if bal is not None else "حالت آزمایشی"
            except Exception as e:
                txt = f"خطا: {esc(str(e)[:80])}"
            lines.append(f"{esc(a.label or a.exchange)} ({a.trading_mode}): {txt} — سرمایه تعیین‌شده: {a.capital:,.2f}")
        await self.send(chat_id, "\n".join(lines))

    async def _set_trading(self, chat_id: str, user: User, on: bool) -> None:
        with SessionLocal() as s:
            u = s.get(User, user.id)
            u.trading_enabled = on
            s.commit()
        await self.send(chat_id, "▶️ معاملات فعال شد." if on else
                        "⏸ ورودهای جدید متوقف شد. پوزیشن‌های باز طبق سیگنال بسته می‌شوند. /resume برای ادامه.")

    async def _pause(self, chat_id: str, user: User) -> None:
        await self._set_trading(chat_id, user, False)

    async def _resume(self, chat_id: str, user: User) -> None:
        await self._set_trading(chat_id, user, True)


bot = TelegramBot(config.telegram_bot_token)
