"""The generated freqtrade_webhooks.py must find, match and patch Freqtrade configs safely."""
import json
import subprocess
import sys
import time
import types
from pathlib import Path

from app.web.export_script import build_script

TEMPLATE = json.loads((Path(__file__).resolve().parents[1] / "freqtrade" / "webhook-config.json").read_text())["webhook"]


def bot(i, name, api, secret):
    return types.SimpleNamespace(id=i, name=name, api_url=api, webhook_secret=secret)


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=True).stdout


def make_script(tmp_path, bots):
    script = tmp_path / "freqtrade_webhooks.py"
    script.write_text(build_script("Test", "https://panel.example.com", bots, TEMPLATE))
    return script


def test_file_scan(tmp_path):
    bots = [bot(1, "NFI v1", "http://65.109.188.248:8015", "s1"), bot(2, "Scalper", "http://65.109.188.248:8016", "s2"),
            bot(3, "Swing", "", "s3"), bot(4, "Lost", "http://51.91.55.39:8099", "s4")]
    script = make_script(tmp_path, bots)
    root = tmp_path / "ft"
    (root / "bot1/user_data/backtest_results").mkdir(parents=True)
    (root / "bot2/user_data").mkdir(parents=True)
    (root / "bot3").mkdir()
    (root / "other").mkdir()
    c1 = root / "bot1/user_data/config.json"
    c1.write_text(json.dumps({"exchange": {"name": "binance"}, "stake_currency": "USDT",
                              "api_server": {"enabled": True, "listen_ip_address": "65.109.188.248", "listen_port": 8015}}, indent=4))
    c2 = root / "bot2/user_data/config.json"  # comments + trailing comma
    c2.write_text('{\n  // my scalper\n  "exchange": {"name": "bybit", "pair_whitelist": ["BTC/USDT:USDT",],},\n'
                  '  "stake_currency": "USDT", /* api */ "api_server": {"listen_ip_address": "0.0.0.0", "listen_port": 8016},\n'
                  '  "url_with_slashes": "http://x//y",\n}\n')
    c3 = root / "bot3/config.json"
    c3.write_text(json.dumps({"bot_name": "swing", "exchange": {"name": "okx"}, "stake_currency": "USDT"}))
    c4 = root / "other/config.json"
    c4.write_text(json.dumps({"exchange": {"name": "kraken"}, "stake_currency": "EUR", "api_server": {"listen_port": 9999}}))
    skip = root / "bot1/user_data/backtest_results/result.json"
    skip.write_text(json.dumps({"exchange": {}, "stake_currency": "USDT", "api_server": {"listen_port": 8015}}))
    # a Freqtrade source checkout: its tests and examples must be ignored
    src = root / "freqtrade"
    (src / "freqtrade").mkdir(parents=True)
    (src / "freqtrade/__init__.py").write_text("")
    (src / "tests/testdata").mkdir(parents=True)
    (src / "tests/testdata/config.tests.json").write_text(json.dumps({"exchange": {}, "stake_currency": "BTC", "bot_name": "x"}))
    (src / "config_examples").mkdir()
    (src / "config_examples/config_full.example.json").write_text(json.dumps({"exchange": {}, "stake_currency": "USDT", "api_server": {"listen_port": 8080}}))

    out = run(script, str(root))
    assert "Dry run" in out and "Matched 3 bots" in out, out
    assert "not found: 'Lost'" in out and "run this script on that server too" in out
    assert "other/config.json" in out and "tests" not in out and "config_examples" not in out
    assert json.loads(c1.read_text()).get("webhook") is None

    out = run(script, str(root), "--apply")
    w1, w2, w3 = (json.loads(c.read_text())["webhook"] for c in (c1, c2, c3))
    assert w1["url"] == "https://panel.example.com/webhook/s1" and w1["enabled"] is True and w1["format"] == "json"
    assert w2["url"].endswith("/webhook/s2") and json.loads(c2.read_text())["url_with_slashes"] == "http://x//y"
    assert w3["url"].endswith("/webhook/s3")
    assert w1["exit_fill"]["cumulative_profit"] == "{cumulative_profit}" and "status" in w1
    assert "comments in the file were removed" in out
    assert len(list((root / "bot1/user_data").glob("config.json.bak-*"))) == 1
    assert "webhook" not in json.loads(skip.read_text())
    assert run(script, str(root), "--apply").count("ALREADY SET") == 3

    out = run(script, str(root), "--map", f"{c4}=Lost", "--separate", "--apply")
    assert (root / "other/webhook-Lost.json").exists() and "webhook" not in json.loads(c4.read_text())
    assert "https://panel.example.com/webhook/s4" in run(script, "--list")


def test_running_process_with_split_configs(tmp_path):
    """Many bots share a base config + each has a small own config with api_server: found via the process."""
    bots = [bot(7, "V1_6515", "http://65.109.188.248:6515", "s7")]
    script = make_script(tmp_path, bots)
    wd = tmp_path / "somewhere/odd/place"
    wd.mkdir(parents=True)
    (wd / "base.json").write_text(json.dumps({"exchange": {"name": "binance"}, "stake_currency": "USDT"}))
    own = wd / "bot-6515.json"  # no "exchange" key at all
    own.write_text(json.dumps({"api_server": {"enabled": True, "listen_ip_address": "65.109.188.248", "listen_port": 6515}}))
    fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "freqtrade", "trade",
                             "--config", "base.json", "-c", "bot-6515.json"], cwd=wd)
    try:
        time.sleep(0.5)
        out = run(script, str(tmp_path / "empty-dir-does-not-exist"), "--apply")
    finally:
        fake.kill()
    assert "Running Freqtrade bots on this server: 1" in out, out
    assert "-> bot 'V1_6515'" in out
    assert json.loads(own.read_text())["webhook"]["url"].endswith("/webhook/s7")
    assert "webhook" not in json.loads((wd / "base.json").read_text())  # the shared base stays untouched


def test_download_requires_admin():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.get("/admin/bots/export/freqtrade_webhooks.py", follow_redirects=False)
        assert r.status_code in (303, 403)
        assert c.get("/favicon.ico").status_code == 200 and c.get("/site-icon").status_code == 200
        assert c.get("/manifest.webmanifest").json()["icons"]


def test_docker_container_via_mounts(tmp_path):
    """Bots in Docker: --config paths inside the container are mapped to host files through the volume mounts."""
    import os
    host_ud = tmp_path / "ft_userdata/user_data"
    host_ud.mkdir(parents=True)
    cfg = host_ud / "config-8020.json"
    cfg.write_text(json.dumps({"exchange": {"name": "binance"}, "stake_currency": "USDT",
                               "api_server": {"enabled": True, "listen_ip_address": "0.0.0.0", "listen_port": 8020}}))
    inspect = [{"Name": "/freqtrade_v1_8020", "Config": {
        "Image": "freqtradeorg/freqtrade:stable", "WorkingDir": "/freqtrade", "Entrypoint": ["freqtrade"],
        "Cmd": ["trade", "--logfile", "/freqtrade/user_data/logs/x.log", "--config", "/freqtrade/user_data/config-8020.json",
                "--strategy", "NFI"]},
        "Mounts": [{"Source": str(host_ud), "Destination": "/freqtrade/user_data"}]}]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = ps ]; then echo abc123; else cat '%s'; fi\n" % (tmp_path / "inspect.json"))
    fake.chmod(0o755)
    (tmp_path / "inspect.json").write_text(json.dumps(inspect))
    script = make_script(tmp_path, [bot(12, "V1_8020", "http://65.109.188.248:8020", "s12")])
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    out = subprocess.run([sys.executable, str(script), str(tmp_path / "nothing"), "--apply"], capture_output=True,
                         text=True, check=True, env=env).stdout
    assert "docker freqtrade_v1_8020" in out and "-> bot 'V1_8020'" in out, out
    assert json.loads(cfg.read_text())["webhook"]["url"].endswith("/webhook/s12")
