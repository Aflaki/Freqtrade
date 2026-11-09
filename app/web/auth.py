"""Sign-up, login (password / Google / Apple / Microsoft / Yahoo / GitHub / Telegram), 2FA step,
profile completion, e-mail verification, forgotten password. Bot protection on every form."""
from __future__ import annotations

import datetime as dt
import html
import re
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from ..auth import captcha, mailer, oauth, passwords, tokens, twofa
from ..db import Identity, SessionLocal, User, get_global, utcnow
from ..i18n import LANGS, t
from ..security import flash, form_with_csrf, hash_password, verify_password
from .common import get_lang, render, tr

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLACEHOLDER_DOMAIN = "@users.invalid"  # social sign-ups without an e-mail until the profile is completed
COUNTRIES = ("IR DE AT CH US GB CA AU TR AE NL FR ES IT SE NO DK FI BE PL CZ PT IE GR RO HU UA RU IN PK AF IQ SA QA KW OM BH "
             "AZ AM GE KZ UZ TJ KG TM EG MA DZ TN NG ZA KE BR AR MX CL CO PE VE JP CN KR HK TW SG MY ID TH VN PH NZ IL "
             "LB JO SY YE LU LI MC MT CY EE LV LT SK SI HR RS BA BG AL MK ME IS BY MD").split()
EXPERIENCE = ("beginner", "intermediate", "pro")
_hits: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def _count(key: str, window: int) -> int:
    now = time.time()
    _hits[key] = [x for x in _hits.get(key, []) if now - x < window]
    return len(_hits[key])


def _hit(key: str) -> None:
    _hits.setdefault(key, []).append(time.time())


def _ctx(request: Request, **kw) -> dict:
    from ..telegram import bot
    with SessionLocal() as s:
        gs = get_global(s)
        min_len = gs.password_min_length
    return dict(providers=oauth.enabled_providers(), PROVIDERS=oauth.PROVIDERS, captcha=captcha.widget(),
                ft=captcha.form_token(), honeypot=captcha.HONEYPOT, countries=COUNTRIES, experience=EXPERIENCE,
                min_len=min_len, tg_bot=bot.username, **kw)


async def _bot_or_captcha_failed(request: Request, f, need_captcha: bool = True) -> str | None:
    if captcha.looks_like_bot(f):
        return "err_bot"
    if need_captcha and not await captcha.verify(f, client_ip(request)):
        return "err_captcha"
    return None


async def _password_errors(request: Request, pw: str, email: str, name: str) -> list[str]:
    with SessionLocal() as s:
        gs = get_global(s)
        min_len, check_breach = gs.password_min_length, gs.check_breached_passwords
    errs = passwords.problems(pw, email, name, min_len)
    if not errs and check_breach and await passwords.breached(pw):
        errs.append("pw_breached")
    return [tr(request, k, n=min_len) for k in errs]


# ------------------------------------------------------------------ login completion, alerts
async def security_alert(u: User, key: str, request: Request, **kw) -> None:
    """Telegram + e-mail notice on sign-in and security changes (with the user's anti-phishing code)."""
    from ..telegram import bot, esc
    with SessionLocal() as s:
        gs = get_global(s)
        if not gs.login_alerts and key == "alert_login":
            return
        site = gs.site_name
    ua = request.headers.get("user-agent", "")[:120]
    when = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    ap = f"\n🔐 {u.anti_phishing}" if u.anti_phishing else ""
    text = t(u.lang, key, ip=client_ip(request), when=when, device=ua, **kw)
    if u.telegram_chat_id:
        await bot.send(u.telegram_chat_id, f"🛡 <b>{esc(site)}</b>\n{esc(text)}{esc(ap)}", u.id, "security")
    if u.email_verified and not u.email.endswith(PLACEHOLDER_DOMAIN):
        await mailer.send(u.email, f"{site}: {t(u.lang, 'mail_security_subject')}",
                          [text, t(u.lang, 'mail_not_you')], anti_phishing=u.anti_phishing, dir_=LANGS[u.lang]["dir"])


async def complete_login(request: Request, user_id: int, method: str) -> RedirectResponse:
    """Password/social step done. Next: 2FA if enabled, then the session, alerts and any missing setup."""
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if u.status == "suspended":
            flash(request, tr(request, "err_suspended"), "error")
            return RedirectResponse("/login", 303)
        if u.totp_enabled:
            request.session.clear()
            request.session["pending_2fa"] = {"uid": u.id, "m": method, "t": int(time.time())}
            return RedirectResponse("/login/2fa", 303)
    return await _start_session(request, user_id, method)


async def _start_session(request: Request, user_id: int, method: str) -> RedirectResponse:
    with SessionLocal() as s:
        u = s.get(User, user_id)
        u.last_login_at, u.last_login_ip = utcnow(), client_ip(request)[:64]
        s.commit()
        gs = get_global(s)
        need_2fa_setup = u.is_admin and gs.require_2fa_admins and not u.totp_enabled
    request.session.clear()
    request.session.update({"uid": u.id, "sv": u.session_version})
    await security_alert(u, "alert_login", request, method=method)
    if u.profile_completed is False:
        return RedirectResponse("/complete-profile", 303)
    if need_2fa_setup:
        flash(request, tr(request, "msg_admin_2fa_required"), "warn")
        return RedirectResponse("/settings/security", 303)
    return RedirectResponse("/dashboard", 303)


# ------------------------------------------------------------------ register
@router.get("/register")
async def register_form(request: Request):
    return render(request, "register.html", **_ctx(request, form={}))


@router.post("/register")
async def register(request: Request):
    f = await form_with_csrf(request)
    ip = client_ip(request)
    form = {k: str(f.get(k, "")).strip() for k in ("email", "name", "phone", "country", "experience", "referral")}
    form["email"] = form["email"].lower()
    errors: list[str] = []
    bad = await _bot_or_captcha_failed(request, f)
    if bad:
        errors.append(tr(request, bad))
    if _count(f"reg:{ip}", 3600) >= 5:
        errors.append(tr(request, "err_too_many"))
    pw, pw2 = str(f.get("password", "")), str(f.get("password2", ""))
    if not EMAIL_RE.match(form["email"]):
        errors.append(tr(request, "err_email"))
    if len(form["name"]) < 2:
        errors.append(tr(request, "err_name"))
    if pw != pw2:
        errors.append(tr(request, "err_password_match"))
    if not errors:
        errors += await _password_errors(request, pw, form["email"], form["name"])
    if not f.get("terms"):
        errors.append(tr(request, "err_terms"))
    with SessionLocal() as s:
        if s.scalar(select(func.count(User.id)).where(User.email == form["email"])):
            errors.append(tr(request, "err_email_taken"))
    if errors:
        _hit(f"reg:{ip}")
        for e in errors:
            flash(request, e, "error")
        return render(request, "register.html", status_code=400, **_ctx(request, form=form))
    _hit(f"reg:{ip}")
    u = _create_user(request, email=form["email"], name=form["name"][:120], password=pw, phone=form["phone"][:32],
                     country=form["country"] if form["country"] in COUNTRIES else "",
                     experience=form["experience"] if form["experience"] in EXPERIENCE else "",
                     referral=form["referral"][:80], marketing=bool(f.get("marketing")), verified=False)
    await _send_verification(request, u)
    flash(request, tr(request, "msg_registered") + " " + (tr(request, "msg_you_are_admin") if u.is_admin else
          tr(request, "msg_pending") if u.status == "pending" else ""))
    return await _start_session(request, u.id, "password")


def _create_user(request: Request, *, email: str, name: str, password: str | None, phone: str = "", country: str = "",
                 experience: str = "", referral: str = "", marketing: bool = False, verified: bool = False,
                 profile_completed: bool | None = True) -> User:
    with SessionLocal() as s:
        gs = get_global(s)
        first = not s.scalar(select(func.count(User.id)))
        u = User(email=email, name=name, phone=phone, country=country, experience=experience, referral_source=referral,
                 marketing_opt_in=marketing, lang=get_lang(request),
                 password_hash=hash_password(password) if password else "!" + secrets.token_hex(16),
                 accepted_terms_at=utcnow() if profile_completed else None, is_admin=first,
                 status="active" if (first or not gs.require_approval) else "pending", email_verified=verified,
                 profile_completed=profile_completed,
                 subscription_until=(utcnow() + dt.timedelta(days=gs.trial_days)) if gs.trial_days and not first else None)
        s.add(u)
        s.commit()
        return u


async def _send_verification(request: Request, u: User) -> bool:
    if u.email_verified or u.email.endswith(PLACEHOLDER_DOMAIN) or not mailer.configured():
        return False
    with SessionLocal() as s:
        base = get_global(s).base_url.rstrip("/")
    link = f"{base}/verify-email/{tokens.make(u, 'verify')}"
    return await mailer.send(u.email, t(u.lang, "mail_verify_subject"), [t(u.lang, "mail_verify_body", name=u.name or "")],
                             (t(u.lang, "mail_verify_button"), link), u.anti_phishing, LANGS[u.lang]["dir"])


# ------------------------------------------------------------------ login (password) + 2FA step
@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html", **_ctx(request, need_captcha=_count(f"login:{client_ip(request)}", 900) >= 3))


@router.post("/login")
async def login(request: Request):
    f = await form_with_csrf(request)
    ip = client_ip(request)
    email = str(f.get("email", "")).strip().lower()
    failures = _count(f"login:{ip}", 900)
    ctx = lambda: _ctx(request, need_captcha=_count(f"login:{ip}", 900) >= 3, email=email)  # noqa: E731
    if failures >= 20 or _count(f"login:{ip}:{email}", 900) >= 8:
        flash(request, tr(request, "err_too_many"), "error")
        return render(request, "login.html", status_code=429, **ctx())
    bad = await _bot_or_captcha_failed(request, f, need_captcha=failures >= 3)
    if bad:
        _hit(f"login:{ip}")
        flash(request, tr(request, bad), "error")
        return render(request, "login.html", status_code=400, **ctx())
    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.email == email)).first()
    if u is None or not u.has_password or not verify_password(str(f.get("password", "")), u.password_hash):
        _hit(f"login:{ip}")
        _hit(f"login:{ip}:{email}")
        flash(request, tr(request, "err_login"), "error")
        return render(request, "login.html", status_code=401, **ctx())
    return await complete_login(request, u.id, "password")


@router.get("/login/2fa")
async def twofa_form(request: Request):
    if not request.session.get("pending_2fa"):
        return RedirectResponse("/login", 303)
    return render(request, "login_2fa.html")


@router.post("/login/2fa")
async def twofa_check(request: Request):
    f = await form_with_csrf(request)
    pend = request.session.get("pending_2fa") or {}
    if not pend or time.time() - pend.get("t", 0) > 600:
        request.session.pop("pending_2fa", None)
        flash(request, tr(request, "err_2fa_expired"), "error")
        return RedirectResponse("/login", 303)
    if _count(f"2fa:{pend['uid']}", 900) >= 6:
        flash(request, tr(request, "err_too_many"), "error")
        return render(request, "login_2fa.html", status_code=429)
    code = str(f.get("code", "")).strip()
    with SessionLocal() as s:
        u = s.get(User, pend["uid"])
        ok = twofa.check_code(u.totp_secret_enc, code)
        used_recovery = False
        if not ok:
            rest = twofa.use_recovery_code(u.recovery_codes, code)
            if rest is not None:
                u.recovery_codes, ok, used_recovery = rest, True, True
                s.commit()
    if not ok:
        _hit(f"2fa:{pend['uid']}")
        flash(request, tr(request, "err_2fa_code"), "error")
        return render(request, "login_2fa.html", status_code=401)
    if used_recovery:
        flash(request, tr(request, "msg_recovery_used", n=len(u.recovery_codes or [])), "warn")
    return await _start_session(request, pend["uid"], pend.get("m", "password") + "+2fa")


@router.post("/logout")
async def logout(request: Request):
    await form_with_csrf(request)
    request.session.clear()
    return RedirectResponse("/", 303)


# ------------------------------------------------------------------ external providers
def _callback_url(provider: str) -> str:
    with SessionLocal() as s:
        return f"{get_global(s).base_url.rstrip('/')}/auth/{provider}/callback"


@router.get("/auth/{provider}/start")
async def oauth_start(provider: str, request: Request):
    if provider not in oauth.enabled_providers():
        flash(request, tr(request, "err_provider"), "error")
        return RedirectResponse("/login", 303)
    mode = "link" if request.query_params.get("mode") == "link" and request.session.get("uid") else "login"
    try:
        url = await oauth.start(provider, _callback_url(provider), request.session, mode)
    except Exception as e:
        flash(request, f"{oauth.PROVIDERS[provider]['name']}: {type(e).__name__}", "error")
        return RedirectResponse("/login", 303)
    return RedirectResponse(url, 303)


@router.get("/auth/{provider}/callback")
async def oauth_callback_get(provider: str, request: Request):
    if provider == "telegram":
        return await _telegram_login(request)
    return await _oauth_callback(provider, request, dict(request.query_params))


@router.post("/auth/{provider}/callback")  # Apple uses response_mode=form_post
async def oauth_callback_post(provider: str, request: Request):
    params = {k: str(v) for k, v in (await request.form()).items()}
    if "oauth" not in request.session and not params.pop("_relay", None):
        # Apple posts cross-site, so the browser leaves out our SameSite=Lax session cookie. Post the same fields
        # again from our own origin: then the cookie (with the saved state / nonce / PKCE verifier) is sent.
        fields = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in params.items())
        return HTMLResponse(f'<!doctype html><meta charset="utf-8"><body onload="document.forms[0].submit()">'
                            f'<form method="post" action="/auth/{html.escape(provider)}/callback">{fields}'
                            f'<input type="hidden" name="_relay" value="1"><noscript><button>Continue</button></noscript>'
                            f'</form></body>', headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    params.pop("_relay", None)
    return await _oauth_callback(provider, request, params)


async def _oauth_callback(provider: str, request: Request, params: dict):
    if provider not in oauth.PROVIDERS:
        return RedirectResponse("/login", 303)
    try:
        ext, mode = await oauth.finish(provider, params, _callback_url(provider), request.session)
    except Exception as e:
        flash(request, f"{oauth.PROVIDERS[provider]['name']}: {str(e)[:200]}", "error")
        return RedirectResponse("/settings/security" if request.session.get("uid") else "/login", 303)
    return await _external_login(request, ext, mode)


async def _telegram_login(request: Request):
    from ..telegram import bot
    if "telegram" not in oauth.enabled_providers():
        return RedirectResponse("/login", 303)
    try:
        ext = oauth.verify_telegram(dict(request.query_params), bot.token)
    except oauth.OAuthError as e:
        flash(request, f"Telegram: {e}", "error")
        return RedirectResponse("/login", 303)
    mode = "link" if request.session.get("uid") and request.query_params.get("link") else "login"
    return await _external_login(request, ext, mode)


async def _external_login(request: Request, ext: oauth.ExternalUser, mode: str):
    name = oauth.PROVIDERS[ext.provider]["name"]
    with SessionLocal() as s:
        ident = s.scalars(select(Identity).where(Identity.provider == ext.provider, Identity.subject == ext.subject)).first()
        current = request.session.get("uid")
        if mode == "link" and current:
            if ident and ident.user_id != current:
                flash(request, tr(request, "err_identity_taken", provider=name), "error")
            elif not ident:
                s.add(Identity(user_id=current, provider=ext.provider, subject=ext.subject, email=ext.email))
                _telegram_bind(s, current, ext)
                s.commit()
                flash(request, tr(request, "msg_identity_linked", provider=name))
            return RedirectResponse("/settings/security", 303)
        if ident:
            uid = ident.user_id
        else:
            existing = s.scalars(select(User).where(User.email == ext.email)).first() if ext.email and ext.email_verified else None
            if existing:  # same verified e-mail -> same person
                uid = existing.id
                if not existing.email_verified:
                    existing.email_verified = True
            else:
                u = _create_user(request, email=ext.email if ext.email and ext.email_verified else
                                 f"{ext.provider}-{ext.subject}{PLACEHOLDER_DOMAIN}", name=ext.name[:120], password=None,
                                 verified=bool(ext.email and ext.email_verified), profile_completed=False)
                uid = u.id
            s.add(Identity(user_id=uid, provider=ext.provider, subject=ext.subject, email=ext.email))
            _telegram_bind(s, uid, ext)
            s.commit()
    return await complete_login(request, uid, name)


def _telegram_bind(s, uid: int, ext: oauth.ExternalUser) -> None:
    """Telegram login = the user's chat with our bot: link it for notifications right away."""
    if ext.provider != "telegram":
        return
    for other in s.scalars(select(User).where(User.telegram_chat_id == ext.subject, User.id != uid)):
        other.telegram_chat_id = None
    u = s.get(User, uid)
    u.telegram_chat_id, u.telegram_username = ext.subject, (ext.username or "")[:64]


# ------------------------------------------------------------------ complete profile (after social sign-up)
@router.get("/complete-profile")
async def complete_profile_form(request: Request):
    from ..security import require_user
    u = require_user(request)
    return render(request, "complete_profile.html", **_ctx(request, form={
        "email": "" if u.email.endswith(PLACEHOLDER_DOMAIN) else u.email, "name": u.name, "phone": u.phone,
        "country": u.country, "experience": u.experience}, needs_email=u.email.endswith(PLACEHOLDER_DOMAIN)))


@router.post("/complete-profile")
async def complete_profile(request: Request):
    from ..security import require_user
    u = require_user(request)
    f = await form_with_csrf(request)
    form = {k: str(f.get(k, "")).strip() for k in ("email", "name", "phone", "country", "experience", "referral")}
    errors = []
    needs_email = u.email.endswith(PLACEHOLDER_DOMAIN)
    email = form["email"].lower()
    if needs_email:
        if not EMAIL_RE.match(email):
            errors.append(tr(request, "err_email"))
        else:
            with SessionLocal() as s:
                if s.scalar(select(func.count(User.id)).where(User.email == email, User.id != u.id)):
                    errors.append(tr(request, "err_email_taken"))
    if len(form["name"]) < 2:
        errors.append(tr(request, "err_name"))
    if not f.get("terms"):
        errors.append(tr(request, "err_terms"))
    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(request, "complete_profile.html", status_code=400, **_ctx(request, form=form, needs_email=needs_email))
    with SessionLocal() as s:
        x = s.get(User, u.id)
        if needs_email:
            x.email, x.email_verified = email, False
        x.name, x.phone = form["name"][:120], form["phone"][:32]
        x.country = form["country"] if form["country"] in COUNTRIES else ""
        x.experience = form["experience"] if form["experience"] in EXPERIENCE else ""
        x.referral_source, x.marketing_opt_in = form["referral"][:80], bool(f.get("marketing"))
        x.accepted_terms_at, x.profile_completed = utcnow(), True
        s.commit()
        x = s.get(User, u.id)
    await _send_verification(request, x)
    flash(request, tr(request, "msg_saved"))
    return RedirectResponse("/dashboard", 303)


# ------------------------------------------------------------------ e-mail verification
@router.get("/verify-email/{token}")
async def verify_email(token: str, request: Request):
    with SessionLocal() as s:
        u = tokens.check(token, "verify", 3 * 86400, lambda i: s.get(User, i))
        if u is None:
            flash(request, tr(request, "err_link_invalid"), "error")
        else:
            u.email_verified = True
            s.commit()
            flash(request, tr(request, "msg_email_verified"))
    return RedirectResponse("/dashboard" if request.session.get("uid") else "/login", 303)


@router.post("/verify-email/resend")
async def verify_resend(request: Request):
    from ..security import require_user
    u = require_user(request)
    await form_with_csrf(request)
    if _count(f"verify:{u.id}", 3600) >= 3:
        flash(request, tr(request, "err_too_many"), "error")
    else:
        _hit(f"verify:{u.id}")
        ok = await _send_verification(request, u)
        flash(request, tr(request, "msg_verify_sent" if ok else "err_mail_unavailable"), "ok" if ok else "error")
    return RedirectResponse(request.headers.get("referer") or "/dashboard", 303)


# ------------------------------------------------------------------ forgotten password
@router.get("/forgot")
async def forgot_form(request: Request):
    return render(request, "forgot.html", **_ctx(request, mail_ok=mailer.configured()))


@router.post("/forgot")
async def forgot(request: Request):
    f = await form_with_csrf(request)
    ip = client_ip(request)
    bad = await _bot_or_captcha_failed(request, f)
    if bad or _count(f"forgot:{ip}", 3600) >= 5:
        flash(request, tr(request, bad or "err_too_many"), "error")
        return render(request, "forgot.html", status_code=400, **_ctx(request, mail_ok=mailer.configured()))
    _hit(f"forgot:{ip}")
    email = str(f.get("email", "")).strip().lower()
    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.email == email)).first()
        base = get_global(s).base_url.rstrip("/")
    if u and u.status != "suspended":
        link = f"{base}/reset/{tokens.make(u, 'reset')}"
        await mailer.send(u.email, t(u.lang, "mail_reset_subject"), [t(u.lang, "mail_reset_body")],
                          (t(u.lang, "mail_reset_button"), link), u.anti_phishing, LANGS[u.lang]["dir"])
        if u.telegram_chat_id:  # also via Telegram: works even without a configured mail server
            from ..telegram import bot, esc
            await bot.send(u.telegram_chat_id, f"🔑 {esc(t(u.lang, 'mail_reset_body'))}\n{esc(link)}"
                           + (f"\n🔐 {esc(u.anti_phishing)}" if u.anti_phishing else ""), u.id, "security")
    flash(request, tr(request, "msg_reset_sent"))  # same answer whether the account exists or not
    return RedirectResponse("/login", 303)


@router.get("/reset/{token}")
async def reset_form(token: str, request: Request):
    with SessionLocal() as s:
        u = tokens.check(token, "reset", 3600, lambda i: s.get(User, i))
    if u is None:
        flash(request, tr(request, "err_link_invalid"), "error")
        return RedirectResponse("/forgot", 303)
    return render(request, "reset.html", **_ctx(request, token=token))


@router.post("/reset/{token}")
async def reset(token: str, request: Request):
    f = await form_with_csrf(request)
    with SessionLocal() as s:
        u = tokens.check(token, "reset", 3600, lambda i: s.get(User, i))
        if u is None:
            flash(request, tr(request, "err_link_invalid"), "error")
            return RedirectResponse("/forgot", 303)
        pw = str(f.get("password", ""))
        errors = [tr(request, "err_password_match")] if pw != str(f.get("password2", "")) else []
        if not errors:
            errors = await _password_errors(request, pw, u.email, u.name)
        if errors:
            for e in errors:
                flash(request, e, "error")
            return render(request, "reset.html", status_code=400, **_ctx(request, token=token))
        u.password_hash = hash_password(pw)
        u.session_version = (u.session_version or 1) + 1  # every other session is logged out
        s.commit()
        uid = u.id
        user = s.get(User, uid)
    await security_alert(user, "alert_password_changed", request)
    flash(request, tr(request, "msg_password_changed"))
    return await complete_login(request, uid, "password")
