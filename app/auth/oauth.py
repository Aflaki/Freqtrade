"""Sign in with Google / Apple / Microsoft / Yahoo (OpenID Connect), GitHub (OAuth 2) and Telegram (Login Widget).

OIDC: authorization code flow + PKCE (S256) + state + nonce; the ID token signature is verified with the
provider's published keys (JWKS), as are audience, issuer and nonce. Credentials are set in the admin panel.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from ..db import SessionLocal, get_global
from ..security import decrypt

PROVIDERS: dict[str, dict[str, Any]] = {
    "google": {"name": "Google", "discovery": "https://accounts.google.com/.well-known/openid-configuration",
               "scope": "openid email profile", "console": "https://console.cloud.google.com/apis/credentials"},
    "apple": {"name": "Apple", "authorize": "https://appleid.apple.com/auth/authorize",
              "token": "https://appleid.apple.com/auth/token", "jwks": "https://appleid.apple.com/auth/keys",
              "issuer": "https://appleid.apple.com", "scope": "name email", "form_post": True,
              "console": "https://developer.apple.com/account/resources/identifiers/list/serviceId"},
    "microsoft": {"name": "Microsoft", "discovery": "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration",
                  "scope": "openid email profile", "skip_issuer": True,
                  "console": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"},
    "yahoo": {"name": "Yahoo", "discovery": "https://api.login.yahoo.com/.well-known/openid-configuration",
              "scope": "openid email profile", "console": "https://developer.yahoo.com/apps/"},
    "github": {"name": "GitHub", "authorize": "https://github.com/login/oauth/authorize",
               "token": "https://github.com/login/oauth/access_token", "scope": "read:user user:email", "oidc": False,
               "console": "https://github.com/settings/developers"},
    "telegram": {"name": "Telegram", "widget": True, "console": "https://t.me/BotFather"},
}
_discovery: dict[str, tuple[float, dict]] = {}
_jwks: dict[str, jwt.PyJWKClient] = {}


@dataclass
class ExternalUser:
    provider: str
    subject: str
    email: str = ""
    email_verified: bool = False
    name: str = ""
    username: str = ""


class OAuthError(Exception):
    pass


# ------------------------------------------------------------------ configuration
def config(provider: str) -> dict:
    with SessionLocal() as s:
        cfg = dict(((get_global(s).auth_providers or {}).get(provider) or {}))
    for k in ("secret_enc", "private_key_enc"):
        if cfg.get(k):
            cfg[k.replace("_enc", "")] = decrypt(cfg[k]) or ""
    return cfg


def enabled_providers() -> list[str]:
    from ..telegram import bot
    with SessionLocal() as s:
        allp = get_global(s).auth_providers or {}
    out = []
    for p, meta in PROVIDERS.items():
        c = allp.get(p) or {}
        if not c.get("enabled"):
            continue
        if meta.get("widget"):
            if bot.username:
                out.append(p)
        elif c.get("client_id") and (c.get("secret_enc") or (p == "apple" and c.get("private_key_enc"))):
            out.append(p)
    return out


async def _meta(provider: str) -> dict:
    p = PROVIDERS[provider]
    if "discovery" not in p:
        return {"authorization_endpoint": p["authorize"], "token_endpoint": p["token"],
                "jwks_uri": p.get("jwks"), "issuer": p.get("issuer")}
    cached = _discovery.get(provider)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(p["discovery"])
        r.raise_for_status()
        data = r.json()
    _discovery[provider] = (time.time(), data)
    return data


def _apple_client_secret(cfg: dict) -> str:
    """Apple wants a short-lived JWT signed with the team's .p8 key instead of a static secret."""
    now = int(time.time())
    return jwt.encode({"iss": cfg.get("team_id", ""), "iat": now, "exp": now + 3600, "aud": "https://appleid.apple.com",
                       "sub": cfg.get("client_id", "")}, cfg.get("private_key", ""), algorithm="ES256",
                      headers={"kid": cfg.get("key_id", "")})


# ------------------------------------------------------------------ flow
async def start(provider: str, redirect_uri: str, session: dict, mode: str = "login") -> str:
    if provider not in PROVIDERS or PROVIDERS[provider].get("widget"):
        raise OAuthError("unknown provider")
    cfg = config(provider)
    meta = await _meta(provider)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session["oauth"] = {"p": provider, "state": state, "nonce": nonce, "v": verifier, "mode": mode, "t": int(time.time())}
    params = {"client_id": cfg.get("client_id", ""), "redirect_uri": redirect_uri, "response_type": "code",
              "scope": PROVIDERS[provider]["scope"], "state": state}
    if PROVIDERS[provider].get("oidc", True):
        params.update(nonce=nonce, code_challenge=challenge, code_challenge_method="S256")
    if PROVIDERS[provider].get("form_post"):
        params["response_mode"] = "form_post"
    if provider == "google":
        params["prompt"] = "select_account"
    return meta["authorization_endpoint"] + "?" + urlencode(params)


async def finish(provider: str, params: dict, redirect_uri: str, session: dict) -> tuple[ExternalUser, str]:
    st = session.pop("oauth", None) or {}
    if st.get("p") != provider or not params.get("state") or not hmac.compare_digest(str(params.get("state")), st.get("state", "")):
        raise OAuthError("state mismatch - start the sign-in again")
    if time.time() - st.get("t", 0) > 900:
        raise OAuthError("sign-in took too long - start again")
    if params.get("error"):
        raise OAuthError(str(params.get("error_description") or params.get("error")))
    cfg = config(provider)
    meta = await _meta(provider)
    secret = _apple_client_secret(cfg) if provider == "apple" else cfg.get("secret", "")
    data = {"grant_type": "authorization_code", "code": params.get("code", ""), "redirect_uri": redirect_uri,
            "client_id": cfg.get("client_id", ""), "client_secret": secret}
    if PROVIDERS[provider].get("oidc", True):
        data["code_verifier"] = st.get("v", "")
    async with httpx.AsyncClient(timeout=15, headers={"Accept": "application/json"}) as c:
        r = await c.post(meta["token_endpoint"], data=data)
        tok = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or "error" in tok:
            raise OAuthError(f"token exchange failed: {tok.get('error_description') or tok.get('error') or r.status_code}")
        if provider == "github":
            return await _github(c, tok.get("access_token", "")), st.get("mode", "login")
    claims = _verify_id_token(provider, tok.get("id_token", ""), cfg.get("client_id", ""), meta, st.get("nonce", ""))
    name = claims.get("name") or " ".join(x for x in (claims.get("given_name"), claims.get("family_name")) if x)
    if provider == "apple" and params.get("user"):  # Apple sends the name only once, as JSON in the form post
        try:
            import json
            nm = json.loads(params["user"]).get("name") or {}
            name = " ".join(x for x in (nm.get("firstName"), nm.get("lastName")) if x) or name
        except ValueError:
            pass
    verified = claims.get("email_verified") in (True, "true", "True") or (provider == "microsoft" and bool(claims.get("email")))
    return ExternalUser(provider, str(claims["sub"]), str(claims.get("email") or "").lower(), verified, name or ""), st.get("mode", "login")


def _verify_id_token(provider: str, token: str, client_id: str, meta: dict, nonce: str) -> dict:
    if not token:
        raise OAuthError("no id_token")
    uri = meta.get("jwks_uri")
    client = _jwks.get(uri) or jwt.PyJWKClient(uri, cache_keys=True)
    _jwks[uri] = client
    key = client.get_signing_key_from_jwt(token)
    opts = {"verify_iss": not PROVIDERS[provider].get("skip_issuer")}
    claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=client_id,
                        issuer=meta.get("issuer") if opts["verify_iss"] else None, options=opts, leeway=60)
    if claims.get("nonce") and not hmac.compare_digest(str(claims["nonce"]), nonce):
        raise OAuthError("nonce mismatch")
    return claims


async def _github(c: httpx.AsyncClient, access_token: str) -> ExternalUser:
    h = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
    u = (await c.get("https://api.github.com/user", headers=h)).json()
    emails = (await c.get("https://api.github.com/user/emails", headers=h)).json()
    primary = next((e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")), None)
    return ExternalUser("github", str(u["id"]), (primary or {}).get("email", "").lower(), bool(primary),
                        u.get("name") or u.get("login") or "", u.get("login") or "")


# ------------------------------------------------------------------ Telegram Login Widget
def verify_telegram(params: dict, bot_token: str, max_age: int = 86400) -> ExternalUser:
    """https://core.telegram.org/widgets/login#checking-authorization"""
    data = {k: str(v) for k, v in params.items() if k != "hash"}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not bot_token or not hmac.compare_digest(expected, str(params.get("hash", ""))):
        raise OAuthError("Telegram signature invalid")
    if time.time() - int(data.get("auth_date", "0") or 0) > max_age:
        raise OAuthError("Telegram login expired - try again")
    name = " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x)
    return ExternalUser("telegram", data["id"], "", False, name, data.get("username", ""))
