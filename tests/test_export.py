"""The generated freqtrade_webhooks.py must find, match and patch Freqtrade configs safely."""
import json
import subprocess
import sys
import types
from pathlib import Path

from app.web.export_script import build_script

TEMPLATE = json.loads((Path(__file__).resolve().parents[1] / "freqtrade" / "webhook-config.json").read_text())["webhook"]


def bot(i, name, api, secret):
    return types.SimpleNamespace(id=i, name=name, api_url=api, webhook_secret=secret)


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=True).stdout


def test_script_end_to_end(tmp_path):
    bots = [bot(1, "NFI v1", "http://65.109.188.248:8015", "s1"), bot(2, "Scalper", "http://65.109.188.248:8016", "s2"),
            bot(3, "Swing", "", "s3"), bot(4, "Lost", "http://65.109.188.248:8099", "s4")]
    script = tmp_path / "freqtrade_webhooks.py"
    script.write_text(build_script("Test", "https://panel.example.com", bots, TEMPLATE))

    root = tmp_path / "ft"
    (root / "bot1/user_data").mkdir(parents=True)
    (root / "bot2/user_data").mkdir(parents=True)
    (root / "bot3").mkdir()
    (root / "other").mkdir()
    (root / "bot1/user_data/backtest_results").mkdir()
    c1 = root / "bot1/user_data/config.json"
    c1.write_text(json.dumps({"exchange": {"name": "binance"}, "stake_currency": "USDT",
                              "api_server": {"enabled": True, "listen_ip_address": "65.109.188.248", "listen_port": 8015}}, indent=4))
    c2 = root / "bot2/user_data/config.json"  # comments + trailing comma, like many real configs
    c2.write_text('{\n  // my scalper\n  "exchange": {"name": "bybit", "pair_whitelist": ["BTC/USDT:USDT",],},\n'
                  '  "stake_currency": "USDT", /* api */ "api_server": {"listen_ip_address": "0.0.0.0", "listen_port": 8016},\n'
                  '  "url_with_slashes": "http://x//y",\n}\n')
    c3 = root / "bot3/config.json"
    c3.write_text(json.dumps({"bot_name": "swing", "exchange": {"name": "okx"}, "stake_currency": "USDT"}))
    c4 = root / "other/config.json"
    c4.write_text(json.dumps({"exchange": {"name": "kraken"}, "stake_currency": "EUR", "api_server": {"listen_port": 9999}}))
    skip = root / "bot1/user_data/backtest_results/result.json"
    skip.write_text(json.dumps({"exchange": {}, "stake_currency": "USDT", "api_server": {"listen_port": 8015}}))

    out = run(script, str(root))
    assert "Dry run" in out and "Matched 3 of 4 bots" in out
    assert "no config found for bot 'Lost'" in out and "other/config.json" in out
    assert json.loads(c1.read_text()).get("webhook") is None  # dry run changed nothing

    out = run(script, str(root), "--apply")
    w1 = json.loads(c1.read_text())["webhook"]
    w2 = json.loads(c2.read_text())["webhook"]
    w3 = json.loads(c3.read_text())["webhook"]
    assert w1["url"] == "https://panel.example.com/webhook/s1" and w1["enabled"] is True and w1["format"] == "json"
    assert w2["url"].endswith("/webhook/s2") and json.loads(c2.read_text())["url_with_slashes"] == "http://x//y"
    assert w3["url"].endswith("/webhook/s3")  # matched by bot_name
    assert w1["exit_fill"]["cumulative_profit"] == "{cumulative_profit}" and "status" in w1
    assert "comments in the file were removed" in out
    assert len(list((root / "bot1/user_data").glob("config.json.bak-*"))) == 1
    assert "webhook" not in json.loads(skip.read_text())  # backtest results untouched

    out = run(script, str(root), "--apply")
    assert out.count("ALREADY SET") == 3  # idempotent

    out = run(script, str(root), "--map", f"{c4}=Lost", "--separate", "--apply")
    assert (root / "other/webhook-Lost.json").exists() and "webhook" not in json.loads(c4.read_text())

    assert "https://panel.example.com/webhook/s4" in run(script, "--list")


def test_download_requires_admin():
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        r = c.get("/admin/bots/export/freqtrade_webhooks.py", follow_redirects=False)
        assert r.status_code in (303, 403)
        assert c.get("/favicon.ico").status_code == 200 and c.get("/site-icon").status_code == 200
        assert c.get("/manifest.webmanifest").json()["icons"]
