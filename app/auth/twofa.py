"""Two-factor authentication with an authenticator app (TOTP, RFC 6238) + one-time recovery codes."""
from __future__ import annotations

import hashlib
import io
import secrets

import pyotp
import qrcode
import qrcode.image.svg

from ..security import decrypt


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def qr_svg(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def check_code(secret_enc: str | None, code: str) -> bool:
    secret = decrypt(secret_enc) if secret_enc else None
    code = "".join(ch for ch in str(code) if ch.isdigit())
    return bool(secret and len(code) == 6 and pyotp.TOTP(secret).verify(code, valid_window=1))


def _h(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").strip().upper().encode()).hexdigest()


def new_recovery_codes(n: int = 8) -> tuple[list[str], list[str]]:
    """(codes to show once, hashes to store)."""
    codes = [f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}" for _ in range(n)]
    return codes, [_h(c) for c in codes]


def use_recovery_code(stored: list[str] | None, code: str) -> list[str] | None:
    """Remaining hashes if the code was valid (it is consumed), else None."""
    h = _h(code)
    if stored and h in stored:
        return [x for x in stored if x != h]
    return None
