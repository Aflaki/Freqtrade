"""Generates freqtrade_webhooks.py: a standalone script that writes each bot's webhook into its Freqtrade config."""
from __future__ import annotations

import datetime as dt
import pprint

SCRIPT = r'''#!/usr/bin/env python3
"""
Signal Relay — add each bot's webhook to its Freqtrade config.
Generated __DATE__ for __SITE__  (__COUNT__ bots)

USAGE (on the server where the Freqtrade configs are):
  python3 freqtrade_webhooks.py                 # dry run: search /root /home /opt /srv and show what would change
  python3 freqtrade_webhooks.py /root/ft_userdata --apply
                                                # write the changes (a backup is kept next to every file)
  python3 freqtrade_webhooks.py --list          # show bots and their webhook URLs
  python3 freqtrade_webhooks.py --map /path/config.json=BotName --apply
                                                # force a config -> bot match
  python3 freqtrade_webhooks.py --separate --apply
                                                # don't touch configs; write webhook-<bot>.json next to them instead

HOW A CONFIG IS MATCHED TO A BOT
  1. api_server.listen_port (+ listen_ip_address) == the bot's API address in the panel
  2. otherwise bot_name in the config == the bot's name in the panel
  3. otherwise --map
Afterwards press "Reload config on all bots" in the panel (or restart the bots).
Needs only Python 3.8+, no extra packages.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
from urllib.parse import urlparse

BOTS = __BOTS__
TEMPLATE = __TEMPLATE__
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "backtest_results", "hyperopt_results",
             "data", "logs", "plot", "notebooks", "freqaimodels", "strategies", "hyperopts", ".cache", "proc", "sys"}


def strip_json(text):
    """Freqtrade configs may contain // and /* */ comments and trailing commas; remove them outside strings."""
    out, i, n, in_str, had_comments = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if c == '"':
                in_str = False
            i += 1; continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if text.startswith("//", i):
            had_comments = True
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            had_comments = True
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        out.append(c); i += 1
    s = "".join(out)
    s = re.sub(r",(\s*[}\]])", r"\1", s)  # trailing commas (strings can't contain a raw ,} after the strip above often enough to matter)
    return s, had_comments


def load(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw), False
    except ValueError:
        clean, had = strip_json(raw)
        return json.loads(clean), had


def looks_like_freqtrade(text):
    return '"exchange"' in text and any(k in text for k in ('"api_server"', '"stake_currency"', '"pair_whitelist"', '"bot_name"'))


def find_configs(roots):
    seen = set()
    for root in roots:
        if os.path.isfile(root):
            yield os.path.abspath(root); continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if not fn.endswith(".json") or fn.startswith("webhook-") or ".bak" in fn:
                    continue
                p = os.path.abspath(os.path.join(dirpath, fn))
                if p in seen:
                    continue
                seen.add(p)
                try:
                    if os.path.getsize(p) > 2_000_000:
                        continue
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        if looks_like_freqtrade(f.read(20000)):
                            yield p
                except OSError:
                    pass


def match(cfg, path, forced):
    if path in forced:
        key = forced[path]
        return next((b for b in BOTS if str(b["id"]) == key or b["name"].lower() == key.lower()), None), "map"
    api = cfg.get("api_server") or {}
    port, ip = api.get("listen_port"), str(api.get("listen_ip_address") or "")
    if port:
        cands = [b for b in BOTS if b["api_port"] == int(port)]
        exact = [b for b in cands if b["api_host"] == ip]
        if len(exact) == 1:
            return exact[0], "api_server %s:%s" % (ip, port)
        local = {"0.0.0.0", "::", "127.0.0.1", "localhost", ""}
        loose = [b for b in cands if ip in local or b["api_host"] in local]
        if len(cands) == 1 or len(loose) == 1:
            return (cands if len(cands) == 1 else loose)[0], "api_server port %s" % port
    name = str(cfg.get("bot_name") or "").strip().lower()
    if name:
        byname = [b for b in BOTS if b["name"].strip().lower() == name]
        if len(byname) == 1:
            return byname[0], "bot_name"
    return None, ""


def block(bot):
    b = dict(TEMPLATE)
    b["enabled"] = True
    b["url"] = bot["webhook_url"]
    return b


def main():
    ap = argparse.ArgumentParser(description="Add Signal Relay webhooks to Freqtrade configs")
    ap.add_argument("paths", nargs="*", help="folders or config files to scan (default: /root /home /opt /srv)")
    ap.add_argument("--apply", action="store_true", help="write the changes (otherwise dry run)")
    ap.add_argument("--separate", action="store_true", help="write webhook-<bot>.json next to the config instead of editing it")
    ap.add_argument("--map", action="append", default=[], metavar="CONFIG=BOT", help="force a match (bot name or id)")
    ap.add_argument("--list", action="store_true", help="list bots and webhook URLs")
    a = ap.parse_args()

    if a.list:
        for b in BOTS:
            print("%-4s %-28s api=%s:%s\n     %s" % (b["id"], b["name"], b["api_host"] or "-", b["api_port"] or "-", b["webhook_url"]))
        return
    forced = {}
    for m in a.map:
        if "=" not in m:
            sys.exit("--map needs CONFIG=BOT")
        k, v = m.split("=", 1)
        forced[os.path.abspath(k)] = v
    roots = a.paths or [p for p in ("/root", "/home", "/opt", "/srv") if os.path.isdir(p)]
    configs = list(find_configs(roots + list(forced)))
    print("Scanning %s -> %d Freqtrade config file(s)\n" % (", ".join(roots), len(configs)))

    done, unmatched, used = 0, [], {}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for path in configs:
        try:
            cfg, had_comments = load(path)
        except Exception as e:
            print("  ! cannot read %s: %s" % (path, e)); continue
        if not isinstance(cfg, dict):
            continue
        bot, how = match(cfg, path, forced)
        if bot is None:
            if (cfg.get("api_server") or {}).get("listen_port") or cfg.get("bot_name"):
                unmatched.append(path)
            continue
        if bot["id"] in used:
            print("  ! %s also matches bot '%s' (already matched %s) - skipped" % (path, bot["name"], used[bot["id"]])); continue
        used[bot["id"]] = path
        current = (cfg.get("webhook") or {}).get("url")
        state = "already set" if current == bot["webhook_url"] else ("replace" if current else "add")
        print("  %-11s %s\n              -> bot '%s' (matched by %s)" % (state.upper(), path, bot["name"], how))
        if state == "already set" or not a.apply:
            continue
        if a.separate:
            out = os.path.join(os.path.dirname(path), "webhook-%s.json" % re.sub(r"[^A-Za-z0-9_-]+", "_", bot["name"]))
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"webhook": block(bot)}, f, indent=4, ensure_ascii=False)
            print("              wrote %s\n              add to the bot's start command:  --config %s" % (out, out))
        else:
            backup = "%s.bak-%s" % (path, stamp)
            shutil.copy2(path, backup)
            cfg["webhook"] = block(bot)
            st = os.stat(path)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
                f.write("\n")
            try:
                os.chown(path, st.st_uid, st.st_gid)
            except OSError:
                pass
            print("              written (backup: %s)%s" % (backup, "  [comments in the file were removed]" if had_comments else ""))
        done += 1

    missing = [b for b in BOTS if b["id"] not in used]
    print("\nMatched %d of %d bots." % (len(used), len(BOTS)))
    for b in missing:
        print("  - no config found for bot '%s' (api %s:%s) - use --map /path/config.json=%s" % (b["name"], b["api_host"] or "?", b["api_port"] or "?", b["id"]))
    for p in unmatched:
        print("  ? config without a matching bot: %s" % p)
    if not a.apply:
        print("\nDry run - nothing was changed. Run again with --apply to write.")
    elif done:
        print("\nDone. Now press 'Reload config on all bots' in the panel (or restart the bots).")


if __name__ == "__main__":
    main()
'''


def build_script(site: str, base_url: str, bots: list, template: dict) -> str:
    entries = []
    for b in bots:
        u = None
        try:
            from urllib.parse import urlparse
            u = urlparse(b.api_url) if b.api_url else None
        except ValueError:
            pass
        entries.append({"id": b.id, "name": b.name, "api_host": (u.hostname or "") if u else "",
                        "api_port": (u.port or 0) if u else 0,
                        "webhook_url": f"{base_url.rstrip('/')}/webhook/{b.webhook_secret}"})
    tpl = {k: v for k, v in template.items() if k not in ("url", "enabled")}
    return (SCRIPT.replace("__DATE__", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__SITE__", base_url).replace("__COUNT__", str(len(entries)))
            .replace("__BOTS__", pprint.pformat(entries, width=120, sort_dicts=False))
            .replace("__TEMPLATE__", pprint.pformat(tpl, width=120, sort_dicts=False)))
