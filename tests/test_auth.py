"""Sign-up / login / 2FA / social sign-in / password reset / security settings."""
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import captcha, mailer, oauth, passwords, twofa
from app.db import Identity, SessionLocal, User, get_global, init_db
from app.main import app
from app.security import decrypt, encrypt
from app.web import auth as authweb

STRONG = "Gr33n-Lantern-Relay"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Fresh users / settings for every test, and nothing left behind for the other test modules."""
    init_db()
    sent = []

    async def fake_send(to, subject, lines, button=None, anti_phishing="", dir_="ltr"):
        sent.append({"to": to, "subject": subject, "lines": lines, "button": button, "ap": anti_phishing})
        return True

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(mailer, "configured", lambda: True)
    authweb._hits.clear()
    yield sent
    authweb._hits.clear()
    oauth._discovery.clear()
    oauth._jwks.clear()
    with SessionLocal() as s:
        s.query(Identity).delete()
        s.query(User).delete()
        gs = get_global(s)
        gs.auth_providers, gs.captcha_provider, gs.captcha_site_key, gs.captcha_secret_enc = None, "", "", None
        gs.require_2fa_admins = gs.require_email_verification = False
        gs.base_url = "http://testserver"
        s.commit()


def fields(client, url):
    page = client.get(url).text
    out = {"csrf": re.search(r'name="csrf" value="([^"]+)"', page).group(1)}
    m = re.search(r'name="ft" value="([^"]+)"', page)
    if m:
        out["ft"] = m.group(1)
    return out


def register(client, email="neo@x.com", name="Neo Anderson", password=STRONG, **extra):
    data = {**fields(client, "/register"), "email": email, "name": name, "password": password,
            "password2": password, "terms": "on", **extra}
    return client.post("/register", data=data, follow_redirects=False)


def login(client, email="neo@x.com", password=STRONG):
    return client.post("/login", data={**fields(client, "/login"), "email": email, "password": password},
                       follow_redirects=False)


def user(email="neo@x.com") -> User:
    with SessionLocal() as s:
        return s.query(User).filter_by(email=email).one()


# ------------------------------------------------------------------ password policy
def test_password_policy():
    assert "pw_short" in passwords.problems("Ab1!", min_length=10)
    assert "pw_variety" in passwords.problems("abcdefghijkl")
    assert passwords.problems("correct horse battery staple") == []  # long passphrase is fine
    assert "pw_common" in passwords.problems("Password123!".lower())
    assert "pw_personal" in passwords.problems("Johnsmith#2026", email="johnsmith@x.com")
    assert passwords.problems(STRONG, email="neo@x.com", name="Neo Anderson") == []


# ------------------------------------------------------------------ register + bot protection
def test_register_weak_password_bot_and_success(clean):
    with TestClient(app) as c:
        r = register(c, password="password1")
        assert r.status_code == 400 and user_count() == 0
        r = register(c, website="http://spam")  # honeypot filled
        assert r.status_code == 400 and user_count() == 0
        r = register(c, country="DE", experience="pro", referral="YouTube", marketing="on")
        assert r.status_code == 303
        u = user()
        assert u.is_admin and u.country == "DE" and u.experience == "pro" and u.marketing_opt_in
        assert u.email_verified is False and u.has_password
        verify = [m for m in clean if m["button"] and "/verify-email/" in m["button"][1]]
        assert verify, clean
        link = verify[0]["button"][1].split("testserver", 1)[-1]
        c.get(link, follow_redirects=False)
        assert user().email_verified is True
        assert register(c, email="neo@x.com").status_code == 400  # taken


def test_form_timer_blocks_instant_posts(monkeypatch):
    monkeypatch.setattr(captcha, "MIN_SECONDS", 3)
    with TestClient(app) as c:
        assert register(c).status_code == 400 and user_count() == 0


def user_count():
    with SessionLocal() as s:
        return s.query(User).count()


# ------------------------------------------------------------------ login, rate limit, captcha after failures
def test_login_failures_then_captcha(monkeypatch):
    with TestClient(app) as c:
        register(c)
        c.post("/logout", data=fields(c, "/dashboard"))
        with SessionLocal() as s:
            gs = get_global(s)
            gs.captcha_provider, gs.captcha_site_key, gs.captcha_secret_enc = "turnstile", "site-key", encrypt("sec")
            s.commit()
        for _ in range(3):
            assert login(c, password="wrong-Password-1").status_code == 401
        assert "cf-turnstile" in c.get("/login").text  # captcha now shown

        async def bad(form, ip=""):
            return False
        monkeypatch.setattr(captcha, "verify", bad)
        assert login(c).status_code == 400  # right password, but captcha not solved

        async def good(form, ip=""):
            return True
        monkeypatch.setattr(captcha, "verify", good)
        r = login(c)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard"


# ------------------------------------------------------------------ 2FA set-up, login, recovery codes
def test_twofa_flow():
    with TestClient(app) as c:
        register(c)
        c.post("/settings/security/2fa/start", data=fields(c, "/settings/security"))
        page = c.get("/settings/security").text
        assert "<svg" in page and "2fa/confirm" in page
        secret = decrypt(user().totp_secret_enc)
        r = c.post("/settings/security/2fa/confirm", data={**fields(c, "/settings/security"), "code": "000000"},
                   follow_redirects=False)
        assert not user().totp_enabled
        r = c.post("/settings/security/2fa/confirm",
                   data={**fields(c, "/settings/security"), "code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200 and user().totp_enabled
        pre = re.search(r'<pre class="codes[^>]*>(.*?)</pre>', r.text, re.S).group(1)
        codes = re.findall(r"\b[0-9A-F]{4}-[0-9A-F]{4}\b", pre)
        assert len(codes) == 8 and len(user().recovery_codes) == 8
        assert codes[0] not in str(user().recovery_codes)  # only hashes stored

        c.post("/logout", data=fields(c, "/dashboard"))
        r = login(c)
        assert r.headers["location"] == "/login/2fa"
        assert c.get("/dashboard", follow_redirects=False).status_code == 303  # not logged in yet
        bad = c.post("/login/2fa", data={**fields(c, "/login/2fa"), "code": "123456"}, follow_redirects=False)
        assert bad.status_code == 401
        ok = c.post("/login/2fa", data={**fields(c, "/login/2fa"), "code": pyotp.TOTP(secret).now()},
                    follow_redirects=False)
        assert ok.headers["location"] == "/dashboard" and c.get("/dashboard").status_code == 200

        c.post("/logout", data=fields(c, "/dashboard"))
        login(c)
        r = c.post("/login/2fa", data={**fields(c, "/login/2fa"), "code": codes[0].lower()}, follow_redirects=False)
        assert r.headers["location"] == "/dashboard" and len(user().recovery_codes) == 7
        c.post("/logout", data=fields(c, "/dashboard"))
        login(c)
        r = c.post("/login/2fa", data={**fields(c, "/login/2fa"), "code": codes[0]}, follow_redirects=False)
        assert r.status_code == 401  # a recovery code works once


def test_admin_must_enable_2fa():
    with TestClient(app) as c:
        register(c)  # first user = admin
        with SessionLocal() as s:
            get_global(s).require_2fa_admins = True
            s.commit()
        c.post("/logout", data=fields(c, "/dashboard"))
        assert login(c).headers["location"] == "/settings/security"


# ------------------------------------------------------------------ sessions
def test_logout_other_devices_and_password_change():
    a, b = TestClient(app), TestClient(app)
    with a, b:
        register(a)
        login(b)
        assert b.get("/dashboard", follow_redirects=False).status_code == 200
        a.post("/settings/security/logout-others", data=fields(a, "/settings/security"))
        assert b.get("/dashboard", follow_redirects=False).status_code == 303  # kicked out
        assert a.get("/dashboard", follow_redirects=False).status_code == 200  # still in
        new = "Blue-Harbor-Signal-7"
        a.post("/settings/security/password", data={**fields(a, "/settings/security"), "old": "nope", "new": new, "new2": new})
        assert login(b, password=new).status_code == 401
        a.post("/settings/security/password", data={**fields(a, "/settings/security"), "old": STRONG, "new": new, "new2": new})
        assert a.get("/dashboard", follow_redirects=False).status_code == 200
        assert login(b, password=new).status_code == 303


def test_antiphishing_code_in_mails(clean):
    with TestClient(app) as c:
        register(c)
        c.post("/settings/security/antiphishing", data={**fields(c, "/settings/security"), "code": "blue-cat-42"})
        assert user().anti_phishing == "blue-cat-42"
        clean.clear()
        c.post("/verify-email/resend", data=fields(c, "/settings/security"))
        assert clean and clean[-1]["ap"] == "blue-cat-42"


# ------------------------------------------------------------------ forgot / reset
def test_forgot_and_reset(clean):
    with TestClient(app) as c:
        register(c)
        c.post("/logout", data=fields(c, "/dashboard"))
        clean.clear()
        c.post("/forgot", data={**fields(c, "/forgot"), "email": "nobody@x.com"})
        assert not clean  # unknown e-mail: same answer, no mail
        c.post("/forgot", data={**fields(c, "/forgot"), "email": "neo@x.com"})
        link = clean[-1]["button"][1].split("testserver", 1)[-1]
        assert c.get(link).status_code == 200
        new = "Quiet-Mountain-River-3"
        r = c.post(link, data={**fields(c, link), "password": "short", "password2": "short"}, follow_redirects=False)
        assert r.status_code == 400
        r = c.post(link, data={**fields(c, link), "password": new, "password2": new}, follow_redirects=False)
        assert r.status_code == 303
        assert c.get(link, follow_redirects=False).headers["location"] == "/forgot"  # single use
        c.post("/logout", data=fields(c, "/dashboard"))
        assert login(c, password=new).status_code == 303


# ------------------------------------------------------------------ OpenID Connect (Google) with a fake provider
@pytest.fixture
def fake_google(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with SessionLocal() as s:
        gs = get_global(s)
        gs.auth_providers = {"google": {"enabled": True, "client_id": "cid", "secret_enc": encrypt("csecret")}}
        s.commit()
    oauth._discovery["google"] = (time.time(), {
        "authorization_endpoint": "https://accounts.example/auth", "token_endpoint": "https://accounts.example/token",
        "jwks_uri": "https://accounts.example/jwks", "issuer": "https://accounts.example"})

    pub = key.public_key()

    class Key:
        pass
    Key.key = pub

    class FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return Key()

    oauth._jwks["https://accounts.example/jwks"] = FakeJWKS()
    state = {"claims": {}, "posted": None}

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, data=None, **k):
            state["posted"] = data
            claims = {"iss": "https://accounts.example", "aud": "cid", "iat": int(time.time()),
                      "exp": int(time.time()) + 600, **state["claims"]}
            return FakeResp({"id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})})

        async def get(self, url, **k):
            raise AssertionError("discovery is cached")

    monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeClient)
    return state


def google_login(client, state, claims, mode=""):
    r = client.get("/auth/google/start" + mode, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["cid"]
    state["claims"] = {"nonce": q["nonce"][0], **claims}
    return client.get(f"/auth/google/callback?state={q['state'][0]}&code=abc", follow_redirects=False)


def test_google_signup_complete_profile_and_link(fake_google):
    with TestClient(app) as c:
        assert "/auth/google/start" in c.get("/register").text
        r = google_login(c, fake_google, {"sub": "g-1", "email": "Trin@Gmail.com", "email_verified": True, "name": "Trinity"})
        assert r.headers["location"] == "/complete-profile"
        assert fake_google["posted"]["code_verifier"] and fake_google["posted"]["client_secret"] == "csecret"
        u = user("trin@gmail.com")
        assert u.email_verified and not u.has_password and u.profile_completed is False
        r = c.post("/complete-profile", data={**fields(c, "/complete-profile"), "name": "Trinity", "country": "AT",
                                              "terms": "on"}, follow_redirects=False)
        assert r.headers["location"] == "/dashboard" and user("trin@gmail.com").profile_completed
        # the only sign-in method can't be removed
        with SessionLocal() as s:
            ident = s.query(Identity).one()
        c.post(f"/settings/security/unlink/{ident.id}", data=fields(c, "/settings/security"))
        with SessionLocal() as s:
            assert s.query(Identity).count() == 1

        c.post("/logout", data=fields(c, "/dashboard"))
        r = google_login(c, fake_google, {"sub": "g-1", "email": "trin@gmail.com", "email_verified": True})
        assert r.headers["location"] == "/dashboard"
        assert user_count() == 1


def test_google_bad_nonce_and_state(fake_google):
    with TestClient(app) as c:
        r = c.get("/auth/google/start", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        fake_google["claims"] = {"nonce": "evil", "sub": "x", "email": "x@x.com", "email_verified": True}
        r = c.get(f"/auth/google/callback?state={q['state'][0]}&code=abc", follow_redirects=False)
        assert r.headers["location"] == "/login" and user_count() == 0
        r = c.get("/auth/google/callback?state=forged&code=abc", follow_redirects=False)
        assert r.headers["location"] == "/login" and user_count() == 0


def test_google_links_to_existing_verified_email(fake_google):
    with TestClient(app) as c:
        register(c)
        with SessionLocal() as s:
            s.query(User).update({"email_verified": True})
            s.commit()
        c.post("/logout", data=fields(c, "/dashboard"))
        r = google_login(c, fake_google, {"sub": "g-2", "email": "neo@x.com", "email_verified": True})
        assert r.headers["location"] == "/dashboard" and user_count() == 1
        # unverified e-mail from a provider must NOT take over an account
        c.post("/logout", data=fields(c, "/dashboard"))
        google_login(c, fake_google, {"sub": "g-3", "email": "neo@x.com", "email_verified": False})
        assert user_count() == 2


def test_apple_form_post_is_relayed():
    with TestClient(app) as c:
        r = c.post("/auth/apple/callback", data={"state": "s", "code": "c"})
        assert r.status_code == 200 and 'name="_relay"' in r.text and 'name="code" value="c"' in r.text


# ------------------------------------------------------------------ Telegram Login Widget
def test_telegram_widget_signature():
    token = "123:ABC"
    data = {"id": "42", "first_name": "Ann", "username": "ann", "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    good = hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    ext = oauth.verify_telegram({**data, "hash": good}, token)
    assert ext.subject == "42" and ext.username == "ann" and ext.name == "Ann"
    with pytest.raises(oauth.OAuthError):
        oauth.verify_telegram({**data, "id": "43", "hash": good}, token)
    old = {**data, "auth_date": "1000"}
    check = "\n".join(f"{k}={old[k]}" for k in sorted(old))
    sig = hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(oauth.OAuthError):
        oauth.verify_telegram({**old, "hash": sig}, token)


# ------------------------------------------------------------------ admin page
def test_admin_security_settings():
    with TestClient(app) as c:
        register(c)  # admin
        page = c.get("/admin/security").text
        assert "/auth/google/callback" in page
        c.post("/admin/security", data={**fields(c, "/admin/security"), "section": "providers",
                                        "google__enabled": "on", "google__client_id": "abc", "google__secret": "s3"})
        c.post("/admin/security", data={**fields(c, "/admin/security"), "section": "policy",
                                        "password_min_length": "12", "login_alerts": "on"})
        assert oauth.config("google")["secret"] == "s3"
        assert "google" in oauth.enabled_providers()
        with SessionLocal() as s:
            gs = get_global(s)
            assert gs.password_min_length == 12 and not gs.check_breached_passwords
        r = register(TestClient(app), email="b@x.com", name="Bob", password="Abcdef-123")  # 10 < 12
        assert r.status_code == 400


def test_recovery_code_helpers():
    codes, hashes = twofa.new_recovery_codes()
    assert len(set(codes)) == 8
    rest = twofa.use_recovery_code(hashes, codes[3].lower().replace("-", ""))
    assert rest is not None and len(rest) == 7
    assert twofa.use_recovery_code(rest, codes[3]) is None
