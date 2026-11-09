#!/usr/bin/env bash
# Update Signal Relay to the latest version from GitHub (keeps all data).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root: bash $0"; exit 1; }
APP_DIR=/opt/signal-relay
/etc/cron.daily/signal-relay-backup || true          # safety backup first
runuser -u relay -- git -C "$APP_DIR" fetch -q origin main
runuser -u relay -- git -C "$APP_DIR" reset -q --hard origin/main
runuser -u relay -- "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
systemctl restart signal-relay
# startup (database migration, Telegram) can take a while: wait up to 60 s for the health check
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health 2>/dev/null; then
    echo; echo "Updated to $(runuser -u relay -- git -C "$APP_DIR" log -1 --format='%h %s')"
    exit 0
  fi
  sleep 2
done
echo "The site did not answer within 60 seconds. Last log lines:"
systemctl --no-pager status signal-relay | head -5 || true
journalctl -u signal-relay -n 40 --no-pager || true
exit 1
