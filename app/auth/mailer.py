"""E-mail via the SMTP server configured in the admin panel (verification, password reset, security alerts)."""
from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from ..db import SessionLocal, get_global
from ..security import decrypt

log = logging.getLogger("relay.mail")


def configured() -> bool:
    with SessionLocal() as s:
        gs = get_global(s)
        return bool(gs.smtp_host and gs.smtp_from)


def _send_sync(to: str, subject: str, text: str, html_body: str) -> None:
    with SessionLocal() as s:
        gs = get_global(s)
        host, port, user, pw = gs.smtp_host, gs.smtp_port, gs.smtp_user, decrypt(gs.smtp_pass_enc) or ""
        sender, mode, site = gs.smtp_from, gs.smtp_security, gs.site_name
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((site, sender))
    msg["To"] = to
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as srv:
            if user:
                srv.login(user, pw)
            srv.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as srv:
            if mode == "starttls":
                srv.starttls(context=ctx)
            if user:
                srv.login(user, pw)
            srv.send_message(msg)


async def send(to: str, subject: str, lines: list[str], button: tuple[str, str] | None = None,
               anti_phishing: str = "", dir_: str = "ltr") -> bool:
    """Plain + HTML mail. `anti_phishing` (the user's own code) is shown at the top of every mail."""
    if not configured():
        return False
    text = "\n\n".join(lines + ([f"{button[0]}: {button[1]}"] if button else []))
    if anti_phishing:
        text = f"[{anti_phishing}]\n\n" + text
    body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
    if button:
        body += (f'<p><a href="{html.escape(button[1])}" style="display:inline-block;padding:10px 18px;border-radius:8px;'
                 f'background:#6d5dfc;color:#fff;text-decoration:none">{html.escape(button[0])}</a></p>'
                 f'<p style="font-size:12px;color:#777">{html.escape(button[1])}</p>')
    ap = (f'<p style="font-size:12px;color:#777;border:1px dashed #bbb;padding:6px 10px;border-radius:6px">'
          f'🔐 {html.escape(anti_phishing)}</p>') if anti_phishing else ""
    html_body = f'<div dir="{dir_}" style="font-family:Tahoma,Arial,sans-serif;font-size:15px;line-height:1.7">{ap}{body}</div>'
    try:
        await asyncio.to_thread(_send_sync, to, subject, text, html_body)
        return True
    except Exception as e:
        log.warning("mail to %s failed: %s", to, e)
        return False
