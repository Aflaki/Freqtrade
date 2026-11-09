"""Web Push (RFC 8030) with VAPID (RFC 8292) and aes128gcm payload encryption (RFC 8291).

Works with iPhone/iPad (iOS 16.4+, the site added to the Home Screen), Android and desktop browsers.
Only `cryptography` + PyJWT are used (pywebpush's http-ece does not build on newer Pythons).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from urllib.parse import urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
from sqlalchemy import select

from ..db import PushSubscription, SessionLocal, get_global, utcnow
from ..security import decrypt, encrypt

log = logging.getLogger("relay.push")
RECORD_SIZE = 4096


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ------------------------------------------------------------------ VAPID keys (created once, kept in the DB)
def vapid_keys() -> tuple[ec.EllipticCurvePrivateKey, str]:
    with SessionLocal() as s:
        gs = get_global(s)
        if not gs.vapid_private_enc:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
            gs.vapid_private_enc, gs.vapid_public = encrypt(pem), b64u(_point(key.public_key()))
            s.commit()
        pem, pub = decrypt(gs.vapid_private_enc), gs.vapid_public
    return serialization.load_pem_private_key(pem.encode(), None), pub


def public_key() -> str:
    return vapid_keys()[1]


def vapid_header(endpoint: str, contact: str) -> str:
    key, pub = vapid_keys()
    u = urlparse(endpoint)
    token = jwt.encode({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": contact},
                       key, algorithm="ES256")
    return f"vapid t={token}, k={pub}"


# ------------------------------------------------------------------ RFC 8291 payload encryption
def encrypt_payload(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
                    server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    ua_pub_bytes, auth_secret = unb64u(p256dh), unb64u(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = _point(as_key.public_key())
    salt = salt or os.urandom(16)
    shared = as_key.exchange(ec.ECDH(), ua_pub)
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes).derive(shared)
    prk = _extract(salt, ikm)
    cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\x00").derive(prk)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # single record, 0x02 = last-record delimiter
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_pub_bytes)) + as_pub_bytes + body


def _extract(salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869): the content key and nonce are then expanded from this PRK."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


# ------------------------------------------------------------------ sending
async def send(sub: PushSubscription, data: dict, contact: str, ttl: int = 24 * 3600, urgency: str = "high") -> int:
    body = encrypt_payload(json.dumps(data, ensure_ascii=False).encode(), sub.p256dh, sub.auth)
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
               "Urgency": urgency, "Authorization": vapid_header(sub.endpoint, contact)}
    if data.get("tag"):
        headers["Topic"] = "".join(c for c in str(data["tag"]) if c.isalnum())[:32] or "alert"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(sub.endpoint, content=body, headers=headers)
    return r.status_code


async def send_to_user(user_id: int, title: str, body: str, url: str = "/", tag: str = "") -> int:
    """Push to every device of the user. Returns how many accepted it; dead subscriptions are removed."""
    with SessionLocal() as s:
        subs = list(s.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)))
        gs = get_global(s)
        contact = "mailto:" + (gs.smtp_from or "admin@example.com")
        if gs.base_url.startswith("https://"):
            contact = gs.base_url
    ok = 0
    for sub in subs:
        try:
            code = await send(sub, {"title": title, "body": body, "url": url, "tag": tag}, contact)
            err = "" if code < 300 else f"HTTP {code}"
        except Exception as e:
            code, err = 0, f"{type(e).__name__}: {str(e)[:150]}"
        with SessionLocal() as s:
            x = s.get(PushSubscription, sub.id)
            if x is None:
                continue
            if code in (404, 410):  # unsubscribed / expired on the phone
                s.delete(x)
            elif code and code < 300:
                x.last_ok_at, x.last_error = utcnow(), ""
                ok += 1
            else:
                x.last_error = err[:200]
            s.commit()
        if err:
            log.warning("push to user %s failed: %s", user_id, err)
    return ok
