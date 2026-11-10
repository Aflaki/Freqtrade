"""User guides: pages in 4 languages, server IP, video embeds, readable exchange errors."""
import re

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal, get_global, init_db
from app.i18n import LANGS, T
from app.main import app
from app.web import guides


@pytest.fixture(autouse=True)
def base(monkeypatch):
    init_db()
    with SessionLocal() as s:
        gs = get_global(s)
        old = gs.base_url, gs.public_ip, gs.guide_videos
        gs.base_url, gs.public_ip, gs.guide_videos = "https://65-109-188-248.sslip.io", "", None
        s.commit()
    yield
    with SessionLocal() as s:
        gs = get_global(s)
        gs.base_url, gs.public_ip, gs.guide_videos = old
        s.commit()


def test_every_guide_text_exists_in_all_languages():
    keys = set()
    for g in guides.GUIDES.values():
        keys |= {g["title"], g["intro"]}
        for st in g["steps"] + g.get("admin_steps", []):
            keys |= {st["t"], st["b"]} | ({st["warn"]} if st.get("warn") else set())
        keys |= {k for _, k in g.get("errors", [])} | {f"g_note_{x}" for x in g.get("notes", [])}
    missing = [k for k in keys if k not in T]
    assert not missing, missing
    empty = [k for k in keys for i in range(4) if not T[k][i].strip()]
    assert not empty, empty


@pytest.mark.parametrize("lang", list(LANGS))
def test_guide_pages_render(lang):
    c = TestClient(app)
    assert c.get(f"/guides?lang={lang}").status_code == 200
    for key in guides.ORDER:
        html = c.get(f"/guides/{key}?lang={lang}").text
        assert "g_" + "tg_" not in html and "{ip}" not in html and "{bot}" not in html, key  # no raw keys/placeholders
    from app.i18n import t
    tg = c.get(f"/guides/telegram?lang={lang}").text
    from markupsafe import escape
    assert str(escape(t(lang, "nav_settings"))) in tg and str(escape(t(lang, "btn_link_tg"))) in tg  # our menus translated
    assert "_nav_settings" not in tg
    bn = c.get(f"/guides/binance?lang={lang}").text
    assert "65.109.188.248" in bn and "Enable Futures" in bn and "Enable Withdrawals" in bn
    assert c.get("/guides/nope").status_code == 404


def test_server_ip_and_video_embed():
    assert guides.server_ip() == "65.109.188.248"  # from the sslip.io address, no DNS needed
    with SessionLocal() as s:
        get_global(s).public_ip = "1.2.3.4"
        s.commit()
    assert guides.server_ip() == "1.2.3.4"
    assert guides.video_embed("https://youtu.be/dQw4w9WgXcQ")["src"] == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert guides.video_embed("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3")["kind"] == "iframe"
    assert "videohash/abc12" in guides.video_embed("https://www.aparat.com/v/abc12")["src"]
    assert guides.video_embed("https://cdn.example.com/a.mp4")["kind"] == "video"
    assert guides.video_embed("javascript:alert(1)") is None


def test_explain_binance_errors():
    assert guides.explain('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}') == "g_err_2015"
    assert guides.explain('{"code":-1022,"msg":"Signature for this request is not valid."}') == "g_err_1022"
    assert guides.explain('{"code":-2014,"msg":"API-key format invalid."}') == "g_err_2014"
    assert guides.explain("okx Passphrase error") == "g_err_passphrase"
    assert guides.explain("NetworkError: timeout") is None


def test_failed_connection_test_explains_and_links(monkeypatch):
    from app.auth import captcha
    from app.db import ExchangeAccount, User
    from app.security import hash_password
    from app.web import user as uweb
    captcha.MIN_SECONDS = 0
    with SessionLocal() as s:
        u = User(email="guide@x.com", name="G", password_hash=hash_password("Xy-12345678-zz"), status="active", lang="en")
        s.add(u)
        s.commit()
        acc = ExchangeAccount(user_id=u.id, exchange="binance", label="b", capital=100)
        s.add(acc)
        s.commit()
        uid, aid = u.id, acc.id

    async def fake_test(acc, cur):
        return False, 'AuthenticationError: binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'
    monkeypatch.setattr(uweb, "test_connection", fake_test)
    c = TestClient(app)
    try:
        page = c.get("/login").text
        data = {k: re.search(rf'name="{k}" value="([^"]+)"', page).group(1) for k in ("csrf", "ft")}
        c.post("/login", data={**data, "email": "guide@x.com", "password": "Xy-12345678-zz"})
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/accounts").text).group(1)
        html = c.post(f"/accounts/{aid}/test", data={"csrf": csrf}).text
        assert "65.109.188.248" in html and "Enable Futures" in html and 'href="/guides/binance"' in html
        settings = c.get("/settings").text
        assert "No need to create a bot" in settings or "tg_not_configured" not in settings
    finally:
        with SessionLocal() as s:
            s.query(ExchangeAccount).filter_by(id=aid).delete()
            s.query(User).filter_by(id=uid).delete()
            s.commit()
