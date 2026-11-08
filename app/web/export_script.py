"""Generates freqtrade_webhooks.py: a standalone script that writes each bot's webhook into its Freqtrade config."""
from __future__ import annotations

import datetime as dt
import pprint

SCRIPT = r'''#!/usr/bin/env python3
"""
Signal Relay — add each bot's webhook to its Freqtrade config.
Generated __DATE__ for __SITE__  (__COUNT__ bots)

USAGE (on the server where the Freqtrade configs are):
  python3 freqtrade_webhooks.py                 # dry run: find the running bots (+ /root /home /opt /srv) and show changes
  python3 freqtrade_webhooks.py -v              # same, with details for every config found
  python3 freqtrade_webhooks.py /root/ft_userdata --apply
                                                # write the changes (a backup is kept next to every file)
  python3 freqtrade_webhooks.py --list          # show bots and their webhook URLs
  python3 freqtrade_webhooks.py --map /path/config.json=BotName --apply
                                                # force a config -> bot match
  python3 freqtrade_webhooks.py --separate --apply
                                                # don't touch configs; write webhook-<bot>.json next to them instead

WHERE IT LOOKS
  running Freqtrade processes (their --config files), Docker containers (through volume mounts),
  then config files under /root /home /opt /srv /data /freqtrade (Freqtrade's own tests/examples are skipped)
  Bots on another server: run the same script there too.

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
import shlex
import shutil
import subprocess
import sys

BOTS = __BOTS__
TEMPLATE = __TEMPLATE__
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "backtest_results", "hyperopt_results",
             "data", "logs", "plot", "notebooks", "freqaimodels", "strategies", "hyperopts", ".cache", "proc", "sys",
             "tests", "testdata", "config_examples", "docs", "build_helpers", "site-packages", "dist-packages"}
CONFIG_FLAGS = ("-c", "--config")


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
    s = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return s, had_comments


def load(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw), False
    except ValueError:
        clean, had = strip_json(raw)
        return json.loads(clean), had


def deep_merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def local_ips():
    ips = {"127.0.0.1", "0.0.0.0", "localhost", ""}
    try:
        ips |= set(subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split())
    except Exception:
        pass
    return ips


LOCAL = local_ips()


# ---------------------------------------------------------------- where are the bots?
def config_args(argv, cwd, default):
    """Config files from a freqtrade command line (-c / --config, repeatable). Default: config.json in cwd."""
    files = []
    i = argv.index("trade") + 1 if "trade" in argv else 0  # only freqtrade's own arguments (not e.g. python -c)
    while i < len(argv):
        a = argv[i]
        if a in CONFIG_FLAGS and i + 1 < len(argv):
            files.append(argv[i + 1]); i += 2; continue
        if a.startswith("--config="):
            files.append(a.split("=", 1)[1])
        i += 1
    if not files:
        files = [default]
    return [f if f.startswith("/") else os.path.normpath(os.path.join(cwd, f)) for f in files]


def from_processes():
    """Freqtrade bots running directly on this machine (not in Docker): read /proc/<pid>/cmdline."""
    bots = []
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
            cwd = os.readlink("/proc/%s/cwd" % pid)
        except OSError:
            continue
        joined = " ".join(argv)
        if "freqtrade" not in joined or " trade" not in " " + joined.split("freqtrade", 1)[-1]:
            continue
        try:  # a process inside a container has a different root filesystem: handled by from_docker()
            r, me = os.stat("/proc/%s/root" % pid), os.stat("/proc/self/root")
            if (r.st_dev, r.st_ino) != (me.st_dev, me.st_ino):
                continue
        except OSError:
            continue
        bots.append({"where": "pid %s" % pid, "configs": config_args(argv, cwd, os.path.join(cwd, "config.json"))})
    return bots


def from_docker():
    """Freqtrade containers: map their --config paths to files on this machine through the volume mounts."""
    try:
        ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=20).stdout.split()
    except Exception:
        return []
    bots = []
    for cid in ids:
        try:
            info = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=20).stdout)[0]
        except Exception:
            continue
        cfg = info.get("Config") or {}
        argv = []
        for x in (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []):
            argv += shlex.split(x) if " " in x else [x]  # e.g. sh -c "freqtrade trade --config ..."
        image = str(cfg.get("Image", ""))
        if "trade" not in argv or ("freqtrade" not in image and not any("freqtrade" in a for a in argv)):
            continue
        workdir = cfg.get("WorkingDir") or "/freqtrade"
        mounts = sorted(info.get("Mounts") or [], key=lambda m: -len(m.get("Destination", "")))
        host = []
        for p in config_args(argv, workdir, os.path.join(workdir, "config.json")):
            for m in mounts:
                d = m.get("Destination", "").rstrip("/")
                if p == d or p.startswith(d + "/"):
                    host.append(m.get("Source", "").rstrip("/") + p[len(d):]); break
            else:
                host.append(None)
        name = (info.get("Name") or cid).lstrip("/")
        bots.append({"where": "docker %s" % name, "configs": host})
    return bots


def looks_like_freqtrade(text):
    return any(k in text for k in ('"api_server"', '"bot_name"')) or (
        '"exchange"' in text and any(k in text for k in ('"stake_currency"', '"pair_whitelist"')))


def from_files(roots):
    """Fallback: scan folders for config files (skips Freqtrade's own source code, tests and examples)."""
    seen = set()
    for root in roots:
        if os.path.isfile(root):
            yield os.path.abspath(root); continue
        for dirpath, dirnames, filenames in os.walk(root):
            if os.path.exists(os.path.join(dirpath, "freqtrade", "__init__.py")):
                dirnames[:] = [d for d in dirnames if d == "user_data"]  # a source checkout: only its user_data
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


# ---------------------------------------------------------------- matching
def match(cfg, forced_key=None):
    if forced_key:
        return next((b for b in BOTS if str(b["id"]) == forced_key or b["name"].lower() == forced_key.lower()), None), "map"
    api = cfg.get("api_server") or {}
    port, ip = api.get("listen_port"), str(api.get("listen_ip_address") or "")
    if port:
        try:
            port = int(port)
        except ValueError:
            port = 0
        cands = [b for b in BOTS if b["api_port"] == port]
        for pick, how in (([b for b in cands if b["api_host"] == ip], "api_server %s:%s" % (ip, port)),
                          ([b for b in cands if b["api_host"] in LOCAL], "api_server port %s (this server)" % port),
                          (cands, "api_server port %s" % port)):
            if len(pick) == 1:
                return pick[0], how
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


def describe(cfg):
    api = cfg.get("api_server") or {}
    return "api_server=%s:%s bot_name=%s" % (api.get("listen_ip_address", "-"), api.get("listen_port", "-"), cfg.get("bot_name", "-"))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Add Signal Relay webhooks to Freqtrade configs")
    ap.add_argument("paths", nargs="*", help="extra folders or config files to scan (default: /root /home /opt /srv /data /freqtrade)")
    ap.add_argument("--apply", action="store_true", help="write the changes (otherwise dry run)")
    ap.add_argument("--separate", action="store_true", help="write webhook-<bot>.json next to the config instead of editing it")
    ap.add_argument("--map", action="append", default=[], metavar="CONFIG=BOT", help="force a match (bot name or id)")
    ap.add_argument("--list", action="store_true", help="list bots and webhook URLs")
    ap.add_argument("-v", "--verbose", action="store_true", help="show every config found and why it (didn't) match")
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

    # 1) running bots (exact config files they use), 2) docker, 3) file scan
    groups = []  # each: {"where", "configs": [paths]}
    running = from_processes() + from_docker()
    print("Running Freqtrade bots on this server: %d" % len(running))
    for r in running:
        missing = [c for c in r["configs"] if not c or not os.path.exists(c)]
        print("  %s  ->  %s%s" % (r["where"], ", ".join(c or "?" for c in r["configs"]),
                                   "   (! not visible on this machine)" if missing else ""))
        if not missing:
            groups.append(r)
    used_files = {c for g in groups for c in g["configs"]}
    roots = a.paths or [p for p in ("/root", "/home", "/opt", "/srv", "/data", "/freqtrade") if os.path.isdir(p)]
    scanned = [p for p in from_files(roots + list(forced)) if p not in used_files]
    groups += [{"where": "file", "configs": [p]} for p in scanned]
    print("Other config files found in %s: %d\n" % (", ".join(roots), len(scanned)))

    done, used, unmatched = 0, {}, []
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for g in groups:
        merged, parts = {}, []
        try:
            for c in g["configs"]:
                cfg, had = load(c)
                parts.append((c, cfg, had))
                merged = deep_merge(merged, cfg) if isinstance(cfg, dict) else merged
        except Exception as e:
            print("  ! cannot read %s: %s" % (", ".join(g["configs"]), e)); continue
        forced_key = next((forced[c] for c in g["configs"] if c in forced), None)
        bot, how = match(merged, forced_key)
        # the file to edit: the last one that defines api_server (the bot's own file), else the last one
        target = next((c for c, cfg, _ in reversed(parts) if isinstance(cfg, dict) and "api_server" in cfg), parts[-1][0])
        tcfg, thad = next((cfg, had) for c, cfg, had in parts if c == target)
        if a.verbose:
            print("  [%s] %s  %s" % (g["where"], target, describe(merged)))
        if bot is None:
            if (merged.get("api_server") or {}).get("listen_port") or merged.get("bot_name"):
                unmatched.append("%s  (%s)" % (target, describe(merged)))
            continue
        if bot["id"] in used:
            if used[bot["id"]] != target:
                print("  ! %s also matches bot '%s' (already %s) - skipped" % (target, bot["name"], used[bot["id"]]))
            continue
        used[bot["id"]] = target
        current = (merged.get("webhook") or {}).get("url")
        state = "already set" if current == bot["webhook_url"] else ("replace" if current else "add")
        print("  %-11s %s\n              -> bot '%s' (matched by %s, %s)" % (state.upper(), target, bot["name"], how, g["where"]))
        if state == "already set" or not a.apply:
            continue
        if a.separate:
            out = os.path.join(os.path.dirname(target), "webhook-%s.json" % re.sub(r"[^A-Za-z0-9_-]+", "_", bot["name"]))
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"webhook": block(bot)}, f, indent=4, ensure_ascii=False)
            print("              wrote %s\n              add to the bot's start command:  --config %s" % (out, out))
        else:
            backup = "%s.bak-%s" % (target, stamp)
            shutil.copy2(target, backup)
            tcfg["webhook"] = block(bot)
            st = os.stat(target)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(tcfg, f, indent=4, ensure_ascii=False)
                f.write("\n")
            try:
                os.chown(target, st.st_uid, st.st_gid)
            except OSError:
                pass
            print("              written (backup: %s)%s" % (backup, "  [comments in the file were removed]" if thad else ""))
        done += 1

    here = [b for b in BOTS if b["api_host"] in LOCAL]
    print("\nMatched %d bots." % len(used))
    for b in BOTS:
        if b["id"] in used:
            continue
        where = "this server" if b in here else ("server %s" % b["api_host"] if b["api_host"] else "unknown server")
        print("  - not found: '%s' (api %s:%s, %s)%s" % (b["name"], b["api_host"] or "?", b["api_port"] or "?", where,
              "" if b in here or not b["api_host"] else " -> run this script on that server too"))
    for p in unmatched:
        print("  ? config without a matching bot: %s" % p)
    if not a.apply:
        print("\nDry run - nothing was changed. Run again with --apply to write.  (-v shows details)")
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
