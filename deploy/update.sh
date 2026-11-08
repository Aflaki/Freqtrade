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
sleep 3
curl -fsS http://127.0.0.1:8000/health && echo && echo "Updated to $(runuser -u relay -- git -C "$APP_DIR" log -1 --format='%h %s')"
