"""Executes one signal on one portfolio, records it, logs every exchange call, and notifies the user."""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass

import ccxt.async_support as ccxt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import (ExchangeAccount, ExecutionLog, GlobalSettings, Order, SessionLocal, Trade, User, get_global,
                  open_trade_for, utcnow)
from ..i18n import t
from ..signal import Signal
from ..telegram import bot, esc, fmt_num
from .exchange import EXCHANGES, pool
from .ratelimit import ApiBanned
from .sizing import leverage_for, max_open_trades, pair_budget, stop_price

log = logging.getLogger("relay.trader")


@dataclass
class Outcome:
    account_id: int
    status: str  # opened | added | adopted | closed | partial | skipped | error | none
    message: str = ""


def _acc_name(acc: ExchangeAccount) -> str:
    if acc.is_signal:
        return acc.label or "Signals"
    return acc.label or EXCHANGES.get(acc.exchange, acc.exchange)


def _side(side: str) -> str:
    return "🟢 LONG" if side == "long" else "🔴 SHORT"


async def run_for_account(account_id: int, sig: Signal, pair: str, event_id: int | None = None,
                          bot_id: int | None = None) -> Outcome:
    t0 = time.monotonic()
    client = None
    from .ratelimit import prio
    prio.set("trade" if sig.action in ("long", "short") else "exit")  # exits go first when requests are scarce
    async with pool.lock(account_id):
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, account_id)
            if acc is None:
                return Outcome(account_id, "none", "account deleted")
            user = s.get(User, acc.user_id)
            gs = get_global(s)
            trade_ref: list[int] = []
            try:
                client = await pool.get(acc)
                if sig.action in ("long", "short"):
                    out = await _open(s, acc, user, gs, client, sig, pair, trade_ref, bot_id)
                else:
                    out = await _close(s, acc, user, gs, client, sig, pair, trade_ref, bot_id)
            except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
                out = _error(s, acc, user, pair, f"API key rejected: {e}", disable=True)
            except ApiBanned as e:
                out = _skip(acc, user, pair, t(user.lang, "why_ip_banned", err=str(e)[:160]))
            except ccxt.InsufficientFunds as e:
                out = _skip(acc, user, pair, t(user.lang, "why_funds", err=str(e)[:120]))
            except ValueError as e:
                out = _skip(acc, user, pair, str(e))
            except Exception as e:
                log.exception("[acc %s] %s %s failed", account_id, sig.action, pair)
                out = _error(s, acc, user, pair, f"{type(e).__name__}: {str(e)[:200]}")
            s.add(ExecutionLog(webhook_event_id=event_id, user_id=acc.user_id, account_id=acc.id,
                               trade_id=trade_ref[0] if trade_ref else None, action=sig.action, pair=pair,
                               status=out.status, message=out.message[:1000],
                               calls=client.drain_calls() if client else [],
                               duration_ms=int((time.monotonic() - t0) * 1000)))
            s.commit()
            return out


def _skip(acc: ExchangeAccount, user: User, pair: str, why: str) -> Outcome:
    log.info("[acc %s] skip %s: %s", acc.id, pair, why)
    bot.notify(user, "skipped", t(user.lang, "tg_skipped", pair=esc(pair), acc=esc(_acc_name(acc)), why=esc(why)))
    return Outcome(acc.id, "skipped", why)


def _error(s: Session, acc: ExchangeAccount, user: User, pair: str, msg: str, disable: bool = False) -> Outcome:
    text = msg
    if disable:
        acc.enabled = False
        acc.last_check_ok = False
        acc.last_check_msg = msg[:500]
        s.commit()
        text += "\n" + t(user.lang, "tg_key_disabled")
    bot.notify(user, "errors", t(user.lang, "tg_error", acc=esc(_acc_name(acc)), pair=esc(pair), msg=esc(text)))
    return Outcome(acc.id, "error", msg)


def _today_pnl(s: Session, acc_id: int) -> float:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return s.scalar(select(func.coalesce(func.sum(Trade.realized_pnl), 0)).where(
        Trade.account_id == acc_id, Trade.closed_at >= start)) or 0.0


async def _refresh_stop(s: Session, acc: ExchangeAccount, user: User, client, trade: Trade) -> None:
    """(Re)place the exchange-side stop loss for the remaining amount."""
    if not acc.stoploss_pct or acc.is_spot:
        return
    await client.cancel(trade.sl_order_id, trade.symbol)
    trade.sl_order_id = None
    if trade.status != "open" or trade.amount <= 0:
        return
    trade.sl_price = stop_price(trade.entry_price, trade.side, acc.stoploss_pct, trade.leverage)
    try:
        trade.sl_order_id = await client.place_stop(trade.symbol, "sell" if trade.side == "long" else "buy",
                                                    trade.amount, trade.sl_price)
    except Exception as e:
        bot.notify(user, "errors", t(user.lang, "tg_error", acc=esc(_acc_name(acc)), pair=esc(trade.pair),
                                     msg=esc(t(user.lang, "tg_sl_failed", err=str(e)[:200]))))


# ---------------------------------------------------------------------------- entries
async def _open(s: Session, acc: ExchangeAccount, user: User, gs: GlobalSettings, client,
                sig: Signal, pair: str, trade_ref: list, bot_id: int | None = None) -> Outcome:
    L = user.lang
    side = sig.action
    if acc.is_spot and side == "short":
        return Outcome(acc.id, "none", "short not possible on spot")

    existing = open_trade_for(s, acc.id, pair, side, bot_id)
    is_dca = existing is not None
    if existing is not None:
        same_ft = sig.ft_trade_id is not None and existing.ft_trade_id == sig.ft_trade_id
        if not same_ft or not sig.stake or not existing.scale:
            return Outcome(acc.id, "none", "position already open")
        stake = sig.stake * existing.scale
    else:
        if open_trade_for(s, acc.id, pair, "short" if side == "long" else "long"):
            return Outcome(acc.id, "none", "opposite position open")
        if open_trade_for(s, acc.id, pair, side):  # same pair already held for another bot
            return _skip(acc, user, pair, t(L, "why_other_bot"))
        if not client.simulated and not client.dry_run and not acc.is_spot:
            found = await _on_exchange(s, acc, user, client, sig, pair, side, bot_id, trade_ref)
            if found is not None:
                return found
        n_open = s.scalar(select(func.count(Trade.id)).where(Trade.account_id == acc.id, Trade.status == "open")) or 0
        mot = max_open_trades(gs, acc)
        if n_open >= mot:
            return _skip(acc, user, pair, t(L, "why_max_trades", n=mot))
        if acc.daily_loss_limit_pct:
            today = _today_pnl(s, acc.id)
            if today <= -acc.daily_loss_limit_pct / 100 * max(acc.capital, 1e-9):
                return _skip(acc, user, pair, t(L, "why_daily_loss", pct=acc.daily_loss_limit_pct))
        balance = None
        if acc.sizing_mode == "wallet_pct" and not client.simulated:
            try:  # compounding: the wallet as it is now (realized profits and losses included)
                balance = await client.wallet_balance(gs.stake_currency)
                acc.wallet_balance, acc.wallet_balance_at = balance, utcnow()
            except Exception as e:
                log.info("[acc %s] wallet balance unavailable, using the last one: %s", acc.id, e)
        stake = pair_budget(gs, acc, pair, balance)
    if gs.max_stake_per_trade:
        stake = min(stake, gs.max_stake_per_trade)
    if stake <= 0:
        return Outcome(acc.id, "none", "zero stake")

    symbol = client.resolve(pair)
    lev = leverage_for(gs, acc, pair, sig.leverage)
    if not client.dry_run:
        free = await client.free_balance(gs.stake_currency, estimate_ok=True)
        if free < stake * 1.3:  # close call: ask the exchange itself
            free = await client.free_balance(gs.stake_currency)
        if free < stake:
            if free < stake * 0.5:
                return _skip(acc, user, pair, t(L, "why_balance", free=f"{free:,.2f}", cur=gs.stake_currency,
                                                need=f"{stake:,.2f}"))
            stake = free * 0.98  # like Freqtrade's amend_last_stake_amount

    price = await client.price(symbol, sig.price)
    await client.setup_leverage(symbol, lev, acc.margin_mode)
    amount = client.amount_for_notional(symbol, stake * lev, price)
    fill = await client.market_order(symbol, "buy" if side == "long" else "sell", amount,
                                     reduce_only=False, price_hint=sig.price)

    cs = client.contract_size(symbol)
    held = fill.amount - (fill.fee_base if acc.is_spot else 0.0)
    real_stake = fill.amount * cs * fill.price / lev
    if hasattr(client, "note_spent"):
        client.note_spent(real_stake + fill.fee_quote, gs.stake_currency)
    if existing is None:
        trade = Trade(user_id=user.id, account_id=acc.id, ft_trade_id=sig.ft_trade_id, bot_id=bot_id, pair=pair, symbol=symbol,
                      side=side, amount=held, contract_size=cs, entry_price=fill.price, stake=real_stake,
                      ft_stake=sig.stake, scale=(real_stake / sig.stake) if sig.stake else None, leverage=lev,
                      fees=fill.fee_quote, enter_tag=sig.enter_tag, dry_run=fill.simulated, is_signal=acc.is_signal)
        s.add(trade)
    else:
        trade = existing
        total = trade.amount + held
        trade.entry_price = (trade.entry_price * trade.amount + fill.price * held) / total
        trade.amount = total
        trade.stake += real_stake
        trade.fees += fill.fee_quote
    s.flush()
    trade_ref.append(trade.id)
    s.add(Order(trade_id=trade.id, exchange_order_id=fill.order_id, kind="dca" if is_dca else "entry",
                side="buy" if side == "long" else "sell", amount=amount, filled=fill.amount,
                price=fill.price, fee=fill.fee_quote, status=fill.status))
    if acc.stoploss_pct:
        trade.sl_price = stop_price(trade.entry_price, side, acc.stoploss_pct, lev)
    if not acc.is_signal:
        await _refresh_stop(s, acc, user, client, trade)
    s.commit()

    qty, value = fill.amount * cs, fill.amount * cs * fill.price
    cur = gs.stake_currency
    sl_line = f"\n{t(L, 'tg_stoploss')}: {fmt_num(trade.sl_price)}" if trade.sl_price else ""
    tag_line = f"\n{t(L, 'tg_tag')}: {esc(sig.enter_tag)}" if sig.enter_tag else ""
    if acc.is_signal:
        head = t(L, "tg_signal_add" if is_dca else "tg_signal_open")
        text = (f"{head}\n<b>{esc(pair)}</b> {_side(side)} {'' if acc.is_spot else f'· {lev}x'}\n"
                f"{t(L, 'tg_entry')}: <b>{fmt_num(fill.price)}</b>\n"
                f"{t(L, 'tg_your_size')}: {real_stake:,.2f} {cur} ({t(L, 'tg_value')} {value:,.2f}){sl_line}{tag_line}")
    else:
        head = t(L, "tg_added" if is_dca else "tg_opened") + (f" {t(L, 'tg_paper')}" if fill.simulated else "")
        text = (f"{head}\n<b>{esc(pair)}</b> {_side(side)} — {esc(_acc_name(acc))}\n"
                f"{t(L, 'tg_price')}: <b>{fmt_num(fill.price)}</b>\n"
                f"{t(L, 'tg_amount')}: {fmt_num(qty)} | {t(L, 'tg_value')}: {value:,.2f} {cur}\n"
                f"{t(L, 'tg_margin')}: {real_stake:,.2f} | {t(L, 'tg_leverage')}: {lev}x{sl_line}{tag_line}")
    bot.notify(user, "entry_fill", text)
    return Outcome(acc.id, "added" if is_dca else "opened", f"{side} {fill.amount} {symbol} @ {fill.price}")


async def _on_exchange(s: Session, acc: ExchangeAccount, user: User, client, sig: Signal, pair: str, side: str,
                       bot_id: int | None, trade_ref: list) -> Outcome | None:
    """A position in this pair that we did not open is already on the exchange (by hand, or by an older bot copy):
    same direction -> take it over and wait for this bot's exit; opposite -> leave it alone and skip."""
    from .positions import adopt, normalize
    symbol = client.resolve(pair)
    try:
        pos = [p for p in normalize(await client.positions([symbol])) if p["symbol"] == symbol]
    except Exception as e:  # can't tell: carry on as before
        log.info("[acc %s] position check for %s failed: %s", acc.id, pair, e)
        return None
    if not pos:
        return None
    p = pos[0]
    L = user.lang
    if p["side"] != side:
        return _skip(acc, user, pair, t(L, "why_opposite_on_exchange", side=p["side"].upper()))
    trade = adopt(s, acc, user, p, bot_id, sig.ft_trade_id, sig.stake)
    acc.foreign_positions = [x for x in acc.foreign_positions or [] if x.get("key") != p["key"]] or None
    trade_ref.append(trade.id)
    await _refresh_stop(s, acc, user, client, trade)
    s.commit()
    bot.notify(user, "entry_fill", t(L, "pos_tg_adopted", acc=esc(_acc_name(acc)),
                                     lines=f"• <b>{esc(pair)}</b> {_side(side)} @ {fmt_num(p['entry'])}"))
    return Outcome(acc.id, "adopted", f"took over {side} {p['contracts']} {symbol} @ {p['entry']}")


# ---------------------------------------------------------------------------- exits
async def _close(s: Session, acc: ExchangeAccount, user: User, gs: GlobalSettings, client,
                 sig: Signal, pair: str, trade_ref: list, bot_id: int | None = None) -> Outcome:
    L = user.lang
    side = {"close_long": "long", "close_short": "short"}.get(sig.action)
    trade = open_trade_for(s, acc.id, pair, side, bot_id)
    if trade is None:
        return Outcome(acc.id, "none", "no open position")
    trade_ref.append(trade.id)

    full = sig.close_fraction >= 0.999
    amount = trade.amount if full else client.precise(trade.symbol, trade.amount * sig.close_fraction)
    if acc.is_spot and not client.dry_run:
        base = client.market(trade.symbol)["base"]
        amount = min(amount, await client.free_balance(base))
    amount = client.precise(trade.symbol, amount)
    if amount <= 0 or amount < client.min_amount(trade.symbol):
        if full:
            trade.status, trade.closed_at, trade.exit_reason = "closed", utcnow(), "dust"
            await client.cancel(trade.sl_order_id, trade.symbol)
            s.commit()
        return Outcome(acc.id, "none", "amount below exchange minimum")

    await client.cancel(trade.sl_order_id, trade.symbol)  # free the position for the exit order
    trade.sl_order_id = None
    close_side = "sell" if trade.side == "long" else "buy"
    try:
        fill = await client.market_order(trade.symbol, close_side, amount, reduce_only=True, price_hint=sig.price)
    except ccxt.InvalidOrder as e:
        msg = str(e).lower()
        if any(k in msg for k in ("reduce", "position", "110017", "-2022", "51169")):
            trade.status, trade.closed_at, trade.exit_reason = "closed", utcnow(), "not_on_exchange"
            s.commit()
            return _error(s, acc, user, pair, t(L, "tg_not_on_exchange"))
        raise

    direction = 1 if trade.side == "long" else -1
    pnl = (fill.price - trade.entry_price) * fill.amount * trade.contract_size * direction - fill.fee_quote
    stake_part = trade.stake * (fill.amount / trade.amount) if trade.amount else 0
    trade.amount = max(0.0, trade.amount - fill.amount)
    trade.closed_amount += fill.amount
    trade.exit_value += fill.price * fill.amount
    trade.realized_pnl += pnl
    trade.fees += fill.fee_quote
    trade.exit_reason = sig.exit_reason or trade.exit_reason
    is_final = full or trade.amount < max(client.min_amount(trade.symbol), 1e-12)
    if is_final:
        trade.status, trade.closed_at = "closed", utcnow()
    s.add(Order(trade_id=trade.id, exchange_order_id=fill.order_id, kind="exit", side=close_side, amount=amount,
                filled=fill.amount, price=fill.price, fee=fill.fee_quote, status=fill.status))
    if not is_final:
        await _refresh_stop(s, acc, user, client, trade)
    s.commit()

    cur = gs.stake_currency
    paper = f" {t(L, 'tg_paper')}" if fill.simulated and not acc.is_signal else ""
    reason = f"\n{t(L, 'tg_reason')}: {esc(sig.exit_reason)}" if sig.exit_reason else ""
    if is_final:
        pct = trade.realized_pnl / trade.stake * 100 if trade.stake else 0
        icon = "💰" if trade.realized_pnl >= 0 else "🔻"
        head = t(L, "tg_signal_close") if acc.is_signal else f"{icon} {t(L, 'tg_closed')}{paper}"
        text = (f"{head}\n<b>{esc(pair)}</b> {_side(trade.side)} — {esc(_acc_name(acc))}\n"
                f"{t(L, 'tg_entry')}: {fmt_num(trade.entry_price)} → {t(L, 'tg_exit')}: <b>{fmt_num(fill.price)}</b>\n"
                f"{t(L, 'tg_pnl')}: <b>{trade.realized_pnl:+,.2f} {cur}</b> ({pct:+.2f}%){reason}")
        bot.notify(user, "exit_fill", text)
        return Outcome(acc.id, "closed", f"closed {trade.symbol} pnl={trade.realized_pnl:.4f}")
    pct = pnl / stake_part * 100 if stake_part else 0
    frac = f"{sig.close_fraction:.0%}"
    head = t(L, "tg_signal_partial", pct=frac) if acc.is_signal else t(L, "tg_partial", pct=frac) + paper
    text = (f"{head}\n<b>{esc(pair)}</b> {_side(trade.side)} — {esc(_acc_name(acc))}\n"
            f"{t(L, 'tg_price')}: <b>{fmt_num(fill.price)}</b> | {t(L, 'tg_amount')}: {fmt_num(fill.amount * trade.contract_size)}\n"
            f"{t(L, 'tg_pnl_part')}: <b>{pnl:+,.2f} {cur}</b> ({pct:+.2f}%)")
    bot.notify(user, "partial_exit", text)
    return Outcome(acc.id, "partial", f"partial {frac} {trade.symbol}")


# ---------------------------------------------------------------------------- manual close
async def force_exit(trade_id: int, user_id: int | None = None) -> Outcome | None:
    """Close one open trade now (web 'close' button, Telegram /forceexit). user_id restricts ownership."""
    with SessionLocal() as s:
        tr = s.get(Trade, trade_id)
        if tr is None or tr.status != "open" or (user_id is not None and tr.user_id != user_id):
            return None
        sig = Signal(action=f"close_{tr.side}", symbol=tr.pair.replace("/", ""), pair=tr.pair,
                     close_fraction=1.0, exit_reason="manual", id=f"manual-{trade_id}-{dt.datetime.now().timestamp()}")
        acc_id, pair, bot_id = tr.account_id, tr.pair, tr.bot_id
    return await run_for_account(acc_id, sig, pair, None, bot_id)
