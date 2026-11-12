#!/usr/bin/env bash
# What the exchange really answered for some pairs, and what it has on record for them (read-only, no secrets).
#   bash /opt/signal-relay/deploy/diag_orders.sh <account id> <hours> PAIR [PAIR...]   -> /root/relay-orders.txt
set -uo pipefail
APP_DIR=/opt/signal-relay
OUT=/root/relay-orders.txt
( cd "$APP_DIR" && runuser -u relay -- env DATA_DIR="$APP_DIR/data" RELAY_STREAMS=0 "$APP_DIR/venv/bin/python" - "$@" <<'PY'
import asyncio, datetime as dt, json, sys
from sqlalchemy import select
from app.db import ExchangeAccount, ExecutionLog, Order, SessionLocal, Trade
acc_id, hours, pairs = int(sys.argv[1]), int(sys.argv[2]), [p.upper() if "/" in p else p.upper() + "/USDT" for p in sys.argv[3:]]
since = dt.datetime.utcnow() - dt.timedelta(hours=hours)
print(f"== account {acc_id}, since {since:%F %T} UTC, pairs {pairs}")
with SessionLocal() as s:
    for pair in pairs:
        print(f"\n######## {pair}")
        for t in s.scalars(select(Trade).where(Trade.account_id == acc_id, Trade.pair == pair).order_by(Trade.id)):
            if t.status == "open" or (t.closed_at and t.closed_at >= since) or t.opened_at >= since:
                print(f"  trade {t.id}: {t.side} {t.status} adopted={t.adopted} amount={t.amount} closed_amount={t.closed_amount} "
                      f"entry={t.entry_price} opened={t.opened_at} closed={t.closed_at} reason={t.exit_reason}")
                for o in s.scalars(select(Order).where(Order.trade_id == t.id).order_by(Order.id)):
                    print(f"     order {o.kind} {o.side} id={o.exchange_order_id} amount={o.amount} filled={o.filled} "
                          f"price={o.price} status={o.status} at={o.created_at}")
        for e in s.scalars(select(ExecutionLog).where(ExecutionLog.account_id == acc_id, ExecutionLog.pair == pair,
                                                      ExecutionLog.created_at >= since).order_by(ExecutionLog.id)):
            print(f"  -- result {e.created_at:%F %T} {e.action} -> {e.status}: {e.message[:200]}")
            for c in e.calls or []:
                if str(c.get("method", "")).startswith(("create_order", "fetch_order", "fetch_my_trades")):
                    print("     ", json.dumps(c, default=str)[:1500])
    acc = s.get(ExchangeAccount, acc_id)
    s.expunge(acc)


async def exchange_side():
    from app.engine.exchange import pool
    c = await pool.get(acc)
    ex = c.ex
    print("\n== exchange side")
    try:
        print("  position mode (dualSidePosition = hedge mode):", await ex.fapiPrivateGetPositionSideDual())
    except Exception as e:
        print("  position mode: ", type(e).__name__, str(e)[:200])
    ours = set()
    with SessionLocal() as s:
        ours = {o for o in s.scalars(select(Order.exchange_order_id)) if o}
    pos = {p["symbol"]: p for p in await ex.fetch_positions()}
    for pair in pairs:
        sym = pair + ":USDT"
        p = pos.get(sym) or {}
        print(f"\n  {pair}: position now: {p.get('side')} {p.get('contracts')} entry={p.get('entryPrice')}")
        try:
            trades = await ex.fetch_my_trades(sym, int(since.timestamp() * 1000), 100)
        except Exception as e:
            print("    fills:", type(e).__name__, str(e)[:200]); continue
        for t in trades:
            info = t.get("info") or {}
            print(f"    fill {t['datetime']} {t['side']} {t['amount']} @ {t['price']} order={t.get('order')} "
                  f"{'OURS' if str(t.get('order')) in ours else 'NOT FROM THIS SITE'} positionSide={info.get('positionSide')} "
                  f"realizedPnl={info.get('realizedPnl')}")
        try:
            orders = await ex.fetch_orders(sym, int(since.timestamp() * 1000), 50)
            for o in orders:
                print(f"    order {o['datetime']} {o['side']} {o['type']} amount={o['amount']} filled={o['filled']} "
                      f"status={o['status']} ({(o.get('info') or {}).get('status')}) reduceOnly={o.get('reduceOnly')} id={o['id']} "
                      f"{'OURS' if str(o['id']) in ours else 'NOT FROM THIS SITE'} clientId={o.get('clientOrderId')}")
        except Exception as e:
            print("    orders:", type(e).__name__, str(e)[:200])
    await pool.close_all()

asyncio.run(exchange_side())
PY
) > "$OUT" 2>&1
echo "Written: $OUT ($(wc -l < "$OUT") lines)"
