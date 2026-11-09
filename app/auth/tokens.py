"""Signed, expiring links for e-mail verification and password reset (single use by design)."""
from __future__ import annotations

import hashlib

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..db import User
from ..settings import SECRET_KEY

_ser = URLSafeTimedSerializer(SECRET_KEY)


def _fingerprint(u: User) -> str:
    # changes when the password or the e-mail changes -> old reset/verify links stop working
    return hashlib.sha256(f"{u.password_hash}|{u.email}|{u.session_version}".encode()).hexdigest()[:16]


def make(u: User, purpose: str) -> str:
    return _ser.dumps({"u": u.id, "f": _fingerprint(u)}, salt=purpose)


def check(token: str, purpose: str, max_age: int, load_user) -> User | None:
    try:
        data = _ser.loads(token, salt=purpose, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    u = load_user(data.get("u"))
    if u is None or data.get("f") != _fingerprint(u):
        return None
    return u
