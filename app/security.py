"""Passwords, API-key encryption, sessions, CSRF."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from .db import SessionLocal, User
from .settings import MASTER_KEY

try:
    _fernet = Fernet(MASTER_KEY.encode())
except (ValueError, TypeError) as e:  # pragma: no cover
    raise RuntimeError("MASTER_KEY is not a valid Fernet key") from e


# ------------------------------------------------------------------ passwords (scrypt, stdlib)
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


# ------------------------------------------------------------------ API key encryption
def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        raise RuntimeError("could not decrypt API key (MASTER_KEY changed?)")


def mask(value: str | None) -> str:
    v = decrypt(value) if value else ""
    return (v[:4] + "…" + v[-4:]) if v and len(v) > 10 else ("••••" if v else "")


# ------------------------------------------------------------------ session helpers
def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def form_with_csrf(request: Request) -> dict:
    form = await request.form()
    sent = str(form.get("csrf", ""))
    if not sent or not hmac.compare_digest(sent, request.session.get("csrf", "")):
        raise HTTPException(400, "CSRF token invalid - reload the page")
    return form


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def pop_flashes(request: Request) -> list:
    return request.session.pop("flash", [])


def current_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    with SessionLocal() as s:
        u = s.get(User, uid)
        # session_version: bumped on password reset / "log out other devices" -> old cookies stop working
        if u is None or u.status == "suspended" or request.session.get("sv", 1) != (u.session_version or 1):
            request.session.pop("uid", None)
            return None
        return u


class LoginRequired(Exception):
    pass


def require_user(request: Request) -> User:
    u = current_user(request)
    if u is None:
        raise LoginRequired()
    return u


def require_admin(request: Request) -> User:
    u = require_user(request)
    if not u.is_admin:
        raise HTTPException(403, "admin only")
    return u
