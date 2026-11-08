"""Public track record of the Freqtrade bot, per pair, for spot (long only, 1x) or futures (long+short, 1x)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy import select

from ..db import BotTrade, SessionLocal, utcnow

log = logging.getLogger("relay.performance")


def _dt(v: Any) -> dt.datetime | None:
    if not v or v == "None":
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e11 else v, dt.UTC).replace(tzinfo=None)
    s = str(v).replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d.astimezone(dt.UTC).replace(tzinfo=None) if d.tzinfo else d


def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ ingestion
def record_from_webhook(payload: dict, bot_id: int | None) -> None:
    """Every Freqtrade entry/exit message updates the bot's own trade record (independent of users)."""
    msg_type = str(payload.get("type", "")).lower()
    tid = _f(payload.get("trade_id"))
    if tid is None or msg_type not in ("entry", "entry_fill", "exit_fill"):
        return
    pair = str(payload.get("pair", "")).split(":")[0].upper()
    side = "short" if str(payload.get("direction", "")).lower() == "short" else "long"
    with SessionLocal() as s:
        bt = s.scalars(select(BotTrade).where(BotTrade.bot_id == bot_id, BotTrade.ft_trade_id == int(tid))).first()
        if bt is None:
            if msg_type == "exit_fill":
                return
            bt = BotTrade(bot_id=bot_id, ft_trade_id=int(tid), pair=pair, side=side)
            s.add(bt)
        bt.leverage = _f(payload.get("leverage")) or bt.leverage or 1.0
        bt.open_rate = _f(payload.get("open_rate")) or bt.open_rate
        bt.open_date = _dt(payload.get("open_date")) or bt.open_date or utcnow()
        bt.enter_tag = str(payload.get("enter_tag") or bt.enter_tag or "")[:80].replace("None", "")
        final = str(payload.get("is_final_exit", "")).lower() in ("true", "1")
        if msg_type == "exit_fill" and final:
            bt.is_open = False
            bt.close_date = _dt(payload.get("close_date")) or utcnow()
            bt.close_rate = _f(payload.get("close_rate")) or _f(payload.get("order_rate"))
            ratio = _f(payload.get("final_profit_ratio"))
            bt.profit_ratio = ratio if ratio is not None else _f(payload.get("profit_ratio"))
            bt.exit_reason = str(payload.get("exit_reason") or "")[:80]
        s.commit()


def _upsert(s, rows: list[dict], source: str, bot_id: int) -> int:
    n = 0
    for r in rows:
        if r.get("trade_id") is None:
            continue
        bt = s.scalars(select(BotTrade).where(BotTrade.bot_id == bot_id,
                                              BotTrade.ft_trade_id == int(r["trade_id"]))).first()
        if bt is None:
            bt = BotTrade(bot_id=bot_id, ft_trade_id=int(r["trade_id"]), pair="", side="long")
            s.add(bt)
        bt.pair = str(r["pair"]).split(":")[0].upper()
        bt.side = "short" if r.get("is_short") in (True, 1, "1", "true") else "long"
        bt.leverage = _f(r.get("leverage")) or 1.0
        bt.open_date = _dt(r.get("open_date")) or utcnow()
        bt.close_date = _dt(r.get("close_date"))
        bt.open_rate = _f(r.get("open_rate"))
        bt.close_rate = _f(r.get("close_rate"))
        bt.is_open = r.get("is_open") in (True, 1, "1", "true")
        bt.profit_ratio = _f(r.get("profit_ratio") if r.get("profit_ratio") is not None else r.get("close_profit"))
        bt.exit_reason = str(r.get("exit_reason") or "")[:80]
        bt.enter_tag = str(r.get("enter_tag") or "")[:80]
        bt.source = source
        n += 1
    s.commit()
    return n


async def import_from_api(bot_id: int, url: str, user: str, password: str) -> int:
    """Freqtrade REST API: GET /api/v1/trades (paginated)."""
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=30, auth=(user, password)) as c:
        offset = 0
        while True:
            r = await c.get(url.rstrip("/") + "/api/v1/trades", params={"limit": 500, "offset": offset})
            r.raise_for_status()
            batch = r.json().get("trades", [])
            rows += batch
            if len(batch) < 500:
                break
            offset += 500
    with SessionLocal() as s:
        return _upsert(s, rows, "api", bot_id)


def import_from_sqlite(bot_id: int, path: str) -> int:
    """Read Freqtrade's own tradesv3.sqlite (read-only)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute("SELECT id AS trade_id, pair, is_short, leverage, open_date, close_date, open_rate, "
                          "close_rate, is_open, close_profit, exit_reason, enter_tag FROM trades")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()
    with SessionLocal() as s:
        return _upsert(s, rows, "sqlite", bot_id)


# ------------------------------------------------------------------ statistics
def _closed(market: str, days: int | None, bots: list[int] | None = None) -> list[BotTrade]:
    with SessionLocal() as s:
        q = select(BotTrade).where(BotTrade.is_open.is_(False), BotTrade.profit_ratio.is_not(None))
        if bots is not None:
            q = q.where(BotTrade.bot_id.in_(bots or [-1]))
        if days:
            q = q.where(BotTrade.close_date >= utcnow() - dt.timedelta(days=days))
        if market == "spot":
            q = q.where(BotTrade.side == "long")
        return list(s.scalars(q.order_by(BotTrade.close_date)))


def _stats(trades: list[BotTrade]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0, "total": 0, "avg": 0, "best": 0, "worst": 0,
                "max_dd": 0, "avg_hours": 0, "last": None}
    r = [t.ratio_1x for t in trades]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in r:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    hours = [((t.close_date - t.open_date).total_seconds() / 3600) for t in trades if t.close_date and t.open_date]
    wins = sum(1 for x in r if x > 0)
    return {"trades": len(r), "wins": wins, "win_rate": wins / len(r) * 100, "total": sum(r) * 100,
            "avg": sum(r) / len(r) * 100, "best": max(r) * 100, "worst": min(r) * 100, "max_dd": dd * 100,
            "avg_hours": sum(hours) / len(hours) if hours else 0, "last": trades[-1].close_date}


def pair_stats(market: str = "futures", days: int | None = None, bots: list[int] | None = None) -> dict[str, dict]:
    by_pair: dict[str, list[BotTrade]] = defaultdict(list)
    for t in _closed(market, days, bots):
        by_pair[t.pair].append(t)
    return {p: _stats(ts) for p, ts in sorted(by_pair.items())}


def summary(market: str = "futures", days: int | None = None, pairs: list[str] | None = None,
            bots: list[int] | None = None) -> dict:
    trades = [t for t in _closed(market, days, bots) if not pairs or t.pair in pairs]
    st = _stats(trades)
    # equity curve (sum of 1x returns, %), downsampled for the SVG
    pts, eq = [], 0.0
    for t in trades:
        eq += t.ratio_1x * 100
        pts.append((t.close_date, eq))
    if len(pts) > 200:
        step = len(pts) / 200
        pts = [pts[int(i * step)] for i in range(200)] + [pts[-1]]
    st["curve"] = svg_path(pts)
    st["recent"] = list(reversed(trades[-15:]))
    return st


def svg_path(points: list[tuple[Any, float]], w: int = 600, h: int = 160) -> dict:
    if len(points) < 2:
        return {"line": "", "area": "", "zero": h / 2, "min": 0, "max": 0}
    ys = [y for _, y in points]
    lo, hi = min(0.0, min(ys)), max(0.0, max(ys))
    span = (hi - lo) or 1.0
    xy = [(i / (len(points) - 1) * w, h - (y - lo) / span * h) for i, (_, y) in enumerate(points)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    zero = h - (0 - lo) / span * h
    return {"line": line, "area": f"{line} L{w},{zero:.1f} L0,{zero:.1f} Z", "zero": zero, "min": lo, "max": hi}


def bot_summaries(bot_ids: list[int], market: str = "futures", days: int | None = 90) -> dict[int, dict]:
    """Headline numbers per bot (cards on the home page and in the portfolio form)."""
    by_bot: dict[int, list[BotTrade]] = defaultdict(list)
    for t in _closed(market, days, bot_ids):
        by_bot[t.bot_id].append(t)
    return {b: {k: v for k, v in _stats(by_bot.get(b, [])).items() if k != "last"} for b in bot_ids}


def stats_json(bot_ids: list[int], days: int | None = 90) -> str:
    """Per-bot, per-market, per-pair stats for the portfolio builder (combined in the browser)."""
    out: dict[str, Any] = {}
    for b in bot_ids:
        out[str(b)] = {}
        for m in ("spot", "futures"):
            by_pair: dict[str, list[BotTrade]] = defaultdict(list)
            for t in _closed(m, days, [b]):
                by_pair[t.pair].append(t)
            out[str(b)][m] = {p: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in _stats(ts).items()
                                  if k != "last"} for p, ts in by_pair.items()}
    return json.dumps(out)
