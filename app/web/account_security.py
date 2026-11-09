"""Settings → Security: two-factor authentication, recovery codes, sign-in methods, password,
anti-phishing code, sessions and e-mail verification."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..auth import mailer, oauth, twofa
from ..db import Identity, SessionLocal, User, get_global
from ..security import encrypt, flash, form_with_csrf, hash_password, require_user, verify_password
from .auth import PLACEHOLDER_DOMAIN, _count, _hit, _password_errors, _send_verification, security_alert
from .common import render, tr

router = APIRouter(prefix="/settings/security")
BACK = "/settings/security"


def _page(request: Request, user: User, status_code: int = 200, **kw):
    from ..telegram import bot
    with SessionLocal() as s:
        u = s.get(User, user.id)
        idents = list(s.scalars(select(Identity).where(Identity.user_id == u.id).order_by(Identity.id)))
        gs = get_global(s)
        min_len, site = gs.password_min_length, gs.site_name
        setup = None
        if u.totp_secret_enc and not u.totp_enabled:  # set-up started, waiting for the first code
            from ..security import decrypt
            secret = decrypt(u.totp_secret_enc) or ""
            label = u.email if not u.email.endswith(PLACEHOLDER_DOMAIN) else (u.name or f"user{u.id}")
            uri = twofa.provisioning_uri(secret, label, site)
            setup = {"secret": secret, "qr": twofa.qr_svg(uri), "uri": uri}
    linked = {i.provider for i in idents}
    checks = [(bool(u.email_verified and not u.email.endswith(PLACEHOLDER_DOMAIN)), "chk_email"),
              (u.totp_enabled, "chk_2fa"), (bool(u.anti_phishing), "chk_antiphishing"), (bool(u.telegram_chat_id), "chk_telegram")]
    return render(request, "security.html", status_code=status_code, user=u, idents=idents, setup=setup,
                  checks=checks, done=sum(ok for ok, _ in checks), tg_bot=bot.username,
                  providers=oauth.enabled_providers(), PROVIDERS=oauth.PROVIDERS, linked=linked, min_len=min_len,
                  mail_ok=mailer.configured(), placeholder=u.email.endswith(PLACEHOLDER_DOMAIN),
                  bot_username=bot.username, recovery_left=len(u.recovery_codes or []), **kw)


@router.get("")
async def security_page(request: Request):
    return _page(request, require_user(request))


def _code_ok(u: User, code: str, allow_recovery: bool = True) -> bool:
    return twofa.check_code(u.totp_secret_enc, code) or (
        allow_recovery and twofa.use_recovery_code(u.recovery_codes, code) is not None)


# ------------------------------------------------------------------ 2FA
@router.post("/2fa/start")
async def twofa_start(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        if not u.totp_enabled:
            u.totp_secret_enc = encrypt(twofa.new_secret())
            s.commit()
    return RedirectResponse(BACK + "#twofa", 303)


@router.post("/2fa/cancel")
async def twofa_cancel(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        if not u.totp_enabled:
            u.totp_secret_enc = None
            s.commit()
    return RedirectResponse(BACK + "#twofa", 303)


@router.post("/2fa/confirm")
async def twofa_confirm(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    if _count(f"2fa-setup:{user.id}", 900) >= 8:
        flash(request, tr(request, "err_too_many"), "error")
        return RedirectResponse(BACK, 303)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        if u.totp_enabled or not twofa.check_code(u.totp_secret_enc, str(f.get("code", ""))):
            _hit(f"2fa-setup:{user.id}")
            flash(request, tr(request, "err_2fa_code"), "error")
            return RedirectResponse(BACK + "#twofa", 303)
        codes, hashes = twofa.new_recovery_codes()
        u.totp_enabled, u.recovery_codes = True, hashes
        s.commit()
        fresh = s.get(User, u.id)
    await security_alert(fresh, "alert_2fa_on", request)
    flash(request, tr(request, "msg_2fa_on"))
    return _page(request, fresh, new_codes=codes)  # shown exactly once, never stored in clear text


@router.post("/2fa/disable")
async def twofa_disable(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        gs = get_global(s)
        if u.is_admin and gs.require_2fa_admins:
            flash(request, tr(request, "msg_admin_2fa_required"), "error")
            return RedirectResponse(BACK + "#twofa", 303)
        if not u.totp_enabled or not _code_ok(u, str(f.get("code", ""))):
            _hit(f"2fa-setup:{user.id}")
            flash(request, tr(request, "err_2fa_code"), "error")
            return RedirectResponse(BACK + "#twofa", 303)
        u.totp_enabled, u.totp_secret_enc, u.recovery_codes = False, None, None
        s.commit()
        fresh = s.get(User, u.id)
    await security_alert(fresh, "alert_2fa_off", request)
    flash(request, tr(request, "msg_2fa_off"), "warn")
    return RedirectResponse(BACK + "#twofa", 303)


@router.post("/2fa/recovery")
async def twofa_recovery(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        if not u.totp_enabled or not twofa.check_code(u.totp_secret_enc, str(f.get("code", ""))):
            flash(request, tr(request, "err_2fa_code"), "error")
            return RedirectResponse(BACK + "#twofa", 303)
        codes, u.recovery_codes = twofa.new_recovery_codes()
        s.commit()
        fresh = s.get(User, u.id)
    flash(request, tr(request, "msg_recovery_new"))
    return _page(request, fresh, new_codes=codes)


# ------------------------------------------------------------------ password
@router.post("/password")
async def change_password(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    pw = str(f.get("new", ""))
    if user.has_password and not verify_password(str(f.get("old", "")), user.password_hash):
        _hit(f"pw:{user.id}")
        flash(request, tr(request, "err_old_password"), "error")
        return RedirectResponse(BACK + "#password", 303)
    errors = [tr(request, "err_password_match")] if pw != str(f.get("new2", "")) else []
    if not errors:
        errors = await _password_errors(request, pw, user.email, user.name)
    if errors:
        for e in errors:
            flash(request, e, "error")
        return RedirectResponse(BACK + "#password", 303)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        u.password_hash = hash_password(pw)
        u.session_version = (u.session_version or 1) + 1  # other devices are logged out, this one stays
        s.commit()
        request.session["sv"] = u.session_version
        fresh = s.get(User, u.id)
    await security_alert(fresh, "alert_password_changed", request)
    flash(request, tr(request, "msg_password_changed"))
    return RedirectResponse(BACK + "#password", 303)


# ------------------------------------------------------------------ sign-in methods
@router.post("/unlink/{ident_id}")
async def unlink(ident_id: int, request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        ident = s.get(Identity, ident_id)
        if ident is None or ident.user_id != user.id:
            return RedirectResponse(BACK, 303)
        others = s.scalars(select(Identity).where(Identity.user_id == user.id, Identity.id != ident.id)).all()
        if not others and not user.has_password:  # never lock the user out
            flash(request, tr(request, "err_last_method"), "error")
            return RedirectResponse(BACK + "#methods", 303)
        name = oauth.PROVIDERS.get(ident.provider, {}).get("name", ident.provider)
        s.delete(ident)
        s.commit()
    flash(request, tr(request, "msg_identity_unlinked", provider=name))
    return RedirectResponse(BACK + "#methods", 303)


# ------------------------------------------------------------------ anti-phishing, sessions, e-mail
@router.post("/antiphishing")
async def antiphishing(request: Request):
    user = require_user(request)
    f = await form_with_csrf(request)
    code = " ".join(str(f.get("code", "")).split())[:32]
    with SessionLocal() as s:
        s.get(User, user.id).anti_phishing = code
        s.commit()
    flash(request, tr(request, "msg_saved"))
    return RedirectResponse(BACK + "#antiphishing", 303)


@router.post("/logout-others")
async def logout_others(request: Request):
    user = require_user(request)
    await form_with_csrf(request)
    with SessionLocal() as s:
        u = s.get(User, user.id)
        u.session_version = (u.session_version or 1) + 1
        s.commit()
        request.session["sv"] = u.session_version
    flash(request, tr(request, "msg_logged_out_others"))
    return RedirectResponse(BACK + "#sessions", 303)


@router.post("/email")
async def change_email(request: Request):
    """Change the e-mail address (needs the password, or a 2FA code for accounts without one)."""
    from .auth import EMAIL_RE
    user = require_user(request)
    f = await form_with_csrf(request)
    email = str(f.get("email", "")).strip().lower()
    proof = str(f.get("password", ""))
    ok = verify_password(proof, user.password_hash) if user.has_password else (
        not user.totp_enabled or twofa.check_code(user.totp_secret_enc, proof))
    if not ok:
        flash(request, tr(request, "err_old_password"), "error")
        return RedirectResponse(BACK + "#email", 303)
    if not EMAIL_RE.match(email):
        flash(request, tr(request, "err_email"), "error")
        return RedirectResponse(BACK + "#email", 303)
    with SessionLocal() as s:
        if s.scalars(select(User).where(User.email == email, User.id != user.id)).first():
            flash(request, tr(request, "err_email_taken"), "error")
            return RedirectResponse(BACK + "#email", 303)
        u = s.get(User, user.id)
        old_email = u.email
        u.email, u.email_verified = email, False
        u.session_version = (u.session_version or 1) + 1
        s.commit()
        request.session["sv"] = u.session_version
        fresh = s.get(User, u.id)
    if not old_email.endswith(PLACEHOLDER_DOMAIN):  # warn the old address
        from types import SimpleNamespace
        old = SimpleNamespace(id=fresh.id, lang=fresh.lang, anti_phishing=fresh.anti_phishing,
                              telegram_chat_id=fresh.telegram_chat_id, email=old_email, email_verified=True)
        await security_alert(old, "alert_email_changed", request, email=email)
    await _send_verification(request, fresh)
    flash(request, tr(request, "msg_saved"))
    return RedirectResponse(BACK + "#email", 303)
