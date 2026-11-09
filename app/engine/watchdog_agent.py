"""The watchdog agent that runs on each bot server (installed by /watchdog/install.sh as a systemd service).

The agent source is AGENT below; the site fills in its address and token when it is downloaded.
"""
from __future__ import annotations

AGENT_VERSION = "1.0"

AGENT = r'''#!/usr/bin/env python3
"""
Signal Relay watchdog — keeps the Freqtrade bots on this server running.

Every minute it looks at the running Freqtrade bots (processes and Docker containers) and remembers how each one
was started (command line, folder, environment, user). When a bot's process disappears (killed, crashed,
out of memory, server reboot) it starts it again exactly the same way and reports to __SITE__,
which alerts the admins (Telegram, alert bot, phone push).

  python3 agent.py status          what is watched right now
  python3 agent.py pause PORT      don't restart this bot (e.g. you stop it on purpose); resume PORT undoes it
  python3 agent.py forget PORT     remove a bot you deleted for good
  python3 agent.py once            one check, printed (for testing)
Bots are identified by their API port. A bot that was never seen running is not known: start it once by hand.
Needs only Python 3.8+.
"""
import hashlib
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

SITE = "__SITE__"
TOKEN = "__TOKEN__"
VERSION = "__VERSION__"
STATE_FILE = "/var/lib/relay-watchdog/state.json"
INTERVAL = 60          # seconds between checks (the site may change it)
MISSING_CHECKS = 2     # a bot must be gone in 2 checks in a row (a manual restart is not interrupted)
MAX_RESTARTS = 3       # at most 3 restarts ...
WINDOW = 1800          # ... in 30 minutes, then give up and alert
START_GRACE = 25       # seconds a restarted bot must stay alive to count as started
CONFIG_FLAGS = ("-c", "--config")
SHELLS = {"sh", "bash", "dash", "zsh", "nohup", "setsid", "screen", "tmux", "env"}


# ---------------------------------------------------------------- Freqtrade configs (JSON with comments)
def strip_json(text):
    out, i, n, in_str = [], 0, len(text), False
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
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        out.append(c); i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except ValueError:
        return json.loads(strip_json(raw))


def deep_merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def config_args(argv, cwd):
    files, i = [], (argv.index("trade") + 1 if "trade" in argv else 0)
    while i < len(argv):
        a = argv[i]
        if a in CONFIG_FLAGS and i + 1 < len(argv):
            files.append(argv[i + 1]); i += 2; continue
        if a.startswith("--config="):
            files.append(a.split("=", 1)[1])
        i += 1
    files = files or ["config.json"]
    return [f if f.startswith("/") else os.path.normpath(os.path.join(cwd, f)) for f in files]


def api_address(argv, cwd, env):
    """(listen_ip, listen_port) of the bot's REST API, from env overrides or its config files."""
    cfg = {}
    for p in config_args(argv, cwd):
        try:
            cfg = deep_merge(cfg, load_json(p))
        except (OSError, ValueError):
            pass
    api = cfg.get("api_server") or {}
    port = env.get("FREQTRADE__API_SERVER__LISTEN_PORT") or (api.get("listen_port") if api.get("enabled", True) else None)
    ip = env.get("FREQTRADE__API_SERVER__LISTEN_IP_ADDRESS") or api.get("listen_ip_address") or "127.0.0.1"
    try:
        return ip, int(port) if port else None
    except (TypeError, ValueError):
        return ip, None


# ---------------------------------------------------------------- finding the bots
def is_trade_cmd(argv):
    joined = " ".join(argv)
    return "freqtrade" in joined and " trade" in " " + joined.split("freqtrade", 1)[-1]


def read_proc(pid):
    base = "/proc/%s/" % pid
    with open(base + "cmdline", "rb") as f:
        argv = [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
    if not argv or not is_trade_cmd(argv):
        return None
    r, me = os.stat(base + "root"), os.stat("/proc/self/root")
    if (r.st_dev, r.st_ino) != (me.st_dev, me.st_ino):
        return None  # inside a container: Docker handles it
    with open(base + "stat") as f:
        if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
            return None  # zombie = dead
    cwd = os.readlink(base + "cwd")
    with open(base + "environ", "rb") as f:
        env = dict(x.decode("utf-8", "replace").split("=", 1) for x in f.read().split(b"\0") if b"=" in x)
    uid = gid = 0
    with open(base + "status") as f:
        for line in f:
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
            elif line.startswith("Gid:"):
                gid = int(line.split()[1])
    unit = ""
    try:
        with open(base + "cgroup") as f:
            m = re.search(r"/([^/\s]+\.service)\s*$", f.read(), re.M)
        if m and not m.group(1).startswith(("relay-watchdog", "relay-ft-", "user@", "session-")):
            unit = m.group(1)
    except OSError:
        pass
    return {"argv": argv, "cwd": cwd, "env": env, "uid": uid, "gid": gid, "unit": unit}


def bot_key(port, argv, cwd):
    if port:
        return "port:%d" % port
    return "cmd:" + hashlib.sha1((cwd + "\0" + "\0".join(argv)).encode()).hexdigest()[:12]


def from_processes():
    found = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            info = read_proc(pid)
        except (OSError, ValueError):
            continue
        if not info:
            continue
        ip, port = api_address(info["argv"], info["cwd"], info["env"])
        key = bot_key(port, info["argv"], info["cwd"])
        shell = os.path.basename(info["argv"][0]) in SHELLS
        prev = found.get(key)
        if prev and (shell or not prev["_shell"]):  # keep the real python process, not its shell wrapper
            continue
        found[key] = dict(info, kind="systemd" if info["unit"] else "process", pid=int(pid), port=port, ip=ip,
                          _shell=shell)
    for v in found.values():
        v.pop("_shell", None)
    return found


def docker(*args, timeout=30):
    return subprocess.run(["docker"] + list(args), capture_output=True, text=True, timeout=timeout)


def from_docker():
    """Freqtrade containers, running or not (a stopped one is restarted only if we saw it running)."""
    if not shutil.which("docker"):
        return {}, set()
    try:
        ids = docker("ps", "-aq").stdout.split()
        infos = json.loads(docker("inspect", *ids).stdout) if ids else []
    except Exception:
        return {}, set()
    running, stopped = {}, set()
    for info in infos:
        cfg = info.get("Config") or {}
        argv = []
        for x in (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []):
            argv += shlex.split(x) if " " in x else [x]
        if "trade" not in argv or ("freqtrade" not in str(cfg.get("Image", "")) and not any("freqtrade" in a for a in argv)):
            continue
        name = (info.get("Name") or "").lstrip("/")
        port = None
        for binds in ((info.get("HostConfig") or {}).get("PortBindings") or {}).values():
            for b in binds or []:
                if str(b.get("HostPort", "")).isdigit():
                    port = int(b["HostPort"]); break
            if port:
                break
        key = "port:%d" % port if port else "docker:" + name
        if (info.get("State") or {}).get("Running"):
            running[key] = {"kind": "docker", "container": name, "port": port, "ip": "127.0.0.1",
                            "pid": (info.get("State") or {}).get("Pid")}
        else:
            stopped.add(key)
    return running, stopped


# ---------------------------------------------------------------- why did it die?
def oom_killed(pid):
    """Was this pid killed by the kernel's out-of-memory killer? (kernel log)"""
    if not pid:
        return False
    for cmd in (["journalctl", "-k", "--since", "-3h", "--no-pager", "-q"], ["dmesg"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
        except Exception:
            continue
        if re.search(r"(Killed process %d\b|oom-kill:.*pid=%d\b)" % (pid, pid), out):
            return True
    return False


def port_open(ip, port):
    if not port:
        return False
    for host in {ip if ip not in ("0.0.0.0", "::", "") else "127.0.0.1", "127.0.0.1"}:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            pass
    return False


def mem_info():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    out[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- restarting
def tail(text, n=12):
    return "\n".join([x for x in (text or "").splitlines() if x.strip()][-n:])[-1500:]


def restart(b):
    """Start the bot again the way it was started. Returns (ok, detail)."""
    kind = b.get("kind")
    if kind == "docker":
        r = docker("start", b["container"])
        if r.returncode:
            return False, tail(r.stderr or r.stdout)
        time.sleep(START_GRACE)
        st = docker("inspect", "-f", "{{.State.Running}}", b["container"]).stdout.strip()
        return (st == "true"), "" if st == "true" else tail(docker("logs", "--tail", "15", b["container"]).stderr)
    if kind == "systemd" and b.get("unit"):
        r = subprocess.run(["systemctl", "restart", b["unit"]], capture_output=True, text=True, timeout=60)
        time.sleep(START_GRACE)
        ok = subprocess.run(["systemctl", "is-active", "--quiet", b["unit"]]).returncode == 0
        if ok:
            return True, ""
        log = subprocess.run(["journalctl", "-u", b["unit"], "-n", "15", "--no-pager", "-q"],
                             capture_output=True, text=True).stdout
        return False, tail(r.stderr + "\n" + log)
    # a plain process: run the same command line again as its own transient systemd service (own cgroup + log)
    unit = "relay-ft-" + re.sub(r"[^A-Za-z0-9_.-]", "-", b["key"].split(":", 1)[-1])
    env = {k: v for k, v in (b.get("env") or {}).items() if "\n" not in v and k not in ("INVOCATION_ID", "JOURNAL_STREAM")}
    if shutil.which("systemd-run") and os.path.isdir("/run/systemd/system"):  # systemd is really running
        subprocess.run(["systemctl", "reset-failed", unit + ".service"], capture_output=True)
        cmd = ["systemd-run", "--unit", unit, "--collect", "--quiet", "--uid=%d" % b.get("uid", 0),
               "--gid=%d" % b.get("gid", 0), "--working-directory=" + b["cwd"],
               "--property=Description=Freqtrade bot (restarted by relay-watchdog)"]
        cmd += ["--setenv=%s=%s" % kv for kv in env.items()]
        r = subprocess.run(cmd + ["--"] + b["argv"], capture_output=True, text=True, timeout=60)
        if r.returncode:
            return False, tail(r.stderr or r.stdout)
        time.sleep(START_GRACE)
        if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
            return True, "journalctl -u " + unit
        log = subprocess.run(["journalctl", "-u", unit, "-n", "15", "--no-pager", "-q"], capture_output=True, text=True).stdout
        return False, tail(log)
    # no systemd: detach with a double fork through sh, output to a log file
    logf = "/var/log/relay-watchdog/%s.log" % unit
    os.makedirs(os.path.dirname(logf), exist_ok=True)

    def drop_privileges():
        if b.get("uid"):
            try:
                os.initgroups(pwd.getpwuid(b["uid"]).pw_name, b.get("gid", 0))
            except KeyError:
                os.setgroups([])
            os.setgid(b.get("gid", 0)); os.setuid(b["uid"])
    p = subprocess.Popen(["/bin/sh", "-c", 'exec "$@" >>"$RELAY_LOG" 2>&1 </dev/null & echo $!', "sh"] + b["argv"],
                         cwd=b["cwd"], env=dict(env, RELAY_LOG=logf), stdout=subprocess.PIPE, text=True,
                         preexec_fn=drop_privileges, start_new_session=True)
    pid = int((p.communicate(timeout=30)[0] or "0").strip() or 0)
    time.sleep(START_GRACE)
    alive = pid and os.path.exists("/proc/%d" % pid)
    if alive:
        return True, logf
    try:
        with open(logf, errors="replace") as f:
            return False, tail(f.read()[-6000:])
    except OSError:
        return False, "process exited"


# ---------------------------------------------------------------- state + report
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"bots": {}, "pending": []}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.chmod(tmp, 0o600)  # holds the bots' environment
    os.replace(tmp, STATE_FILE)


def host_ips():
    ips = set()
    try:
        ips |= set(subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split())
    except Exception:
        pass
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def report(st, events):
    bots = [{"key": k, "port": b.get("port"), "kind": b.get("kind"), "status": b.get("status"), "pid": b.get("pid"),
             "since": b.get("since"), "paused": bool(b.get("paused"))} for k, b in sorted(st["bots"].items())]
    body = json.dumps({"host": platform.node(), "ips": host_ips(), "version": VERSION, "mem": mem_info(),
                       "load": list(os.getloadavg()), "bots": bots, "events": st["pending"] + events}).encode()
    req = urllib.request.Request(SITE.rstrip("/") + "/watchdog/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Watchdog-Token": TOKEN,
                                          "User-Agent": "relay-watchdog/" + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ans = json.loads(r.read().decode() or "{}")
        st["pending"] = []
        return ans
    except Exception as e:
        st["pending"] = (st["pending"] + events)[-50:]  # sent with the next report
        print("report failed: %s" % e, file=sys.stderr)
        return {}


def check(st, cfg, verbose=False):
    now = int(time.time())
    events = []
    procs = from_processes()
    dock, dock_stopped = from_docker()
    procs.update(dock)
    disabled = {int(p) for p in cfg.get("disabled_ports", []) if str(p).isdigit()}
    for key, info in procs.items():
        b = st["bots"].get(key, {})
        if b.get("status") in ("down", "gave_up") and b.get("missing", 0) >= 1:
            events.append({"key": key, "port": info.get("port"), "action": "recovered", "at": now,
                           "detail": "running again (started by someone else)"})
        if b.get("pid") != info.get("pid") or "since" not in b:
            b["since"] = now  # a new process
        b.update(info, key=key, status="running", missing=0, last_seen=now)
        st["bots"][key] = b
    for key, b in st["bots"].items():
        if key in procs:
            continue
        if b.get("paused") or (b.get("port") in disabled):
            b["status"] = "paused"
            continue
        if b.get("kind") == "docker" and key not in dock_stopped:
            b["status"] = "removed"  # container deleted: nothing to start
            continue
        b["missing"] = b.get("missing", 0) + 1
        if b["missing"] == 1:
            b["status"], b["gone_at"] = "down", now
            b["cause"] = "oom" if oom_killed(b.get("pid")) else ""
            events.append({"key": key, "port": b.get("port"), "action": "gone", "at": now, "detail": b["cause"]})
            continue
        if b.get("status") == "gave_up":
            if now - max(b.get("restarts") or [0]) < WINDOW:
                continue
            b["restarts"] = []
        if b["missing"] < MISSING_CHECKS:
            continue
        if port_open(b.get("ip"), b.get("port")):
            continue  # something answers on its port: probably restarted another way, it will be found next time
        recent = [t for t in b.get("restarts", []) if now - t < WINDOW]
        if len(recent) >= MAX_RESTARTS:
            b["status"] = "gave_up"
            events.append({"key": key, "port": b.get("port"), "action": "gave_up", "at": now,
                           "detail": "%d restarts in %d min" % (len(recent), WINDOW // 60)})
            continue
        if verbose:
            print("restarting", key)
        try:
            ok, detail = restart(b)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        b["restarts"] = recent + [now]
        cause = b.get("cause", "")
        if ok:
            b.update(status="restarting", missing=0)
        events.append({"key": key, "port": b.get("port"), "action": "restarted" if ok else "restart_failed",
                       "at": int(time.time()), "detail": detail, "cause": cause,
                       "down_for": now - b.get("gone_at", now)})
    for k in [k for k, b in st["bots"].items() if b.get("status") == "removed" and now - b.get("last_seen", now) > 86400]:
        del st["bots"][k]
    return events


def main():
    args = sys.argv[1:] or ["run"]
    if os.geteuid() != 0 and args[0] in ("run", "once"):
        sys.exit("run as root (it must read /proc and start the bots as their users)")
    st = load_state()
    st.setdefault("pending", [])
    cmd = args[0]
    if cmd in ("pause", "resume", "forget"):
        if len(args) < 2:
            sys.exit("usage: agent.py %s PORT" % cmd)
        keys = [k for k in st["bots"] if k == args[1] or k == "port:" + args[1]]
        if not keys:
            sys.exit("not watched: " + args[1])
        for k in keys:
            if cmd == "forget":
                del st["bots"][k]
            else:
                st["bots"][k]["paused"] = cmd == "pause"
        save_state(st)
        print("%s: %s" % (cmd, ", ".join(keys)))
        return
    if cmd == "status":
        for k, b in sorted(st["bots"].items()):
            print("%-14s %-9s %-8s pid=%-8s %s" % (k, b.get("status"), b.get("kind"), b.get("pid"),
                                                    " ".join(b.get("argv", [])[:6]) or b.get("container", "")))
        print("%d bot(s) watched; state: %s" % (len(st["bots"]), STATE_FILE))
        return
    cfg = {}
    while True:
        events = check(st, cfg, verbose=cmd == "once")
        save_state(st)
        ans = report(st, events)
        if ans:
            cfg = ans
        save_state(st)
        if cmd == "once":
            print(json.dumps({"events": events, "site": ans}, indent=1))
            return
        time.sleep(max(20, int(cfg.get("interval") or INTERVAL)))


if __name__ == "__main__":
    main()
'''


INSTALL = r'''#!/bin/sh
# Signal Relay watchdog installer: run on every bot server as root:
#   curl -fsSL "__SITE__/watchdog/install.sh?t=__TOKEN__" | sudo sh
set -e
[ "$(id -u)" = 0 ] || { echo "Please run as root (sudo)."; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required (apt install python3)."; exit 1; }
mkdir -p /opt/relay-watchdog /var/lib/relay-watchdog
curl -fsSL "__SITE__/watchdog/agent.py?t=__TOKEN__" -o /opt/relay-watchdog/agent.py.new
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" /opt/relay-watchdog/agent.py.new
mv /opt/relay-watchdog/agent.py.new /opt/relay-watchdog/agent.py
chmod 700 /opt/relay-watchdog/agent.py
cat > /etc/systemd/system/relay-watchdog.service <<'UNIT'
[Unit]
Description=Signal Relay watchdog (restarts Freqtrade bots that died)
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/env python3 /opt/relay-watchdog/agent.py run
Restart=always
RestartSec=10
# only the watchdog itself is stopped with the service, never the bots it started
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable relay-watchdog >/dev/null 2>&1
systemctl restart relay-watchdog
sleep 3
python3 /opt/relay-watchdog/agent.py status || true
echo
echo "Watchdog installed. It reports to __SITE__ every minute."
echo "Log: journalctl -u relay-watchdog -f"
'''


def build_agent(site: str, token: str) -> str:
    return AGENT.replace("__SITE__", site.rstrip("/")).replace("__TOKEN__", token).replace("__VERSION__", AGENT_VERSION)


def build_install(site: str, token: str) -> str:
    return INSTALL.replace("__SITE__", site.rstrip("/")).replace("__TOKEN__", token)
