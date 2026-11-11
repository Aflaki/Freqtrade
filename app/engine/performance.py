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

from ..db import Bot, BotGroup, BotTrade, SessionLocal, utcnow

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
        bt.stake_amount = _f(payload.get("stake_amount")) or bt.stake_amount
        bt.open_date = _dt(payload.get("open_date")) or bt.open_date or utcnow()
        bt.enter_tag = str(payload.get("enter_tag") or bt.enter_tag or "")[:80].replace("None", "")
        final = str(payload.get("is_final_exit", "")).lower() in ("true", "1")
        if msg_type == "exit_fill" and final:
            bt.is_open = False
            bt.close_date = _dt(payload.get("close_date")) or utcnow()
            bt.close_rate = _f(payload.get("close_rate")) or _f(payload.get("order_rate"))
            ratio = _f(payload.get("final_profit_ratio"))
            bt.profit_ratio = ratio if ratio is not None else _f(payload.get("profit_ratio"))
            # total realized profit of the whole trade (incl. earlier partial exits), like FreqUI
            cum = _f(payload.get("cumulative_profit"))
            bt.profit_abs = cum if cum is not None else _f(payload.get("profit_amount"))
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
        bt.profit_ratio = _f(r.get("close_profit") if r.get("close_profit") is not None else r.get("profit_ratio"))
        abs_ = r.get("close_profit_abs") if r.get("close_profit_abs") is not None else r.get("profit_abs")
        bt.profit_abs = _f(abs_)
        bt.stake_amount = _f(r.get("stake_amount")) or bt.stake_amount
        bt.exit_reason = str(r.get("exit_reason") or "")[:80]
        bt.enter_tag = str(r.get("enter_tag") or "")[:80]
        bt.source = source
        n += 1
    s.commit()
    return n


async def sync_recent_from_api(bot_id: int, url: str, user: str, password: str, limit: int = 100) -> int:
    """Latest closed trades (newest first) -> keeps the record identical to Freqtrade's own database."""
    async with httpx.AsyncClient(timeout=20, auth=(user, password)) as c:
        r = await c.get(url.rstrip("/") + "/api/v1/trades", params={"limit": limit, "order_by_id": "false"})
        r.raise_for_status()
        rows = r.json().get("trades", [])
    with SessionLocal() as s:
        return _upsert(s, rows, "api", bot_id)


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
        if market in ("long", "spot"):
            q = q.where(BotTrade.side == "long")
        elif market == "short":
            q = q.where(BotTrade.side == "short")
        rows = list(s.scalars(q.order_by(BotTrade.close_date)))
        bots = {b.id: b for b in s.scalars(select(Bot))}
    # each bot's whitelist / blacklist hides pairs from its results (data is kept)
    return [t for t in rows if t.bot_id not in bots or bots[t.bot_id].allows(t.pair)]


def _amount(t: BotTrade) -> float:
    """Profit in stake currency: Freqtrade's close_profit_abs, else stake * ratio."""
    if t.profit_abs is not None:
        return t.profit_abs
    return (t.stake_amount or 0) * (t.profit_ratio or 0)


def bot_pair_count(b: Bot) -> int:
    """Pairs a bot trades: its Freqtrade whitelist (from the API), else the pairs it has traded; after its own filter."""
    pairs = [p for p in (b.state or {}).get("whitelist") or [] if b.allows(p)]
    if not pairs:
        with SessionLocal() as s:
            pairs = [p for p in s.scalars(select(BotTrade.pair).where(BotTrade.bot_id == b.id).distinct()) if b.allows(p)]
    return max(1, len(set(pairs)))


def capital_breakdown() -> dict[int, dict]:
    """Effective starting capital of every bot and where it comes from.

    In a group with a capital: bots with a manually entered capital keep it; the rest of the group's capital
    is shared by the other bots in proportion to how many pairs each one trades.
    Without group capital: the bot's manual capital, else Freqtrade's starting_capital.
    """
    with SessionLocal() as s:
        bots = list(s.scalars(select(Bot).where(Bot.enabled.is_(True))))
        groups = {g.id: g for g in s.scalars(select(BotGroup))}
    out: dict[int, dict] = {}
    by_group: dict[int, list[Bot]] = defaultdict(list)
    for b in bots:
        g = groups.get(b.group_id)
        if g and g.stats_capital:
            by_group[g.id].append(b)
        else:
            out[b.id] = {"capital": b.capital, "how": "manual" if b.stats_capital else ("freqtrade" if b.capital else "none")}
    for gid, members in by_group.items():
        g = groups[gid]
        own = {b.id: b.stats_capital for b in members if b.stats_capital}
        rest = [b for b in members if b.id not in own]
        remainder = max(0.0, g.stats_capital - sum(own.values()))
        counts = {b.id: bot_pair_count(b) for b in rest}
        total_pairs = sum(counts.values())
        for b in members:
            if b.id in own:
                out[b.id] = {"capital": own[b.id], "how": "manual"}
            else:
                share = remainder * counts[b.id] / total_pairs if total_pairs else 0.0
                out[b.id] = {"capital": share or None, "how": "group", "pairs": counts[b.id], "group_pairs": total_pairs,
                             "group_capital": g.stats_capital, "remainder": remainder}
    return out


def capitals(bot_ids: list[int]) -> float | None:
    """Starting capital behind a set of bots (None if any is unknown -> no % figures)."""
    ids = set(bot_ids or [])
    if not ids:
        return None
    bd = capital_breakdown()
    caps = [bd.get(i, {}).get("capital") for i in ids]
    if not caps or any(not c for c in caps):
        return None
    return sum(caps)


def _stats(trades: list[BotTrade], capital: float | None = None) -> dict:
    """Same definitions as Freqtrade/FreqUI:
    profit_abs  = sum of close_profit_abs                     (FreqUI "Profit")
    return_pct  = profit_abs / starting capital               (FreqUI "Profit %", needs capital)
    avg         = mean of close_profit per trade              (FreqUI "Avg. profit")
    max_dd_*    = largest fall of the balance from its high   (Freqtrade calculate_max_drawdown)
    """
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0, "profit_abs": 0, "sum_ratio": 0, "return_pct": None, "avg": 0,
                "best": 0, "worst": 0, "max_dd_pct": None, "max_dd_abs": 0, "avg_lev": 0, "avg_hours": 0, "last": None}
    r = [t.profit_ratio or 0.0 for t in trades]
    amounts = [_amount(t) for t in trades]
    cum = high = dd_abs = dd_rel = 0.0
    for x in amounts:
        cum += x
        high = max(high, cum)
        dd_abs = max(dd_abs, high - cum)
        if capital:
            dd_rel = max(dd_rel, (high - cum) / (capital + high))
    hours = [((t.close_date - t.open_date).total_seconds() / 3600) for t in trades if t.close_date and t.open_date]
    wins = sum(1 for x in amounts if x > 0)
    profit = sum(amounts)
    return {"trades": len(r), "wins": wins, "win_rate": wins / len(r) * 100, "profit_abs": profit,
            "sum_ratio": sum(r), "return_pct": profit / capital * 100 if capital else None,
            "avg": sum(r) / len(r) * 100, "best": max(r) * 100, "worst": min(r) * 100,
            "max_dd_pct": dd_rel * 100 if capital else None, "max_dd_abs": dd_abs,
            "avg_lev": sum(t.leverage or 1 for t in trades) / len(trades),
            "avg_hours": sum(hours) / len(hours) if hours else 0, "last": trades[-1].close_date}


def pair_stats(market: str = "all", days: int | None = None, bots: list[int] | None = None) -> dict[str, dict]:
    return pair_stats_of(_closed(market, days, bots))


def pair_stats_of(trades: list) -> dict[str, dict]:
    """Per-pair numbers of any list of closed trades (bot trades, or a user's own trades)."""
    by_pair: dict[str, list] = defaultdict(list)
    for t in trades:
        by_pair[t.pair].append(t)
    return {p: _stats(ts) for p, ts in sorted(by_pair.items())}


def summary(market: str = "all", days: int | None = None, pairs: list[str] | None = None,
            bots: list[int] | None = None) -> dict:
    trades = [t for t in _closed(market, days, bots) if not pairs or t.pair in pairs]
    ids = bots if bots is not None else sorted({t.bot_id for t in trades if t.bot_id})
    return summary_of(trades, capitals(ids), days)


def summary_of(trades: list, capital: float | None, days: int | None) -> dict:
    """Headline numbers, equity curve and market comparison of any list of closed trades (oldest first).
    Each trade needs: pair, side, leverage, open_date, close_date, profit_ratio, profit_abs, stake_amount."""
    st = _stats(trades, capital)
    st["capital"] = capital
    # equity curve: cumulative profit in stake currency, like the FreqUI chart
    st["curve_abs"] = True
    pts, eq = [], 0.0
    for t in trades:
        eq += _amount(t)
        pts.append((t.close_date, eq))
    if len(pts) > 200:
        step = len(pts) / 200
        pts = [pts[int(i * step)] for i in range(200)] + [pts[-1]]
    st["curve"] = svg_path(pts)
    st["market"] = market_chart(trades, st["capital"], days)
    st["recent"] = list(reversed(trades[-15:]))
    st["all_trades"] = list(reversed(trades))[:1000]  # newest first, for the trades tab
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


def bot_summaries(bot_ids: list[int], market: str = "all", days: int | None = 90) -> dict[int, dict]:
    """Headline numbers per bot (cards on the home page and in the portfolio form)."""
    by_bot: dict[int, list[BotTrade]] = defaultdict(list)
    for t in _closed(market, days, bot_ids):
        by_bot[t.bot_id].append(t)
    caps = {b: capitals([b]) for b in bot_ids}
    return {b: {k: v for k, v in _stats(by_bot.get(b, []), caps[b]).items() if k != "last"} for b in bot_ids}


def stats_json(bot_ids: list[int], days: int | None = 90) -> str:
    """Per-bot, per-market, per-pair stats for the portfolio builder (combined in the browser)."""
    out: dict[str, Any] = {}
    for b in bot_ids:
        out[str(b)] = {}
        for m in ("long", "all"):
            by_pair: dict[str, list[BotTrade]] = defaultdict(list)
            for t in _closed(m, days, [b]):
                by_pair[t.pair].append(t)
            out[str(b)][m] = {p: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in _stats(ts).items()
                                  if k in ("trades", "wins", "profit_abs", "sum_ratio")} for p, ts in by_pair.items()}
    return json.dumps(out)


# ------------------------------------------------------------------ groups & comparison
PALETTE = ["#7c5cff", "#00c2ff", "#f0b90b", "#0ecb81", "#ff6b9a", "#ff8a3d", "#a3e635", "#38bdf8", "#e879f9", "#94a3b8"]


def visible_bots(group_id: int | None = None) -> tuple[list[Bot], list[BotGroup]]:
    """Public, enabled bots (optionally of one group; 0 = ungrouped) and the public groups."""
    with SessionLocal() as s:
        groups = list(s.scalars(select(BotGroup).where(BotGroup.public.is_(True)).order_by(BotGroup.sort, BotGroup.id)))
        q = select(Bot).where(Bot.enabled.is_(True), Bot.public.is_(True))
        if group_id == 0:
            q = q.where(Bot.group_id.is_(None))
        elif group_id is not None:
            q = q.where(Bot.group_id == group_id)
        bots = list(s.scalars(q.order_by(Bot.sort, Bot.id)))
    return bots, groups


def group_summaries(groups: list[BotGroup], bots_by_group: dict[int, list[int]], market: str = "all",
                    days: int | None = 90) -> dict[int, dict]:
    """Combined numbers of all bots in each group."""
    out = {}
    for g in groups:
        ids = bots_by_group.get(g.id, [])
        st = _stats(_closed(market, days, ids), capitals(ids)) if ids else _stats([])
        out[g.id] = {k: v for k, v in st.items() if k != "last"} | {"bots": len(ids)}
    return out


def multi_svg(series: list[list[tuple[Any, float]]], w: int = 600, h: int = 200) -> dict:
    """Several cumulative curves on one time axis (one path per series)."""
    pts = [p for ser in series for p in ser]
    if len(pts) < 2:
        return {"paths": [], "zero": h / 2}
    t0 = min(p[0] for p in pts).timestamp()
    t1 = max(p[0] for p in pts).timestamp()
    tspan = (t1 - t0) or 1.0
    lo = min(0.0, min(p[1] for p in pts))
    hi = max(0.0, max(p[1] for p in pts))
    span = (hi - lo) or 1.0
    paths = []
    for i, ser in enumerate(series):
        if not ser:
            paths.append("")
            continue
        xy = [((d.timestamp() - t0) / tspan * w, h - (y - lo) / span * h) for d, y in ser]
        if len(xy) == 1:
            xy = [(0.0, h - (0 - lo) / span * h)] + xy
        paths.append("M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy))
    return {"paths": paths, "zero": h - (0 - lo) / span * h, "lo": lo, "hi": hi}


def _curve(trades: list[BotTrade]) -> list[tuple[Any, float]]:
    """Cumulative profit in stake currency."""
    pts, eq = [], 0.0
    for t in trades:
        eq += _amount(t)
        pts.append((t.close_date, eq))
    return pts


def pair_compare(pair: str, market: str, days: int | None, bots: list[Bot], groups: list[BotGroup]) -> dict:
    """One pair across bots (and groups): stats + overlaid cumulative-% curves."""
    trades = [t for t in _closed(market, days, [b.id for b in bots]) if t.pair == pair]
    rows = []
    for b in bots:
        ts = [t for t in trades if t.bot_id == b.id]
        if ts:
            rows.append({"kind": "bot", "id": b.id, "name": b.name, "group_id": b.group_id, "stats": _stats(ts),
                         "curve": _curve(ts)})
    grows = []
    for g in groups:
        ids = {b.id for b in bots if b.group_id == g.id}
        ts = [t for t in trades if t.bot_id in ids]
        if ts:
            grows.append({"kind": "group", "id": g.id, "name": g.name, "stats": _stats(ts), "curve": _curve(ts)})
    rows.sort(key=lambda r: -r["stats"]["profit_abs"])
    shown = rows[: len(PALETTE)]
    for i, r in enumerate(shown):
        r["color"] = PALETTE[i]
    chart = build_chart([{"key": f"b{r['id']}", "name": r["name"], "color": r["color"], "points": r["curve"],
                          "dash": False, "on": True} for r in shown], unit="USDT")
    return {"rows": rows, "groups": grows, "chart": chart}


def pair_matrix(market: str, days: int | None, bots: list[Bot]) -> dict[str, dict[int, dict]]:
    """{pair: {bot_id: stats}} for the pairs x bots table."""
    cell: dict[str, dict[int, list[BotTrade]]] = defaultdict(lambda: defaultdict(list))
    for t in _closed(market, days, [b.id for b in bots]):
        cell[t.pair][t.bot_id].append(t)
    return {p: {b: _stats(ts) for b, ts in byb.items()} for p, byb in sorted(cell.items())}


# ------------------------------------------------------------------ charts with axes
BENCH_COLORS = {"TOTAL": "#94a3b8", "BTC": "#f7931a", "ETH": "#627eea", "XRP": "#23a2d5", "BNB": "#f3ba2f",
                "SOL": "#14f195", "DOGE": "#c2a633", "ADA": "#3468d1", "TRX": "#ef0027"}


def _nice_step(span: float, n: int = 5) -> float:
    import math
    raw = span / max(1, n) or 1
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def build_chart(series: list[dict], unit: str = "%", w: int = 820, h: int = 280) -> dict | None:
    """series: [{"key","name","color","points":[(datetime, value)], "dash": bool, "on": bool}] -> SVG geometry."""
    import datetime as _dt
    L, R, T, B = 62, 14, 12, 30
    pts = [p for ser in series for p in ser["points"]]
    if len(pts) < 2:
        return None
    t0 = min(p[0] for p in pts).timestamp()
    t1 = max(p[0] for p in pts).timestamp()
    tspan = (t1 - t0) or 1.0
    lo, hi = min(0.0, min(p[1] for p in pts)), max(0.0, max(p[1] for p in pts))
    step = _nice_step((hi - lo) or 1)
    lo, hi = step * (lo // step), step * (-(-hi // step))
    span = (hi - lo) or 1.0
    pw, ph = w - L - R, h - T - B

    def X(ts: float) -> float:
        return L + (ts - t0) / tspan * pw

    def Y(v: float) -> float:
        return T + ph - (v - lo) / span * ph

    out = []
    for ser in series:
        if not ser["points"]:
            continue
        path = "M" + " L".join(f"{X(d.timestamp()):.1f},{Y(v):.1f}" for d, v in ser["points"])
        out.append({**{k: v for k, v in ser.items() if k != "points"}, "path": path, "last": ser["points"][-1][1]})
    fmt = "%Y-%m-%d" if tspan < 400 * 86400 else "%Y-%m"
    xticks = [(X(t0 + tspan * i / 4), _dt.datetime.fromtimestamp(t0 + tspan * i / 4).strftime(fmt)) for i in range(5)]
    yticks, v = [], lo
    while v <= hi + 1e-9:
        label = (f"{v:+.0f}%" if step >= 1 else f"{v:+.1f}%") if unit == "%" else f"{v:,.0f}"
        yticks.append((Y(v), label if v else "0"))
        v += step
    start = _dt.datetime.fromtimestamp(t0)
    end = _dt.datetime.fromtimestamp(t1)
    return {"w": w, "h": h, "L": L, "R": R, "T": T, "B": B, "series": out, "xticks": xticks, "yticks": yticks,
            "zero": Y(0), "unit": unit, "start": start, "end": end, "days": max(1, round(tspan / 86400)),
            # dates wrapped in Unicode LTR isolates so they read correctly inside Persian text
            "start_s": "\u2066" + start.strftime("%Y-%m-%d") + "\u2069", "end_s": "\u2066" + end.strftime("%Y-%m-%d") + "\u2069"}


def _drawdown_pct(values: list[float], base: float = 100.0) -> float:
    """Max fall from a high, in % of the high, for a path of % returns (equity = base * (1 + v/100))."""
    peak, dd = None, 0.0
    for v in values:
        eq = base * (1 + v / 100)
        peak = eq if peak is None else max(peak, eq)
        if peak > 0:
            dd = max(dd, (peak - eq) / peak * 100)
    return dd


def market_chart(trades: list[BotTrade], capital: float | None, days: int | None, stake: str = "USDT") -> dict:
    """The bots' cumulative result next to BTC, ETH, top coins and the whole market, same dates."""
    import datetime as _dt

    from . import benchmarks as bench
    if not trades:
        return {"chart": None, "rows": [], "has_capital": bool(capital)}
    start = (utcnow() - _dt.timedelta(days=days)) if days else min(t.close_date for t in trades)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    cum, pts = 0.0, [(start, 0.0)]
    for t in trades:
        cum += _amount(t)
        pts.append((t.close_date, cum / capital * 100 if capital else cum))
    series = [{"key": "bot", "name": "bot", "color": "var(--chart)", "points": pts, "dash": False, "on": True}]
    rows = []
    if capital:
        rows.append({"key": "bot", "name": "bot", "ret": pts[-1][1], "dd": _drawdown_pct([p[1] for p in pts])})
        for sym in bench.symbols():
            sp = bench.series(sym, start.date())
            if len(sp) < 2:
                continue
            series.append({"key": sym, "name": bench.NAMES.get(sym, sym), "color": BENCH_COLORS.get(sym, "#a3a3a3"),
                           "points": sp, "dash": sym == bench.TOTAL, "on": sym in ("TOTAL", "BTC", "ETH")})
            rows.append({"key": sym, "name": bench.NAMES.get(sym, sym), "ret": sp[-1][1], "dd": _drawdown_pct([p[1] for p in sp])})
    chart = build_chart(series, "%" if capital else stake)
    return {"chart": chart, "rows": rows, "has_capital": bool(capital), "approx_total": bench.is_approx_total()}


# ------------------------------------------------------------------ live (open) trades
def live_trades(bots: list[Bot], market: str = "all") -> dict:
    """Open trades of the given bots, as Freqtrade shows them right now.

    Bots with a REST API: /status (current price, unrealized profit incl. leverage), refreshed every few seconds.
    Bots with webhooks only: the open trades we know from the webhooks (no current price).
    The bot's pair filter and the side filter apply, like for closed trades.
    """
    from .botmonitor import monitor
    monitor.want_live()
    rows: list[dict] = []
    fallback = []
    updated, stale = [], []
    for b in bots:
        live = monitor.live.get(b.id)
        if live and live.get("at"):
            updated.append(live["at"])
            if live.get("error"):
                stale.append(b.name)
            for r in live["trades"]:
                rows.append({**r, "bot_id": b.id, "bot": b.name, "live": True})
        else:
            fallback.append(b)
    if fallback:
        names = {b.id: b.name for b in fallback}
        with SessionLocal() as s:
            for t in s.scalars(select(BotTrade).where(BotTrade.bot_id.in_(list(names)), BotTrade.is_open.is_(True))):
                rows.append({"ft_trade_id": t.ft_trade_id, "pair": t.pair, "side": t.side, "leverage": t.leverage or 1.0,
                             "open_date": t.open_date, "open_rate": t.open_rate, "current_rate": None,
                             "profit_ratio": None, "profit_abs": None, "stake_amount": t.stake_amount,
                             "stop_loss": None, "enter_tag": t.enter_tag, "pending": False,
                             "bot_id": t.bot_id, "bot": names[t.bot_id], "live": False})
    allows = {b.id: b.allows for b in bots}
    rows = [r for r in rows if allows[r["bot_id"]](r["pair"]) and (market == "all" or r["side"] == market)]
    rows.sort(key=lambda r: r["open_date"] or dt.datetime.min, reverse=True)
    now = utcnow()
    for r in rows:
        r["hours"] = (now - r["open_date"]).total_seconds() / 3600 if r["open_date"] else None
    priced = [r for r in rows if r["profit_abs"] is not None]
    pnl = sum(r["profit_abs"] for r in priced)
    stake = sum(r["stake_amount"] or 0 for r in priced)
    return {"rows": rows, "count": len(rows), "profit_abs": pnl, "stake": stake,
            "ratio": (pnl / stake) if stake else None,
            "winning": sum(1 for r in priced if r["profit_abs"] > 0), "priced": len(priced),
            "longs": sum(1 for r in rows if r["side"] == "long"), "shorts": sum(1 for r in rows if r["side"] == "short"),
            "updated": max(updated) if updated else None, "stale": stale}
