"""Bot protection for sign-up / login / password reset.

Always on (invisible): a honeypot field and a signed form timestamp (forms sent < 3 s after loading are bots).
Optional: a real CAPTCHA chosen in the admin panel — Cloudflare Turnstile, hCaptcha or Google reCAPTCHA v2.
"""
from __future__ import annotations

import time

import httpx
from itsdangerous import BadSignature, URLSafeSerializer

from ..db import SessionLocal, get_global
from ..security import decrypt
from ..settings import SECRET_KEY

PROVIDERS = {
    "turnstile": {"script": "https://challenges.cloudflare.com/turnstile/v0/api.js", "cls": "cf-turnstile",
                  "field": "cf-turnstile-response", "verify": "https://challenges.cloudflare.com/turnstile/v0/siteverify"},
    "hcaptcha": {"script": "https://js.hcaptcha.com/1/api.js", "cls": "h-captcha",
                 "field": "h-captcha-response", "verify": "https://api.hcaptcha.com/siteverify"},
    "recaptcha": {"script": "https://www.google.com/recaptcha/api.js", "cls": "g-recaptcha",
                  "field": "g-recaptcha-response", "verify": "https://www.google.com/recaptcha/api/siteverify"},
}
_ser = URLSafeSerializer(SECRET_KEY, salt="form-time")
HONEYPOT = "website"
MIN_SECONDS = 3


def widget() -> dict | None:
    """What the template needs to render the CAPTCHA (None = not configured)."""
    with SessionLocal() as s:
        gs = get_global(s)
        prov, key = gs.captcha_provider, gs.captcha_site_key
    if prov in PROVIDERS and key:
        return {"provider": prov, "site_key": key, **PROVIDERS[prov]}
    return None


def form_token() -> str:
    return _ser.dumps(int(time.time()))


def looks_like_bot(form) -> bool:
    if str(form.get(HONEYPOT, "")).strip():
        return True
    try:
        started = int(_ser.loads(str(form.get("ft", ""))))
    except (BadSignature, ValueError, TypeError):
        return True
    return time.time() - started < MIN_SECONDS


async def verify(form, ip: str = "") -> bool:
    """True if the configured CAPTCHA was solved (or no CAPTCHA is configured)."""
    with SessionLocal() as s:
        gs = get_global(s)
        prov, secret = gs.captcha_provider, decrypt(gs.captcha_secret_enc) or ""
    if prov not in PROVIDERS or not secret:
        return True
    token = str(form.get(PROVIDERS[prov]["field"], ""))
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(PROVIDERS[prov]["verify"], data={"secret": secret, "response": token, "remoteip": ip})
            return bool(r.json().get("success"))
    except Exception:
        return False
