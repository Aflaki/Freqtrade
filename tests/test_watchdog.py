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


def test_agent_reports_duplicates_not_children(tmp_path):
    """The same config started twice is a duplicate (reported, with who started each); a bot's own forked child
    is not; and while one copy still runs, the other is never started again."""
    a = load_agent(tmp_path)
    a["oom_killed"] = lambda pid: False
    a["from_docker"] = lambda: ({}, set())
    a["port_open"] = lambda ip, port: False
    restarts = []
    a["restart"] = lambda b: restarts.append(b["key"]) or (True, "")
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "freqtrade"
    fake.write_text("#!/usr/bin/env python3\nimport os, time\nif os.environ.get('FORK') and os.fork() == 0:\n"
                    "    time.sleep(60)\nwhile True:\n    time.sleep(1)\n")
    fake.chmod(0o755)
    botdir = tmp_path / "bot2"
    botdir.mkdir()
    (botdir / "config.json").write_text('{"api_server": {"enabled": true, "listen_port": 18777}}')
    cmd = [sys.executable, str(fake), "trade", "-c", "config.json"]
    first = subprocess.Popen(cmd, cwd=botdir, env={**os.environ, "FORK": "1"}, start_new_session=True)
    procs = [first]
    try:
        time.sleep(1.2)
        found = a["from_processes"]()["port:18777"]
        assert found["pid"] == first.pid and found["dups"] == []  # the forked child is the same bot
        time.sleep(1.1)  # a later start time
        second = subprocess.Popen(cmd, cwd=botdir, start_new_session=True)
        procs.append(second)
        time.sleep(0.8)
        st = {"bots": {}, "pending": []}
        ev = a["check"](st, {})
        assert [e["action"] for e in ev] == ["duplicate"] and "pid %d" % second.pid in ev[0]["detail"]
        found = a["from_processes"]()["port:18777"]
        assert found["pid"] == first.pid and [d["pid"] for d in found["dups"]] == [second.pid]
        assert a["check"](st, {}) == []  # reported once, not every minute
        # the first copy dies while the second still runs: no restart (that would make it two again)
        os.killpg(first.pid, signal.SIGKILL)  # with its child
        first.wait()
        time.sleep(0.3)
        for _ in range(3):
            a["check"](st, {})
        assert restarts == [] and st["bots"]["port:18777"]["pid"] == second.pid
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
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
    from app.db import ServerSample
    with SessionLocal() as s:
        s.query(WatchdogEvent).delete()
        s.query(WatchdogHost).delete()
        s.query(ServerSample).delete()
        s.query(Bot).filter(Bot.id.in_(ids)).delete()
        get_global(s).watchdog_token = ""
        s.commit()


def test_report_maps_bots_and_alerts(site):
    sent, (on_id, off_id, other_id) = site
    c = TestClient(app)
    assert c.get("/watchdog/agent.py?t=wrong").status_code == 403
    assert 'TOKEN = "wd-token-123"' in c.get("/watchdog/agent.py?t=wd-token-123").text
    inst = c.get("/watchdog/install.sh?t=wd-token-123").text
    import base64
    import re as _re
    agent_src = base64.b64decode(_re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', inst).group(1)).decode()
    assert 'TOKEN = "wd-token-123"' in agent_src and "sudo" not in inst.split("\n", 3)[2]  # agent embedded, no 2nd download
    assert subprocess.run(["sh", "-n"], input=inst, text=True).returncode == 0
    assert c.post("/watchdog/ping", headers={"X-Watchdog-Token": "wd-token-123"}).status_code == 200
    assert c.post("/watchdog/ping", headers={"X-Watchdog-Token": "x"}).status_code == 403
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
        assert "/watchdog/install.sh?t=wd-token-123" in html and "| sh</pre>" in html and "wget -qO-" in html and "sudo sh" not in html
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


@pytest.mark.skipif(not __import__("shutil").which("tmux"), reason="tmux not installed")
def test_agent_with_tmux_sessions_like_tmux_sh(tmp_path):
    """Bots started like the user's tmux.sh: session per bot, `cd freqtrade`, venv activated, `freqtrade trade -c`."""
    a = load_agent(tmp_path)
    a["START_GRACE"] = 2
    a["oom_killed"] = lambda pid: False
    a["from_docker"] = lambda: ({}, set())
    home = tmp_path / "root"
    ft = home / "freqtrade"
    (ft / ".venv" / "bin").mkdir(parents=True)
    (ft / "user_data" / "strategies").mkdir(parents=True)
    (ft / ".venv" / "bin" / "activate").write_text(f'export VIRTUAL_ENV="{ft}/.venv"\nexport PATH="{ft}/.venv/bin:$PATH"\n')
    fake = ft / ".venv" / "bin" / "freqtrade"
    fake.write_text(f"#!{sys.executable}\nimport time\ntry:\n    while True:\n        time.sleep(1)\n"
                    "except KeyboardInterrupt:\n    print('freqtrade - INFO - SIGINT received, aborting ...')\n")
    fake.chmod(0o755)
    (ft / "user_data" / "strategies" / "New_Daily_Config_45_20.json").write_text(
        '{"api_server": {"enabled": true, "listen_port": 18820}}')
    sock = str(tmp_path / "tmux.sock")
    env = {**os.environ, "HOME": str(home), "SHELL": "/bin/bash"}
    env.pop("TMUX", None)

    def tm(*args):
        return subprocess.run(["tmux", "-S", sock, *args], capture_output=True, text=True, env=env)

    def start_like_tmux_sh():
        tm("send-keys", "-t", "4520", "cd ", "C-m")
        tm("send-keys", "-t", "4520", "cd ./freqtrade", "C-m")
        tm("send-keys", "-t", "4520", "source ./.venv/bin/activate", "C-m")
        tm("send-keys", "-t", "4520", "freqtrade trade --config ./user_data/strategies/New_Daily_Config_45_20.json", "C-m")

    def wait_running(present=True):
        for _ in range(50):
            if ("port:18820" in a["from_processes"]()) == present:
                return True
            time.sleep(0.2)
        return False

    tm("new-session", "-d", "-s", "4520", "-x", "200", "-y", "50", "bash", "--norc", "--noprofile")
    try:
        start_like_tmux_sh()
        assert wait_running()
        st = {"bots": {}, "pending": []}
        a["check"](st, {})
        b = st["bots"]["port:18820"]
        assert b["kind"] == "tmux" and b["tmux"]["session"] == "4520" and b["cwd"] == str(ft)
        pane = b["tmux"]["pane"]

        # 1) killed (like the OOM killer / kill -9): restarted in the same pane
        os.kill(b["pid"], signal.SIGKILL)
        assert wait_running(False)
        assert [e["action"] for e in a["check"](st, {})] == ["gone"]
        ev = a["check"](st, {})
        assert [e["action"] for e in ev] == ["restarted"] and "4520" in ev[0]["detail"], ev
        now = a["from_processes"]()["port:18820"]
        assert now["tmux"]["pane"] == pane and now["tmux"]["session"] == "4520" and now["cwd"] == str(ft)
        a["check"](st, {})

        # 2) stopped by hand with Ctrl+C: respected, not restarted
        tm("send-keys", "-t", "4520", "C-c")
        assert wait_running(False)
        ev = a["check"](st, {})
        assert [e["action"] for e in ev] == ["stopped"] and "Ctrl+C" in ev[0]["detail"], ev
        for _ in range(3):
            assert a["check"](st, {}) == []
        assert "port:18820" not in a["from_processes"]()
        # started again by hand: watched again
        tm("send-keys", "-t", "4520", "freqtrade trade --config ./user_data/strategies/New_Daily_Config_45_20.json", "C-m")
        assert wait_running()
        assert a["check"](st, {}) == [] and st["bots"]["port:18820"]["status"] == "running"

        # 3) tmux server gone (as after a reboot): the session is created again with the same name
        tm("kill-server")
        assert wait_running(False)
        assert [e["action"] for e in a["check"](st, {})] == ["gone"]
        ev = a["check"](st, {})
        assert [e["action"] for e in ev] == ["restarted"], ev
        assert "4520" in tm("list-sessions", "-F", "#{session_name}").stdout.split()
        assert a["from_processes"]()["port:18820"]["tmux"]["session"] == "4520"
    finally:
        tm("kill-server")


# ------------------------------------------------------------------ Binance IP ban
def test_agent_binance_probe(tmp_path, monkeypatch):
    import io
    import urllib.error
    a = load_agent(tmp_path)
    st = {"bots": {"port:1": {"exchange": "binance", "mode": "futures"}, "port:2": {"exchange": "kucoin"}}}
    calls = []

    class Resp(io.BytesIO):
        headers = {"X-MBX-USED-WEIGHT-1M": "1990"}

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False
    answers = iter(["ok", "ban"])

    def urlopen(req, timeout=0):
        calls.append(req.full_url)
        if next(answers) == "ok":
            return Resp(b"{}")
        body = b'{"code":-1003,"msg":"Way too much request weight used; IP banned until 1794211200000."}'
        raise urllib.error.HTTPError(req.full_url, 418, "I'm a teapot", {}, io.BytesIO(body))
    monkeypatch.setattr(a["urllib"].request, "urlopen", urlopen)
    out = a["binance_probe"](st)
    assert list(out) == ["futures"] and out["futures"]["used_weight"] == 1990 and out["futures"]["limit"] == 2400
    assert calls == ["https://fapi.binance.com/fapi/v1/time"]  # only futures: the kucoin bot is ignored
    out = a["binance_probe"](st)
    assert out["futures"]["status"] == "banned" and out["futures"]["banned_until"] == 1794211200000
    a["time"].time = lambda: 1794211100.0  # still banned: Binance is not touched again
    a["binance_probe"](st)
    assert len(calls) == 2


def test_ban_pauses_server_bots_and_resumes_one_by_one(site, monkeypatch):
    import asyncio
    import datetime as dt

    from app.web import watchdog as wd
    sent, (on_id, off_id, other_id) = site
    with SessionLocal() as s:
        for i in (on_id, off_id, other_id):
            s.get(Bot, i).state = {"state": "running", "api": "ok"}
        s.commit()
    posts = []

    async def fake_post(bot_id, action):
        posts.append((bot_id, action))
        return "ok"
    monkeypatch.setattr(wd, "ft_post", fake_post)
    monkeypatch.setattr(wd, "BAN_COOLDOWN", 0)
    until_ms = int((utcnow() + dt.timedelta(minutes=5)).replace(tzinfo=dt.UTC).timestamp() * 1000)
    rep = {"host": "ft-v1", "ips": ["65.109.188.248"], "bots": [], "events": [],
           "binance": {"futures": {"status": "banned", "banned_until": until_ms, "limit": 2400}}}

    async def run():
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        h = {"X-Watchdog-Token": "wd-token-123"}
        await c.post("/watchdog/report", json=rep, headers=h)
        await asyncio.sleep(0.05)
        assert sorted(posts) == [(on_id, "stop"), (off_id, "stop")]  # both bots of that server, not the other server's
        assert wd.ban_paused_ids() == {on_id, off_id}
        assert sent[-1]["critical"] and "2 bots" in sent[-1]["text"] and "ft-v1" in sent[-1]["title"]
        await c.post("/watchdog/report", json=rep, headers=h)  # still banned: nothing new
        assert len(posts) == 2
        await wd.resume_due_hosts()
        assert len(posts) == 2  # ban not over yet
        with SessionLocal() as s:  # time passes, Binance answers again
            hh = s.query(WatchdogHost).one()
            hh.ban = {**hh.ban, "until": (utcnow() - dt.timedelta(seconds=1)).isoformat()}
            s.commit()
        ok = {**rep, "binance": {"futures": {"status": "ok", "used_weight": 40, "limit": 2400}}}
        await c.post("/watchdog/report", json=ok, headers=h)
        with SessionLocal() as s:
            get_global(s).ban_resume_gap = 0
            s.commit()
        await wd.resume_due_hosts()
        await asyncio.sleep(0.05)
        assert sorted(p for p in posts if p[1] == "start") == [(on_id, "start"), (off_id, "start")]
        assert wd.ban_paused_ids() == set() and "over" in sent[-1]["title"]
        await c.aclose()
    asyncio.run(run())


def test_weight_warning_and_repeated_429(site, monkeypatch):
    import asyncio

    from app.web import watchdog as wd
    sent, _ = site
    monkeypatch.setattr(wd, "ft_post", lambda *a: asyncio.sleep(0))

    async def run():
        await wd.handle_binance("ft-x", {"futures": {"status": "ok", "used_weight": 2100, "limit": 2400}}, [])
        await wd.handle_binance("ft-x", {"futures": {"status": "ok", "used_weight": 2200, "limit": 2400}}, [])
        assert len(sent) == 1 and "2100/2400" in sent[0]["text"] and not sent[0]["critical"]  # once per 30 min
        lim = {"futures": {"status": "limited", "retry_after": 60}}
        await wd.handle_binance("ft-x", lim, [])
        assert len(sent) == 1  # a single 429 is normal: Freqtrade backs off by itself
        await wd.handle_binance("ft-x", lim, [])
        assert len(sent) == 2 and sent[-1]["critical"]
    with SessionLocal() as s:
        s.add(WatchdogHost(hostname="ft-x", ips=[], last_seen=utcnow()))
        s.commit()
    asyncio.run(run())


def test_paused_bots_are_not_polled(monkeypatch):
    import asyncio

    from app.engine.botmonitor import monitor
    init_db()
    with SessionLocal() as s:
        a = Bot(name="P1", webhook_secret="pp-1", api_url="http://h:1", state={"ban_paused": True})
        b = Bot(name="P2", webhook_secret="pp-2", api_url="http://h:2", state={})
        s.add_all([a, b])
        s.commit()
        ids = (a.id, b.id)
    polled = []

    async def fake_poll(bot_id, alert=True):
        polled.append(bot_id)
    monkeypatch.setattr(monitor, "poll_bot", fake_poll)
    try:
        asyncio.run(monitor.poll_all())
        assert ids[1] in polled and ids[0] not in polled
    finally:
        with SessionLocal() as s:
            s.query(Bot).filter(Bot.id.in_(ids)).delete()
            s.commit()


# ------------------------------------------------------------------ servers dashboard
def _admin_client():
    import re

    from app.auth import captcha
    from app.security import hash_password
    captcha.MIN_SECONDS = 0
    with SessionLocal() as s:
        u = s.query(User).filter_by(email="srv-admin@x.com").first()
        if u is None:
            u = User(email="srv-admin@x.com", name="S", password_hash=hash_password("Xy-12345678-zz"), is_admin=True, lang="en",
                     status="active")
            s.add(u)
            s.commit()
        uid = u.id
    c = TestClient(app)
    page = c.get("/login").text
    data = {k: re.search(rf'name="{k}" value="([^"]+)"', page).group(1) for k in ("csrf", "ft")}
    c.post("/login", data={**data, "email": "srv-admin@x.com", "password": "Xy-12345678-zz"})
    return c, uid


def test_servers_dashboard(site, monkeypatch):
    import re

    from app.db import ServerSample as _S
    with SessionLocal() as s:
        s.query(_S).delete()
        s.commit()

    from app.db import ServerSample
    from app.web import admin_servers, watchdog as wd
    sent, (on_id, off_id, other_id) = site
    with SessionLocal() as s:
        s.get(Bot, on_id).state = {"api": "ok", "state": "running", "last_process": utcnow().isoformat(), "max_open_trades": 5}
        s.get(Bot, off_id).state = {"api": "ok", "state": "running", "ban_paused": True}
        s.get(Bot, other_id).state = {"api": "unreachable", "error": "ConnectError"}
        s.commit()
    rep = {"host": "ft-v1", "ips": ["65.109.188.248"], "version": "1.3", "events": [],
           "mem": {"MemTotal": 16000, "MemAvailable": 1600, "SwapTotal": 2000, "SwapFree": 500}, "load": [6.0, 4, 3],
           "host_info": {"cores": 4, "disk_pct": 71.0, "disk_free_gb": 40.2, "uptime_s": 90000},
           "binance": {"futures": {"status": "ok", "used_weight": 1800, "limit": 2400}},
           "bots": [{"key": "port:8015", "port": 8015, "status": "running", "kind": "tmux", "session": "4520",
                     "rss_mb": 812, "cpu": 12.5, "since": int(time.time()) - 7200},
                    {"key": "port:9999", "port": 9999, "status": "running", "session": "9999"}]}
    c = TestClient(app)
    c.post("/watchdog/report", json=rep, headers={"X-Watchdog-Token": "wd-token-123"})
    with SessionLocal() as s:
        smp = s.query(ServerSample).filter_by(host="ft-v1").one()
        assert smp.mem_pct == 90.0 and smp.swap_pct == 75.0 and smp.weight_pct == 75.0 and smp.load1 == 6.0
    admin, uid = _admin_client()
    try:
        html = admin.get("/admin/servers?lang=en").text
        assert "ft-v1" in html and "812 MB" in html and "12.5%" in html and "4520" in html and "1800/2400" in html
        assert "paused (ban)" in html and "API not answering" in html  # V1_8020 paused; V2 bot on a server w/o agent
        assert "port:9999" in html  # a bot running there that the panel doesn't know
        assert "no watchdog" in html and "51.91.55.39" in html
        calls = []

        async def fake_control(bot_id, act):
            calls.append((bot_id, act))
            return "ok"
        monkeypatch.setattr(admin_servers.monitor, "control", fake_control)
        posted = []

        async def fake_post(bot_id, act):
            posted.append((bot_id, act))
            return "ok"
        monkeypatch.setattr(admin_servers, "ft_post", fake_post)
        csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        admin.post("/admin/servers/action", data={"csrf": csrf, "bot_id": str(on_id), "action": "stop"})
        assert calls == [(on_id, "stop")]
        admin.post("/admin/servers/action", data={"csrf": csrf, "host": "ft-v1", "action": "stop_all"})
        assert sorted(posted) == [(on_id, "stop"), (off_id, "stop")]
        assert admin.post("/admin/servers/action", data={"csrf": csrf, "bot_id": str(on_id), "action": "rm -rf"},
                          follow_redirects=False).status_code == 400
        assert TestClient(app).get("/admin/servers", follow_redirects=False).status_code in (303, 403)  # admins only
    finally:
        with SessionLocal() as s:
            s.query(ServerSample).delete()
            s.query(User).filter_by(id=uid).delete()
            s.commit()
        wd._tasks.clear()


def test_monitoring_alerts_not_silenced_by_muting_freqtrade_events(monkeypatch):
    import asyncio

    from app.engine.botmonitor import monitor
    from app.notify import alerts as al
    from app.security import hash_password
    from app.telegram import bot as tg
    init_db()
    with SessionLocal() as s:
        u = User(email="mute@x.com", name="M", password_hash=hash_password("Xy-12345678-zz"), is_admin=True,
                 status="active", telegram_chat_id="777", notify={"bot_status": False, "bot_trades": False})
        s.add(u)
        s.commit()
        uid = u.id
    got = []

    async def fake_send(chat_id, text, user_id=None, kind="reply"):
        if str(chat_id) == "777":  # other admins may exist from other test modules
            got.append((kind, text))
        return True

    async def fake_phone(user, title, text, url="/", tag=""):
        return {"push": 0, "alert_bot": False}
    monkeypatch.setattr(tg, "send", fake_send)
    monkeypatch.setattr(al, "phone", fake_phone)

    async def run():
        monitor._alert_changes("V1_8015", {"api": "ok", "state": "running"}, {"api": "unreachable", "error": "x"})
        monitor.notify_admins("bot_status", lambda L: "freqtrade chatter")
        await asyncio.sleep(0.05)
    try:
        asyncio.run(run())
        kinds = [k for k, _ in got]
        assert kinds == ["monitoring"], got  # the down alert arrives, the muted Freqtrade message does not
    finally:
        with SessionLocal() as s:
            s.query(User).filter_by(id=uid).delete()
            s.commit()


def test_server_alias_in_page_and_alerts(site):
    import re
    sent, _ = site
    c = TestClient(app)
    hdr = {"X-Watchdog-Token": "wd-token-123"}
    c.post("/watchdog/report", json={"host": "355672", "ips": ["65.109.188.248"], "bots": [], "events": []}, headers=hdr)
    admin, uid = _admin_client()
    try:
        csrf = re.search(r'name="csrf" value="([^"]+)"', admin.get("/admin/servers").text).group(1)
        admin.post("/admin/servers/alias", data={"csrf": csrf, "host": "355672", "alias": "  V1  Hetzner "})
        assert "V1 Hetzner" in admin.get("/admin/servers").text
        c.post("/watchdog/report", json={"host": "355672", "ips": ["65.109.188.248"], "bots": [], "events": [
            {"key": "port:8015", "port": 8015, "action": "restarted", "down_for": 60}]}, headers=hdr)
        assert "(V1 Hetzner)" in sent[-1]["text"]
    finally:
        with SessionLocal() as s:
            s.query(User).filter_by(id=uid).delete()
            s.commit()


def test_agent_registers_bot_and_writes_webhook(site, tmp_path, monkeypatch):
    """A bot nobody added in the panel: the agent finds it, the site adds it, the agent writes its webhook + reloads."""
    import json as _json
    import socket as _socket
    from app.security import decrypt
    sent, _ = site
    with _socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    a = load_agent(tmp_path)
    a["from_docker"] = lambda: ({}, set())
    a["host_ips"] = lambda: ["65.109.188.248"]
    botdir = tmp_path / "ft"
    botdir.mkdir()
    cfg = botdir / "config.json"
    cfg.write_text(_json.dumps({"bot_name": "V9_9015", "strategy": "MyStrat", "dry_run": True,
                                "api_server": {"enabled": True, "listen_ip_address": "127.0.0.1", "listen_port": port,
                                               "username": "ftuser", "password": "ftpass"}}))
    marker = tmp_path / "reloaded.txt"
    fake = tmp_path / "freqtrade"  # answers POST /api/v1/reload_config like Freqtrade
    fake.write_text(f"""#!{sys.executable}
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        open({str(marker)!r}, "w").write(self.path + " " + self.headers.get("Authorization", ""))
        self.send_response(200); self.end_headers(); self.wfile.write(b'{{"status":"Reloading config ..."}}')
http.server.HTTPServer(("127.0.0.1", {port}), H).serve_forever()
""")
    fake.chmod(0o755)
    p = subprocess.Popen([sys.executable, str(fake), "trade", "-c", "config.json"], cwd=botdir, start_new_session=True)
    client = TestClient(app)

    class Resp:
        def __init__(self, r):
            self.r = r

        def read(self):
            return self.r.content

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def urlopen(req, timeout=0):
        if req.full_url.startswith("http://site.test"):  # the agent's report -> our app
            return Resp(client.post(req.full_url.replace("http://site.test", ""), content=req.data,
                                    headers=dict(req.header_items())))
        return real_urlopen(req, timeout=timeout)  # the bot's own API (reload_config)
    real_urlopen = a["urllib"].request.urlopen
    monkeypatch.setattr(a["urllib"].request, "urlopen", urlopen)
    a["TOKEN"] = "wd-token-123"
    try:
        time.sleep(0.6)
        st = {"bots": {}, "pending": []}
        a["check"](st, {})
        b = st["bots"][f"port:{port}"]
        assert b["ft"]["bot_name"] == "V9_9015" and b["ft"]["api_user"] == "ftuser" and b["ft"]["webhook_url"] == ""
        ans = a["report"](st, [])
        with SessionLocal() as s:
            bot = s.query(Bot).filter_by(name="V9_9015").one()
            assert bot.api_url == f"http://65.109.188.248:{port}" and bot.api_user == "ftuser"
            assert decrypt(bot.api_pass_enc) == "ftpass" and bot.group_id is None  # grouping stays with the admin
            secret = bot.webhook_secret
        assert any("V9_9015" in m["text"] for m in sent)  # admins are told
        hook = ans["webhooks"][str(port)]
        assert hook["url"].endswith("/webhook/" + secret) and hook["enabled"]
        ev = a["apply_webhooks"](st, ans)
        assert [e["action"] for e in ev] == ["webhook_set"], ev
        written = _json.loads(cfg.read_text())
        assert written["webhook"]["url"] == hook["url"] and written["api_server"]["password"] == "ftpass"
        assert list(botdir.glob("config.json.bak-relay-*"))  # backup kept
        assert marker.read_text().startswith("/api/v1/reload_config Basic ")  # reloaded with the bot's own login
        a["check"](st, {})
        assert a["apply_webhooks"](st, ans) == []  # already set: nothing more to do
        a["report"](st, [])
        with SessionLocal() as s:
            assert s.query(Bot).filter_by(name="V9_9015").count() == 1  # not added twice
    finally:
        os.kill(p.pid, signal.SIGKILL)
        with SessionLocal() as s:
            s.query(Bot).filter_by(name="V9_9015").delete()
            s.commit()
