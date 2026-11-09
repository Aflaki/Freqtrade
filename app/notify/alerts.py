"""Critical alerts (a bot died, was restarted, is stuck…) reach the phone even when the busy main bot is muted:

* Web Push to the user's phone / browser (the site installed as an app; iOS 16.4+), and
* a second Telegram bot used only for these alerts — its chat can stay unmuted.

Telegram private chat ids are the user's Telegram id, the same for every bot, so the alert bot can write to the
chat id we already know, once the user has pressed Start in the alert bot.
"""
from __future__ import annotations

import html
import logging
import re

import httpx

from ..db import SessionLocal, get_global
from ..security import decrypt
from . import webpush

log = logging.getLogger("relay.alerts")
_username: dict[str, str] = {}


def alert_bot_token() -> str:
    with SessionLocal() as s:
        return decrypt(get_global(s).alert_bot_token_enc) or ""


async def alert_bot_username(token: str | None = None) -> str:
    token = token if token is not None else alert_bot_token()
    if not token:
        return ""
    if token not in _username:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = (await c.get(f"https://api.telegram.org/bot{token}/getMe")).json()
            _username[token] = (r.get("result") or {}).get("username", "") if r.get("ok") else ""
        except Exception as e:
            log.warning("alert bot getMe failed: %s", e)
            return ""
    return _username[token]


async def send_alert_bot(chat_id: str | int, text_html: str) -> tuple[bool, str]:
    token = alert_bot_token()
    if not token or not chat_id:
        return False, "not configured"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = (await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text_html[:4000], "parse_mode": "HTML",
                                    "disable_web_page_preview": True})).json()
        if r.get("ok"):
            return True, ""
        return False, str(r.get("description", "error"))[:200]  # e.g. "chat not found" = Start not pressed yet
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def plain(text_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text_html)).strip()


async def phone(user, title: str, text_html: str, url: str = "/admin/bot", tag: str = "") -> dict:
    """Send one critical alert to a user's phone through every configured channel."""
    out = {"push": 0, "alert_bot": False}
    try:
        out["push"] = await webpush.send_to_user(user.id, title, plain(text_html)[:900], url, tag)
    except Exception as e:
        log.warning("push failed: %s", e)
    if user.telegram_chat_id:
        out["alert_bot"], _ = await send_alert_bot(user.telegram_chat_id, f"🚨 <b>{html.escape(title)}</b>\n{text_html}")
    return out
