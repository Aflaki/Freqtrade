#!/usr/bin/env bash
# =============================================================================
#  Signal Relay — full backup in ONE file (database + encryption keys + icon).
#  Also the first step of moving to another server.
#
#      bash backup.sh                 # backup while running  -> /root/signal-relay-backup-<date>.tar.gz
#      bash backup.sh --stop          # final copy before a move: stops the site first (no trade gets lost)
#      bash backup.sh --out /some/dir
#
#  The file contains secrets.json: whoever has it can decrypt your users' API keys. Keep it private.
# =============================================================================
set -euo pipefail
APP_DIR=/opt/signal-relay
DATA="$APP_DIR/data"
OUT=/root
STOP=0

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop) STOP=1; shift ;;
    --out) OUT="${2:?--out needs a folder}"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ $EUID -eq 0 ]] || die "Run as root: bash $0"
[[ -f "$DATA/relay.db" && -f "$DATA/secrets.json" ]] || die "No Signal Relay data in $DATA"
command -v python3 >/dev/null || die "python3 is required"
db() {  # db <file> <sql>  -> first column of the first row (python's sqlite3, no extra package needed)
  python3 - "$1" "$2" <<'PY'
import sqlite3, sys
try:
    row = sqlite3.connect(sys.argv[1]).execute(sys.argv[2]).fetchone()
    print(row[0] if row else "")
except Exception:
    print("")
PY
}
mkdir -p "$OUT"

if [[ $STOP == 1 ]]; then
  say "Stopping the site (it will not start again on reboot)"
  systemctl stop signal-relay
  systemctl disable -q signal-relay
  ok "stopped — webhooks now fail until the new server takes over"
fi

TS="$(date +%Y%m%d-%H%M)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "Copying the data"
python3 - "$DATA/relay.db" "$TMP/relay.db" <<'PY'                # consistent copy, also while the site runs
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
PY
[[ "$(db "$TMP/relay.db" 'PRAGMA integrity_check')" == "ok" ]] || die "database copy failed its integrity check"
cp -p "$DATA/secrets.json" "$TMP/"
for f in "$DATA"/*; do                                          # site icon and anything else, not the live db files
  case "$(basename "$f")" in relay.db|relay.db-wal|relay.db-shm|secrets.json) ;; *) cp -a "$f" "$TMP/" ;; esac
done
{
  echo "created=$(date -u +%FT%TZ)"
  echo "from_host=$(hostname)"
  echo "site=$(db "$TMP/relay.db" 'select base_url from global_settings limit 1')"
  echo "code=$(git -C "$APP_DIR" log -1 --format='%h %s' 2>/dev/null || true)"
  echo "users=$(db "$TMP/relay.db" 'select count(*) from users')"
} > "$TMP/MANIFEST.txt"

FILE="$OUT/signal-relay-backup-$TS.tar.gz"
umask 077
tar -czf "$FILE" -C "$TMP" .
chmod 600 "$FILE"
ok "backup: $FILE ($(du -h "$FILE" | cut -f1))"
echo "   sha256: $(sha256sum "$FILE" | cut -d' ' -f1)"
cat "$TMP/MANIFEST.txt" | sed 's/^/   /'
cat <<NEXT

To move to a new server:
   scp $FILE root@NEW_SERVER_IP:/root/
then on the new server:
   apt update && apt install -y curl
   curl -fsSLO https://raw.githubusercontent.com/Aflaki/Freqtrade/main/deploy/install.sh
   bash install.sh --restore /root/$(basename "$FILE")              # or: bash install.sh your.domain --restore ...
NEXT
