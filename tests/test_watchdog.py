"""Watchdog agent (restarts dead bots) + the site's report endpoint + phone alert channels."""
import ast
import json
import os
import signal
import subprocess
import sys
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.db import (Bot, PushSubscription, SessionLocal, User, WatchdogEvent, WatchdogHost, get_global, init_db,
                    utcnow)
from app.engine.watchdog_agent import build_agent, build_install
from app.main import app
from app.notify import alerts, webpush


def load_agent(tmp_path):
    """The generated agent script as a module (state file in a temp folder)."""
    ns = {"__name__": "agent"}
    exec(compile(build_agent("http://site.test", "tok"), "agent.py", "exec"), ns)
    ns["STATE_FILE"] = str(tmp_path / "state.json")
    return ns


# ------------------------------------------------------------------ agent: logic with fake discovery
def test_agent_script_is_valid_and_parses_configs(tmp_path):
    src = build_agent("https://relay.example", "secret-token")
    ast.parse(src)
    assert 'SITE = "https://relay.example"' in src and 'TOKEN = "secret-token"' in src and "__" + "SITE" not in src
    assert "KillMode=process" in build_install("https://relay.example", "t")  # bots survive a watchdog restart
    a = load_agent(tmp_path)
    (tmp_path / "config.json").write_text('{\n // comment\n "api_server": {"enabled": true, "listen_port": 8015,},\n}')
    (tmp_path / "private.json").write_text('{"api_server": {"listen_ip_address": "65.109.188.248"}}')
    argv = ["/venv/bin/python3", "/venv/bin/freqtrade", "trade", "-c", "config.json", "--config", "private.json"]
    assert a["api_address"](argv, str(tmp_path), {}) == ("65.109.188.248", 8015)
    assert a["api_address"](argv, str(tmp_path), {"FREQTRADE__API_SERVER__LISTEN_PORT": "9000"})[1] == 9000
    assert a["is_trade_cmd"](argv) and not a["is_trade_cmd"](["freqtrade", "download-data"])


def test_agent_restart_flow(tmp_path):
    a = load_agent(tmp_path)
    running = {"port:8015": {"kind": "process", "pid": 111, "port": 8015, "ip": "127.0.0.1", "argv": ["freqtrade", "trade"],
                             "cwd": "/root/ft", "env": {}, "uid": 0, "gid": 0, "unit": ""}}
    calls = []
    a["from_processes"] = lambda: dict(running)
    a["from_docker"] = lambda: ({}, set())
    a["oom_killed"] = lambda pid: pid == 111
    a["port_open"] = lambda ip, port: False

    def fake_restart(b):
        calls.append(b["key"])
        return len(calls) <= 1, "boom" if len(calls) > 1 else "journalctl -u relay-ft-8015"
    a["restart"] = fake_restart
    st = {"bots": {}, "pending": []}
    assert a["check"](st, {}) == [] and st["bots"]["port:8015"]["status"] == "running"
    running.clear()
    ev = a["check"](st, {})
    assert [e["action"] for e in ev] == ["gone"] and ev[0]["detail"] == "oom" and not calls  # waits one more check
    ev = a["check"](st, {})
    assert [e["action"] for e in ev] == ["restarted"] and ev[0]["cause"] == "oom" and calls == ["port:8015"]
    # it dies again and again: restart fails, then gives up after 3 attempts in 30 minutes
    actions = []
    for _ in range(8):
        actions += [e["action"] for e in a["check"](st, {})]
    assert actions.count("gave_up") == 1 and len(calls) == 3
    # an admin switched off auto-restart for this bot: nothing happens
    st2 = {"bots": {"port:9000": {"kind": "process", "port": 9000, "status": "running"}}, "pending": []}
    for _ in range(3):
        assert a["check"](st2, {"disabled_ports": [9000]}) == []
    assert st2["bots"]["port:9000"]["status"] == "paused"


def test_agent_restarts_a_real_killed_process(tmp_path):
    """A fake `freqtrade trade` process is killed; the agent finds it, remembers how it ran and starts it again."""
    a = load_agent(tmp_path)
    a["START_GRACE"] = 2
    a["oom_killed"] = lambda pid: False
    a["from_docker"] = lambda: ({}, set())
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "freqtrade"
    fake.write_text("#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(1)\n")
    fake.chmod(0o755)
    botdir = tmp_path / "bot1"
    botdir.mkdir()
    (botdir / "config.json").write_text('{"api_server": {"enabled": true, "listen_port": 18765}}')
    env = {**os.environ, "BOT_MARKER": "v1-8015"}
    p = subprocess.Popen([sys.executable, str(fake), "trade", "-c", "config.json"], cwd=botdir, env=env,
                         start_new_session=True)
    new_pid = None
    try:
        time.sleep(0.5)
        st = {"bots": {}, "pending": []}
        a["check"](st, {})
        b = st["bots"]["port:18765"]
        assert b["pid"] == p.pid and b["cwd"] == str(botdir) and b["env"]["BOT_MARKER"] == "v1-8015"
        p.kill()
        p.wait()
        assert [e["action"] for e in a["check"](st, {})] == ["gone"]
        ev = a["check"](st, {})
        assert [e["action"] for e in ev] == ["restarted"], ev
        found = a["from_processes"]()["port:18765"]
        new_pid = found["pid"]
        assert new_pid != p.pid and found["cwd"] == str(botdir) and found["env"]["BOT_MARKER"] == "v1-8015"
        assert found["argv"][1:] == [str(fake), "trade", "-c", "config.json"]
        assert [e["action"] for e in a["check"](st, {})] == []  # running again, nothing to report
    finally:
        for pid in (p.pid, new_pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


# ------------------------------------------------------------------ site: report endpoint + alerts
@pytest.fixture
def site(monkeypatch):
    init_db()
    from app.engine.botmonitor import monitor
    sent = []
    monkeypatch.setattr(monitor, "notify_admins",
                        lambda kind, make_text, critical=False, title=None, tag="": sent.append(
                            {"text": make_text("en"), "critical": critical, "title": title("en") if title else "", "tag": tag}))
    with SessionLocal() as s:
        gs = get_global(s)
        gs.watchdog_token, gs.base_url = "wd-token-123", "https://relay.example"
        on = Bot(name="V1_8015", webhook_secret="wd-1", api_url="http://65.109.188.248:8015")
        off = Bot(name="V1_8020", webhook_secret="wd-2", api_url="http://65.109.188.248:8020", auto_restart=False)
        other = Bot(name="V2_4520", webhook_secret="wd-3", api_url="http://51.91.55.39:8015")
        s.add_all([on, off, other])
        s.commit()
        ids = [on.id, off.id, other.id]
    yield sent, ids
    with SessionLocal() as s:
        s.query(WatchdogEvent).delete()
        s.query(WatchdogHost).delete()
        s.query(Bot).filter(Bot.id.in_(ids)).delete()
        get_global(s).watchdog_token = ""
        s.commit()


def test_report_maps_bots_and_alerts(site):
    sent, (on_id, off_id, other_id) = site
    c = TestClient(app)
    assert c.get("/watchdog/agent.py?t=wrong").status_code == 403
    assert 'TOKEN = "wd-token-123"' in c.get("/watchdog/agent.py?t=wd-token-123").text
    assert "/watchdog/agent.py?t=wd-token-123" in c.get("/watchdog/install.sh?t=wd-token-123").text
    body = {"host": "ft-v1", "ips": ["65.109.188.248", "10.0.0.5"], "version": "1.0",
            "mem": {"MemTotal": 16000, "MemAvailable": 900}, "load": [3.1, 2.0, 1.5],
            "bots": [{"key": "port:8015", "port": 8015, "status": "running"}],
            "events": [{"key": "port:8015", "port": 8015, "action": "restarted", "cause": "oom", "down_for": 130,
                        "detail": "journalctl -u relay-ft-8015"},
                       {"key": "port:8015", "port": 8015, "action": "gone", "detail": "oom"}]}
    assert c.post("/watchdog/report", json=body, headers={"X-Watchdog-Token": "nope"}).status_code == 403
    r = c.post("/watchdog/report", json=body, headers={"X-Watchdog-Token": "wd-token-123"})
    assert r.status_code == 200 and r.json()["disabled_ports"] == [8020]  # auto_restart off; other server's bot not listed
    assert len(sent) == 1 and sent[0]["critical"]  # "gone" is stored, but only the outcome is pushed
    assert "V1_8015" in sent[0]["title"] and "out of memory" in sent[0]["text"] and "2 min" in sent[0]["text"]
    with SessionLocal() as s:
        evs = s.query(WatchdogEvent).order_by(WatchdogEvent.id).all()
        assert [(e.action, e.bot_id) for e in evs] == [("restarted", on_id), ("gone", on_id)]
        h = s.query(WatchdogHost).one()
        assert h.hostname == "ft-v1" and h.report["mem"]["MemAvailable"] == 900


def test_silent_server_alert_once_and_back(site):
    sent, _ = site
    from app.web.watchdog import check_silent_hosts
    import datetime as dt
    with SessionLocal() as s:
        s.add(WatchdogHost(hostname="ft-v2", ips=["51.91.55.39"], last_seen=utcnow() - dt.timedelta(minutes=9)))
        s.commit()
    check_silent_hosts()
    check_silent_hosts()
    assert len(sent) == 1 and "ft-v2" in sent[0]["text"] and sent[0]["critical"]
    TestClient(app).post("/watchdog/report", json={"host": "ft-v2", "ips": ["51.91.55.39"], "bots": [], "events": []},
                         headers={"X-Watchdog-Token": "wd-token-123"})
    assert len(sent) == 2 and "reporting again" in sent[1]["text"]


def test_admin_bots_page_shows_watchdog(site):
    c = TestClient(app)
    from app.security import hash_password
    with SessionLocal() as s:
        u = User(email="wd-admin@x.com", name="WD", password_hash=hash_password("Xy-12345678-zz"), is_admin=True,
                 status="active")
        s.add(u)
        s.commit()
        uid = u.id
    try:
        page = c.get("/login").text
        import re
        data = {k: re.search(rf'name="{k}" value="([^"]+)"', page).group(1) for k in ("csrf", "ft")}
        from app.auth import captcha
        captcha.MIN_SECONDS = 0
        c.post("/login", data={**data, "email": "wd-admin@x.com", "password": "Xy-12345678-zz"})
        html = c.get("/admin/bot").text
        assert "/watchdog/install.sh?t=wd-token-123" in html and "sudo sh" in html
    finally:
        with SessionLocal() as s:
            s.query(User).filter_by(id=uid).delete()
            s.commit()


# ------------------------------------------------------------------ Web Push
def test_webpush_rfc8291_vector():
    as_key = ec.derive_private_key(int.from_bytes(webpush.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                   ec.SECP256R1())
    out = webpush.encrypt_payload(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg", salt=webpush.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), server_key=as_key)
    assert webpush.b64u(out) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
        "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_vapid_header():
    init_db()
    hdr = webpush.vapid_header("https://web.push.apple.com/QGuQyavXutnMH...", "mailto:a@b.c")
    token = hdr.split("t=", 1)[1].split(",", 1)[0]
    pub = webpush.public_key()
    assert hdr.endswith("k=" + pub)
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.unb64u(pub))
    claims = jwt.decode(token, key, algorithms=["ES256"], audience="https://web.push.apple.com")
    assert claims["sub"] == "mailto:a@b.c" and claims["exp"] > time.time()


def test_push_subscribe_send_and_cleanup(monkeypatch):
    init_db()
    from app.security import hash_password
    ua = ec.generate_private_key(ec.SECP256R1())
    sub = {"endpoint": "https://web.push.apple.com/abc", "keys": {
        "p256dh": webpush.b64u(webpush._point(ua.public_key())), "auth": webpush.b64u(os.urandom(16))}}
    with SessionLocal() as s:
        u = User(email="push@x.com", name="P", password_hash=hash_password("Xy-12345678-zz"), status="active")
        s.add(u)
        s.commit()
        uid = u.id
    c = TestClient(app)
    try:
        import re
        from app.auth import captcha
        captcha.MIN_SECONDS = 0
        page = c.get("/login").text
        data = {k: re.search(rf'name="{k}" value="([^"]+)"', page).group(1) for k in ("csrf", "ft")}
        c.post("/login", data={**data, "email": "push@x.com", "password": "Xy-12345678-zz"})
        assert c.get("/sw.js").headers["content-type"].startswith("application/javascript")
        assert c.post("/push/subscribe", json={"csrf": "bad", "subscription": sub}).status_code == 400
        csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/settings").text).group(1)
        assert c.post("/push/subscribe", json={"csrf": csrf, "subscription": sub, "device": "iPhone"}).status_code == 200
        posts = []
        codes = iter([201, 410])
        real = httpx.AsyncClient

        def handler(req):
            posts.append(req)
            return httpx.Response(next(codes))
        monkeypatch.setattr(webpush.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        import asyncio
        assert asyncio.run(webpush.send_to_user(uid, "⛔ Bot V1_8015 is down", "details", "/admin/bot", "bot-V1")) == 1
        req = posts[0]
        assert req.headers["content-encoding"] == "aes128gcm" and req.headers["urgency"] == "high"
        assert req.headers["authorization"].startswith("vapid t=")
        with SessionLocal() as s:
            assert s.query(PushSubscription).filter_by(user_id=uid).one().last_ok_at is not None
        assert asyncio.run(webpush.send_to_user(uid, "x", "y")) == 0  # 410 Gone: phone unsubscribed
        with SessionLocal() as s:
            assert s.query(PushSubscription).filter_by(user_id=uid).count() == 0
    finally:
        with SessionLocal() as s:
            s.query(User).filter_by(id=uid).delete()
            s.commit()


def test_alert_bot_message(monkeypatch):
    init_db()
    from app.security import encrypt
    with SessionLocal() as s:
        get_global(s).alert_bot_token_enc = encrypt("999:ALERT")
        s.commit()
    seen = []
    real = httpx.AsyncClient

    def handler(req):
        seen.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})
    monkeypatch.setattr(alerts.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    import asyncio
    try:
        ok, _ = asyncio.run(alerts.send_alert_bot("12345", "<b>[V1_8015]</b> restarted"))
        assert ok and seen[0][0].endswith("/bot999:ALERT/sendMessage") and seen[0][1]["chat_id"] == "12345"
        assert alerts.plain("<b>[V1]</b> a &amp; b") == "[V1] a & b"
    finally:
        with SessionLocal() as s:
            get_global(s).alert_bot_token_enc = None
            s.commit()


def test_still_down_reminder_every_two_hours(monkeypatch):
    import datetime as dt

    from app.engine.botmonitor import monitor
    sent = []
    monkeypatch.setattr(monitor, "notify_admins", lambda kind, mk, critical=False, title=None, tag="": sent.append(
        (mk("en"), critical)))
    three_h = (utcnow() - dt.timedelta(hours=3)).isoformat(timespec="seconds")
    old = {"api": "unreachable", "down_since": three_h}
    new = {"api": "unreachable", "error": "ConnectError", "checked_at": utcnow().isoformat()}
    monitor._alert_changes("V1_8015", old, new)
    assert len(sent) == 1 and sent[0][1] and "Still down" in sent[0][0] and new["down_since"] == three_h
    monitor._alert_changes("V1_8015", dict(new), {"api": "unreachable", "checked_at": utcnow().isoformat()})
    assert len(sent) == 1  # reminded just now: not again for 2 hours
    up = {"api": "ok", "state": "running"}
    monitor._alert_changes("V1_8015", dict(new), up)
    assert "down_since" not in up and len(sent) == 2 and sent[-1][1]  # "back up" alert, also to the phone
