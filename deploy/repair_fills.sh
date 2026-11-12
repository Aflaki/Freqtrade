#!/usr/bin/env bash
# Recompute a portfolio's recorded trades from the exchange's own fills (exact price, amount, fee).
#   bash /opt/signal-relay/deploy/repair_fills.sh <portfolio id>           -> shows old vs new, changes nothing
#   bash /opt/signal-relay/deploy/repair_fills.sh <portfolio id> apply     -> saves the corrected numbers
set -uo pipefail
APP_DIR=/opt/signal-relay
[[ $# -ge 1 ]] || { echo "usage: $0 <portfolio id> [apply]"; exit 1; }
cd "$APP_DIR" && runuser -u relay -- env DATA_DIR="$APP_DIR/data" RELAY_STREAMS=0 "$APP_DIR/venv/bin/python" - "$@" <<'PY'
import asyncio, sys
from app.engine.repair import from_fills
acc, apply = int(sys.argv[1]), len(sys.argv) > 2 and sys.argv[2] == "apply"
rows = asyncio.run(from_fills(acc, apply=apply))
f = lambda v: "-" if v is None else f"{v:.6g}"
changed = 0
for r in rows:
    o, n = r["old"], r["new"]
    diff = abs((o["pnl"] or 0) - (n["pnl"] or 0)) > 0.005 or abs((o["entry"] or 0) - n["entry"]) > 1e-9 * max(1, n["entry"])
    changed += diff
    print(f"{'*' if diff else ' '} #{r['id']} {r['pair']:<16} {r['status']:<6} entry {f(o['entry'])} -> {f(n['entry'])} | "
          f"exit {f(o['exit'])} -> {f(n['exit'])} | fees {f(o['fees'])} -> {f(n['fees'])} | result {f(o['pnl'])} -> {f(n['pnl'])}"
          + (f"  ({r['missing']} order(s) not found on the exchange: not changed)" if r["missing"] else ""))
print(f"\n{len(rows)} trades read, {changed} differ. " + ("Saved." if apply else "Nothing changed yet: add 'apply' to save."))
PY
