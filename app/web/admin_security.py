"""Admin → Sign-in & security: social login providers, CAPTCHA, SMTP mail server and password / 2FA policies."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import captcha, mailer, oauth
from ..db import SessionLocal, get_global
from ..security import encrypt, flash, form_with_csrf, require_admin
from .common import render, tr

router = APIRouter(prefix="/admin/security")


@router.get("")
async def page(request: Request):
    require_admin(request)
    from ..telegram import bot
    with SessionLocal() as s:
        gs = get_global(s)
    base = (gs.base_url or str(request.base_url)).rstrip("/")
    cfg = gs.auth_providers or {}
    return render(request, "admin/security.html", gs=gs, tab="security", PROVIDERS=oauth.PROVIDERS, cfg=cfg,
                  callbacks={p: f"{base}/auth/{p}/callback" for p in oauth.PROVIDERS}, base=base,
                  domain=base.split("://", 1)[-1].split("/")[0], enabled=oauth.enabled_providers(),
                  CAPTCHAS=captcha.PROVIDERS, bot_username=bot.username, mail_ok=mailer.configured())


def _num(f, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(f.get(name, default)).strip())))
    except ValueError:
        return default


@router.post("")
async def save(request: Request):
    require_admin(request)
    f = await form_with_csrf(request)
    section = str(f.get("section", ""))
    with SessionLocal() as s:
        gs = get_global(s)
        if section == "providers":
            allp = dict(gs.auth_providers or {})
            for p, meta in oauth.PROVIDERS.items():
                c = dict(allp.get(p) or {})
                c["enabled"] = bool(f.get(f"{p}__enabled"))
                if not meta.get("widget"):
                    c["client_id"] = str(f.get(f"{p}__client_id", "")).strip()[:300]
                    secret = str(f.get(f"{p}__secret", "")).strip()
                    if secret:
                        c["secret_enc"] = encrypt(secret)
                    if f.get(f"{p}__clear"):
                        c.pop("secret_enc", None)
                        c.pop("private_key_enc", None)
                if p == "apple":
                    c["team_id"] = str(f.get("apple__team_id", "")).strip()[:20]
                    c["key_id"] = str(f.get("apple__key_id", "")).strip()[:20]
                    key = str(f.get("apple__private_key", "")).strip()
                    if key:
                        if "BEGIN PRIVATE KEY" not in key:
                            flash(request, tr(request, "err_apple_key"), "error")
                        else:
                            c["private_key_enc"] = encrypt(key)
                allp[p] = c
            gs.auth_providers = allp
        elif section == "captcha":
            prov = str(f.get("captcha_provider", ""))
            gs.captcha_provider = prov if prov in captcha.PROVIDERS else ""
            gs.captcha_site_key = str(f.get("captcha_site_key", "")).strip()[:200]
            secret = str(f.get("captcha_secret", "")).strip()
            if secret:
                gs.captcha_secret_enc = encrypt(secret)
            if not gs.captcha_provider:
                gs.captcha_secret_enc, gs.captcha_site_key = None, ""
        elif section == "smtp":
            gs.smtp_host = str(f.get("smtp_host", "")).strip()[:200]
            gs.smtp_port = _num(f, "smtp_port", 587, 1, 65535)
            gs.smtp_user = str(f.get("smtp_user", "")).strip()[:200]
            pw = str(f.get("smtp_pass", ""))
            if pw:
                gs.smtp_pass_enc = encrypt(pw)
            if f.get("smtp_clear"):
                gs.smtp_pass_enc = None
            gs.smtp_from = str(f.get("smtp_from", "")).strip()[:200]
            sec = str(f.get("smtp_security", "starttls"))
            gs.smtp_security = sec if sec in ("starttls", "ssl", "none") else "starttls"
        elif section == "policy":
            gs.password_min_length = _num(f, "password_min_length", 10, 8, 64)
            gs.check_breached_passwords = bool(f.get("check_breached_passwords"))
            gs.require_email_verification = bool(f.get("require_email_verification"))
            gs.require_2fa_admins = bool(f.get("require_2fa_admins"))
            gs.login_alerts = bool(f.get("login_alerts"))
        s.commit()
    flash(request, tr(request, "msg_saved"))
    return RedirectResponse(f"/admin/security#{section}", 303)


@router.post("/test-mail")
async def test_mail(request: Request):
    admin = require_admin(request)
    f = await form_with_csrf(request)
    to = str(f.get("to", "")).strip() or admin.email
    import asyncio

    from ..auth.mailer import _send_sync
    try:
        await asyncio.to_thread(_send_sync, to, "✅ SMTP test", "SMTP works.", "<p>SMTP works.</p>")
        flash(request, tr(request, "msg_mail_test_ok", to=to))
    except Exception as e:  # show the real SMTP error to the admin
        flash(request, f"SMTP: {type(e).__name__}: {str(e)[:300]}", "error")
    return RedirectResponse("/admin/security#smtp", 303)


