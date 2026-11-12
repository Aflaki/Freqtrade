#!/usr/bin/env bash
# Collect what's needed to explain differences between the bots and a portfolio (no keys, no secrets).
#   bash /opt/signal-relay/deploy/diag.sh [account id] [hours]     -> /root/relay-diag.txt
set -uo pipefail
APP_DIR=/opt/signal-relay
ACC="${1:-1}"; HOURS="${2:-36}"
OUT=/root/relay-diag.txt
{
echo "== Signal Relay diagnostics: account $ACC, last $HOURS h, $(date -u '+%F %T') UTC"
echo "== version: $(git -C "$APP_DIR" log -1 --format='%h %ci %s' 2>/dev/null)"
echo; echo "== service restarts / crashes"
journalctl -u signal-relay --since "-${HOURS}h" --no-pager -o short-iso 2>/dev/null \
  | grep -Ei "Started|Stopped|Stopping|Main process exited|Killed|out of memory|Traceback" | tail -40
echo; echo "== app warnings/errors (last 300)"
journalctl -u signal-relay --since "-${HOURS}h" --no-pager -o short-iso -p warning 2>/dev/null | tail -300
echo; echo "== app log lines about rate limits, streams, bans, timeouts"
journalctl -u signal-relay --since "-${HOURS}h" --no-pager -o short-iso 2>/dev/null \
  | grep -Ei "429|418|banned|rate|timeout|stream|reconnect|insufficient|margin|failed" | tail -150
echo; echo "== memory now"; free -h
} > "$OUT" 2>&1

( cd "$APP_DIR" && runuser -u relay -- env DATA_DIR="$APP_DIR/data" "$APP_DIR/venv/bin/python" - "$ACC" "$HOURS" <<'PY'
import sqlite3, sys, json, os
acc, hours = int(sys.argv[1]), int(sys.argv[2])
db = sqlite3.connect(os.path.join(os.environ["DATA_DIR"], "relay.db"))
db.row_factory = sqlite3.Row
since = f"datetime('now','-{hours} hours')"
def q(title, sql, *a):
    print(f"\n== {title}")
    try:
        rows = db.execute(sql, a).fetchall()
    except Exception as e:
        print("  (query failed:", e, ")"); return
    if not rows:
        print("  (none)"); return
    print("  " + " | ".join(rows[0].keys()))
    for r in rows:
        print("  " + " | ".join("" if v is None else str(v)[:220] for v in r))
a = db.execute("select * from exchange_accounts where id=?", (acc,)).fetchone()
print("\n== account")
print("  ", {k: (str(a[k])[:300] if a[k] is not None else None) for k in a.keys() if not k.endswith(("_enc", "_hash"))} if a else "not found")
q("bots", "select id,name,enabled,copy_enabled,api_url from bots order by id")
q("webhooks per bot/type/status", f"select bot_id,msg_type,status,count(*) n,min(received_at) first,max(received_at) last "
  f"from webhook_events where received_at>={since} group by 1,2,3 order by 1,2,3")
q("gaps > 20 min between webhooks per bot", f"""select bot_id, prev, received_at, round((julianday(received_at)-julianday(prev))*1440) minutes from (
  select bot_id, received_at, lag(received_at) over (partition by bot_id order by received_at) prev
  from webhook_events where received_at>={since}) where prev is not null and (julianday(received_at)-julianday(prev))*1440 > 20
  order by received_at""")
q("rejected / errored webhooks", f"select id,received_at,bot_id,msg_type,action,pair,status,error,summary from webhook_events "
  f"where received_at>={since} and (status not in ('accepted','duplicate','ignored') or error!='') order by id")
q("results for this account (count per action/status)", f"select action,status,count(*) n from execution_logs "
  f"where account_id=? and created_at>={since} group by 1,2 order by 3 desc", acc)
q("not-ok results for this account", f"select created_at,action,pair,status,message,duration_ms from execution_logs "
  f"where account_id=? and created_at>={since} and status not in ('opened','closed','partial','adopted','dca','ok') "
  f"order by id", acc)
q("slow executions (> 10 s)", f"select created_at,action,pair,status,duration_ms,message from execution_logs "
  f"where account_id=? and created_at>={since} and duration_ms>10000 order by id", acc)
q("trades of this account opened/closed in the window", f"select id,bot_id,ft_trade_id,pair,side,status,adopted,opened_at,closed_at,exit_reason,round(realized_pnl,2) pnl "
  f"from trades where account_id=? and (opened_at>={since} or closed_at>={since}) order by id", acc)
q("open trades of this account", "select id,bot_id,ft_trade_id,pair,side,adopted,opened_at from trades where account_id=? and status='open' order by pair", acc)
q("bots' trades in the window (from their API/webhooks)", f"select bot_id,ft_trade_id,pair,side,is_open,open_date,close_date,exit_reason from bot_trades "
  f"where open_date>={since} or close_date>={since} order by bot_id,open_date")
q("watchdog events", f"select created_at,host,bot_id,port,action,detail from watchdog_events where created_at>={since} order by id")
r = db.execute("select recon_summary, recon_ack from exchange_accounts where id=?", (acc,)).fetchone()
print("\n== last reconciliation summary:", r["recon_summary"] if r else None)
PY
) >> "$OUT" 2>&1
sed -i -E 's/(api[_-]?key|secret|password|token)([^|]{0,3})[A-Za-z0-9_\-]{16,}/\1\2***/Ig' "$OUT"
echo "Written: $OUT ($(wc -l < "$OUT") lines)"
