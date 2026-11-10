#!/usr/bin/env bash
# =============================================================================
#  Signal Relay — remove the site from this server (after moving it elsewhere).
#
#      bash uninstall.sh                  # asks for confirmation, makes a last backup in /root first
#      bash uninstall.sh --no-backup      # skip the last backup
#      bash uninstall.sh --purge-backups  # also delete /var/backups/signal-relay
#
#  NOT touched: your Freqtrade bots, their tmux sessions, their configs, and the relay-watchdog agent
#  (re-run the watchdog install command from the NEW site's panel so it reports there).
# =============================================================================
set -euo pipefail
APP_DIR=/opt/signal-relay
APP_USER=relay
BACKUP=1
PURGE=0

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-backup) BACKUP=0; shift ;;
    --purge-backups) PURGE=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ $EUID -eq 0 ]] || die "Run as root: bash $0"
[[ -d "$APP_DIR" || -f /etc/systemd/system/signal-relay.service ]] || die "Signal Relay is not installed here"

SITE="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("select base_url from global_settings").fetchone()[0])' "$APP_DIR/data/relay.db" 2>/dev/null || true)"
cat <<PLAN

This removes Signal Relay ${SITE:+($SITE) }from $(hostname):
  - the service signal-relay and its settings (/etc/signal-relay.env)
  - the program and its data: $APP_DIR
  - the daily backup job, the system user '$APP_USER'
  - the site from the web server Caddy
$( [[ $BACKUP == 1 ]] && echo "  A last backup is written to /root first." || echo "  NO backup is made." )
$( [[ $PURGE == 1 ]] && echo "  /var/backups/signal-relay is deleted too." || echo "  /var/backups/signal-relay is kept." )
Not touched: Freqtrade bots, tmux sessions, bot configs, relay-watchdog.
PLAN
read -rp "Type DELETE to continue: " ANSWER
[[ "$ANSWER" == "DELETE" ]] || die "cancelled"

if [[ $BACKUP == 1 && -f "$APP_DIR/data/relay.db" ]]; then
  say "Last backup"
  bash "$APP_DIR/deploy/backup.sh" --stop --out /root | grep -E "✔|backup:" || die "backup failed — nothing was removed"
fi

say "Stopping and removing the service"
systemctl stop signal-relay 2>/dev/null || true
systemctl disable -q signal-relay 2>/dev/null || true
rm -f /etc/systemd/system/signal-relay.service /etc/signal-relay.env /etc/cron.daily/signal-relay-backup
systemctl daemon-reload
ok "service removed"

say "Removing the site from Caddy"
if [[ -f /etc/caddy/Caddyfile.orig ]]; then
  mv -f /etc/caddy/Caddyfile.orig /etc/caddy/Caddyfile
  systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || true
  ok "previous Caddy configuration restored"
elif [[ -f /etc/caddy/Caddyfile ]] && grep -q "reverse_proxy 127.0.0.1:8000" /etc/caddy/Caddyfile; then
  # the file was written by our installer and serves only this site
  printf '# Signal Relay was removed from this server.\n' > /etc/caddy/Caddyfile
  systemctl stop caddy 2>/dev/null || true
  systemctl disable -q caddy 2>/dev/null || true
  ok "Caddy stopped (it served only this site). Remove it completely with: apt purge caddy"
fi

say "Removing the program and its data"
[[ "$APP_DIR" == "/opt/signal-relay" ]] && rm -rf -- "$APP_DIR"
id -u "$APP_USER" >/dev/null 2>&1 && userdel "$APP_USER" 2>/dev/null || true
if [[ $PURGE == 1 ]]; then rm -rf -- /var/backups/signal-relay; fi
ok "removed"

cat <<DONE

Signal Relay is removed from this server.
$( [[ $BACKUP == 1 ]] && echo "Last backup: $(ls -1t /root/signal-relay-backup-*.tar.gz 2>/dev/null | head -1)" )
Your Freqtrade bots keep running. Make sure their webhooks and the watchdog point to the NEW site.
DONE
