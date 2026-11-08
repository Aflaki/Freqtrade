#!/usr/bin/env bash
# =============================================================================
#  Signal Relay — one-command installer for Debian 12+ (also Ubuntu 22.04+)
#
#  With your own domain (its DNS A record must point to this server):
#      bash install.sh panel.example.com
#  Without a domain — a free hostname <ip>.sslip.io is used automatically:
#      bash install.sh
#  A real HTTPS certificate is obtained automatically in both cases.
#  Ports 80 and 443 must be free and open.
#
#  Safe to run again: it updates the code and keeps your data.
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Aflaki/Freqtrade.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/signal-relay
APP_USER=relay
PORT=8000
DOMAIN="${1:-}"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (log in as root, or: su -  then  bash install.sh $DOMAIN)"
[[ -r /etc/os-release ]] && . /etc/os-release
say "System: ${PRETTY_NAME:-unknown}"

# ----------------------------------------------------------------- packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl sqlite3 ca-certificates iproute2 >/dev/null
if ! command -v caddy >/dev/null; then
  if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11+ is required (you have $(python3 -V)). Use Debian 12 or newer."
ok "packages ready ($(python3 -V), caddy $(caddy version | cut -d' ' -f1))"

# ----------------------------------------------------------------- address & ports
PUBLIC_IP="$(curl -fsS4 --max-time 8 https://api.ipify.org || hostname -I | awk '{print $1}')"
DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"
if [[ "$DOMAIN" == *:* ]]; then
  die "Give only a domain name, without a port (you gave '$DOMAIN').
   HTTPS needs the standard ports 80/443. Run:  bash install.sh   (no argument)  or  bash install.sh your-domain.com"
fi
if [[ -z "$DOMAIN" || "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  DOMAIN="${PUBLIC_IP//./-}.sslip.io"
  say "No domain given — using the free hostname $DOMAIN (points to $PUBLIC_IP)"
else
  RESOLVED="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
  [[ "$RESOLVED" == "$PUBLIC_IP" ]] || die "$DOMAIN points to '${RESOLVED:-nothing}', not to this server ($PUBLIC_IP).
   Add a DNS A record: $DOMAIN -> $PUBLIC_IP, wait a few minutes and run again."
fi
BUSY="$(ss -Htlnp 'sport = :80 or sport = :443' 2>/dev/null | grep -v '"caddy"' || true)"
if [[ -n "$BUSY" ]]; then
  die "Ports 80/443 are already used by another program:
$BUSY
   Stop it (or tell the developer which program it is) and run again."
fi
ok "address: https://$DOMAIN"

# ----------------------------------------------------------------- user & code
say "Getting the code into $APP_DIR"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
if [[ -d "$APP_DIR/.git" ]]; then
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  runuser -u "$APP_USER" -- git -C "$APP_DIR" fetch -q origin "$BRANCH"
  runuser -u "$APP_USER" -- git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  [[ -e "$APP_DIR" && -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]] && die "$APP_DIR exists and is not empty"
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$APP_DIR/data"
ok "code at $(runuser -u "$APP_USER" -- git -C "$APP_DIR" log -1 --format='%h %s')"

say "Installing Python dependencies (takes a minute)"
runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/venv"
runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install -q --upgrade pip
runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "dependencies installed"

# ----------------------------------------------------------------- config
URL="https://$DOMAIN"; SECURE=true
cat > /etc/signal-relay.env <<ENV
DATA_DIR=$APP_DIR/data
SECURE_COOKIES=$SECURE
LOG_LEVEL=INFO
ENV
chmod 640 /etc/signal-relay.env

say "Preparing the database"
( cd "$APP_DIR" && runuser -u "$APP_USER" -- env DATA_DIR="$APP_DIR/data" "$APP_DIR/venv/bin/python" - "$URL" <<'PY'
import sys
from app.db import SessionLocal, get_global, init_db
init_db()
with SessionLocal() as s:
    gs = get_global(s)
    gs.base_url = sys.argv[1]
    s.commit()
print("base URL:", sys.argv[1])
PY
)
chmod 600 "$APP_DIR/data/secrets.json"

# ----------------------------------------------------------------- admin account
if [[ -t 0 ]]; then
  HAS_ADMIN="$(sqlite3 "$APP_DIR/data/relay.db" 'select count(*) from users where is_admin=1;' 2>/dev/null || echo 0)"
  if [[ "$HAS_ADMIN" == "0" ]]; then
    say "Create the admin account"
    read -rp "  Admin email: " ADMIN_EMAIL
    while true; do
      read -rsp "  Admin password (min 8 chars): " ADMIN_PW; echo
      [[ ${#ADMIN_PW} -ge 8 ]] && break; echo "  too short"
    done
    ( cd "$APP_DIR" && runuser -u "$APP_USER" -- env DATA_DIR="$APP_DIR/data" "$APP_DIR/venv/bin/python" -m app.cli create-admin "$ADMIN_EMAIL" "$ADMIN_PW" )
  fi
fi

# ----------------------------------------------------------------- service
say "Creating the system service"
cat > /etc/systemd/system/signal-relay.service <<UNIT
[Unit]
Description=Signal Relay (Freqtrade copy-trading panel)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/signal-relay.env
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --workers 1 --proxy-headers
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q signal-relay
systemctl restart signal-relay

# ----------------------------------------------------------------- web server (HTTPS)
say "Configuring Caddy (web server + HTTPS certificate)"
[[ -f /etc/caddy/Caddyfile && ! -f /etc/caddy/Caddyfile.orig ]] && cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
CADDY
systemctl enable -q caddy
systemctl restart caddy

# ----------------------------------------------------------------- daily backup
cat > /etc/cron.daily/signal-relay-backup <<'CRON'
#!/bin/sh
# keeps 14 daily copies of the database and the encryption keys
set -e
D=/var/backups/signal-relay; mkdir -p "$D"; chmod 700 "$D"
T=$(date +%F)
sqlite3 /opt/signal-relay/data/relay.db ".backup '$D/relay-$T.db'"
cp /opt/signal-relay/data/secrets.json "$D/secrets-$T.json"
find "$D" -type f -mtime +14 -delete
CRON
chmod 755 /etc/cron.daily/signal-relay-backup

# ----------------------------------------------------------------- check
say "Checking"
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null || { journalctl -u signal-relay -n 40 --no-pager; die "service did not start"; }
ok "service is running"
for _ in $(seq 1 30); do   # wait for the certificate
  curl -fsS --max-time 5 "$URL/health" >/dev/null 2>&1 && { ok "HTTPS works: $URL"; break; }
  sleep 2
done
curl -fsS --max-time 5 "$URL/health" >/dev/null 2>&1 || printf '\033[1;33m! HTTPS not ready yet. Check that ports 80 and 443 are open in your provider firewall, then: journalctl -u caddy -n 50\033[0m\n'

cat <<DONE

=====================================================================
  Installed.  Open:  $URL
=====================================================================
  Next: log in → Admin → Settings → Telegram bot token, pairs, formula.
  Webhook URL for Freqtrade is shown on the same page.

  Useful commands:
    systemctl status signal-relay         # is it running?
    journalctl -u signal-relay -f         # live logs
    systemctl restart signal-relay        # restart
    bash $APP_DIR/deploy/update.sh   # update to the latest version

  IMPORTANT: keep a copy of $APP_DIR/data/secrets.json somewhere safe.
  Daily backups go to /var/backups/signal-relay
DONE
