"""Executes one signal on one user's exchange account, records it, and notifies the user."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import ccxt.async_support as ccxt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import ExchangeAccount, GlobalSettings, Order, SessionLocal, Trade, User, get_global, open_trade_for, utcnow
from ..signal import Signal
from ..telegram import bot, esc, fmt_num
from .exchange import EXCHANGES, ExchangeClient, pool
from .sizing import max_open_trades, pair_budget

log = logging.getLogger("relay.trader")


@dataclass
class Outcome:
    account_id: int
    status: str  # opened | added | closed | partial | skipped | error | none
    message: str = ""


def _acc_name(acc: ExchangeAccount) -> str:
    return acc.label or EXCHANGES.get(acc.exchange, acc.exchange)


def _side_txt(side: str) -> str:
    return "🟢 Long" if side == "long" else "🔴 Short"


async def run_for_account(account_id: int, sig: Signal, pair: str) -> Outcome:
    async with pool.lock(account_id):
        with SessionLocal() as s:
            acc = s.get(ExchangeAccount, account_id)
            if acc is None:
                return Outcome(account_id, "none", "account deleted")
            user = s.get(User, acc.user_id)
            gs = get_global(s)
            try:
                client = await pool.get(acc)
                if sig.action in ("long", "short"):
                    return await _open(s, acc, user, gs, client, sig, pair)
                return await _close(s, acc, user, gs, client, sig, pair)
            except (ccxt.AuthenticationError, ccxt.PermissionDenied) as e:
                return _error(s, acc, user, pair, f"کلید API رد شد: {e}", disable=True)
            except ccxt.InsufficientFunds as e:
                return _skip(acc, user, pair, f"موجودی کافی نیست ({str(e)[:120]})")
            except ValueError as e:  # our own validation messages (min size, symbol not found, ...)
                return _skip(acc, user, pair, str(e))
            except Exception as e:
                log.exception("[acc %s] %s %s failed", account_id, sig.action, pair)
                return _error(s, acc, user, pair, f"{type(e).__name__}: {str(e)[:200]}")


def _skip(acc: ExchangeAccount, user: User, pair: str, why: str) -> Outcome:
    log.info("[acc %s] skip %s: %s", acc.id, pair, why)
    bot.notify(user, "skipped", f"⏭ <b>{esc(pair)}</b> روی {esc(_acc_name(acc))} انجام نشد\nدلیل: {esc(why)}")
    return Outcome(acc.id, "skipped", why)


def _error(s: Session, acc: ExchangeAccount, user: User, pair: str, msg: str, disable: bool = False) -> Outcome:
    if disable:
        acc.enabled = False
        acc.last_check_ok = False
        acc.last_check_msg = msg[:500]
        s.commit()
        msg += "\nاین صرافی غیرفعال شد؛ کلید را در سایت اصلاح و دوباره فعال کنید."
    bot.notify(user, "errors", f"⚠️ خطا در {esc(_acc_name(acc))} — <b>{esc(pair)}</b>\n{esc(msg)}")
    return Outcome(acc.id, "error", msg)


# ---------------------------------------------------------------------------- entries
async def _open(s: Session, acc: ExchangeAccount, user: User, gs: GlobalSettings,
                client: ExchangeClient, sig: Signal, pair: str) -> Outcome:
    side = sig.action
    if acc.is_spot and side == "short":
        return Outcome(acc.id, "none", "short not possible on spot")

    existing = open_trade_for(s, acc.id, pair, side)
    is_dca = existing is not None
    if existing is not None:
        same_ft = sig.ft_trade_id is not None and existing.ft_trade_id == sig.ft_trade_id
        if not same_ft or not sig.stake or not existing.scale:
            return Outcome(acc.id, "none", "position already open")
        stake = sig.stake * existing.scale  # DCA: same proportion as the original entry
    else:
        if gs.sizing_formula == "equal_split":
            n_open = s.scalar(select(func.count(Trade.id)).where(
                Trade.account_id == acc.id, Trade.status == "open")) or 0
            if n_open >= max_open_trades(gs, acc):
                return _skip(acc, user, pair, f"به سقف {max_open_trades(gs, acc)} معامله باز رسیده‌اید")
        stake = pair_budget(gs, acc, pair)
    if gs.max_stake_per_trade:
        stake = min(stake, gs.max_stake_per_trade)
    if stake <= 0:
        return Outcome(acc.id, "none", "zero stake")

    symbol = client.resolve(pair)
    lev = 1 if acc.is_spot else max(1, min(acc.leverage, gs.max_leverage))

    if not client.dry_run:
        free = await client.free_balance(gs.stake_currency)
        if free < stake:
            if free < stake * 0.5:
                return _skip(acc, user, pair, f"موجودی آزاد {free:,.2f} {gs.stake_currency} کمتر از مارجین لازم {stake:,.2f} است")
            stake = free * 0.98  # amend last stake, like Freqtrade's amend_last_stake_amount

    price = await client.price(symbol, sig.price)
    await client.setup_leverage(symbol, lev, acc.margin_mode)
    amount = client.amount_for_notional(symbol, stake * lev, price)
    fill = await client.market_order(symbol, "buy" if side == "long" else "sell", amount,
                                     reduce_only=False, price_hint=sig.price)

    cs = client.contract_size(symbol)
    held = fill.amount - (fill.fee_base if acc.is_spot else 0.0)  # spot buy fee is taken in base
    real_stake = fill.amount * cs * fill.price / lev

    if existing is None:
        trade = Trade(user_id=user.id, account_id=acc.id, ft_trade_id=sig.ft_trade_id, pair=pair, symbol=symbol,
                      side=side, amount=held, contract_size=cs, entry_price=fill.price, stake=real_stake,
                      ft_stake=sig.stake, scale=(real_stake / sig.stake) if sig.stake else None, leverage=lev,
                      fees=fill.fee_quote, enter_tag=sig.enter_tag, dry_run=fill.simulated)
        s.add(trade)
    else:
        trade = existing
        total = trade.amount + held
        trade.entry_price = (trade.entry_price * trade.amount + fill.price * held) / total
        trade.amount = total
        trade.stake += real_stake
        trade.fees += fill.fee_quote
    s.flush()
    s.add(Order(trade_id=trade.id, exchange_order_id=fill.order_id, kind="dca" if is_dca else "entry",
                side="buy" if side == "long" else "sell", amount=amount, filled=fill.amount,
                price=fill.price, fee=fill.fee_quote, status=fill.status))
    s.commit()

    title = "➕ افزایش پوزیشن" if is_dca else "✅ پوزیشن باز شد"
    bot.notify(user, "entry_fill", (
        f"{title} {'(آزمایشی)' if fill.simulated else ''}\n"
        f"<b>{esc(pair)}</b> {_side_txt(side)} — {esc(_acc_name(acc))}\n"
        f"قیمت: <b>{fmt_num(fill.price)}</b>\n"
        f"مقدار: {fmt_num(fill.amount * cs)} | ارزش: {fill.amount * cs * fill.price:,.2f} {gs.stake_currency}\n"
        f"مارجین: {real_stake:,.2f} | لوریج: {lev}x"
        + (f"\nتگ: {esc(sig.enter_tag)}" if sig.enter_tag else "")))
    return Outcome(acc.id, "added" if is_dca else "opened", f"{side} {fill.amount} {symbol} @ {fill.price}")


# ---------------------------------------------------------------------------- exits
async def _close(s: Session, acc: ExchangeAccount, user: User, gs: GlobalSettings,
                 client: ExchangeClient, sig: Signal, pair: str) -> Outcome:
    side = {"close_long": "long", "close_short": "short"}.get(sig.action)
    trade = open_trade_for(s, acc.id, pair, side)
    if trade is None:
        return Outcome(acc.id, "none", "no open position")

    full = sig.close_fraction >= 0.999
    amount = trade.amount if full else client.precise(trade.symbol, trade.amount * sig.close_fraction)
    if acc.is_spot and not client.dry_run:
        base = client.market(trade.symbol)["base"]
        amount = client.precise(trade.symbol, min(amount, await client.free_balance(base)))
    else:
        amount = client.precise(trade.symbol, amount)
    if amount <= 0 or amount < client.min_amount(trade.symbol):
        if full:  # nothing left that the exchange would accept: treat as closed
            trade.status, trade.closed_at, trade.exit_reason = "closed", utcnow(), "dust"
            s.commit()
        return Outcome(acc.id, "none", "amount below exchange minimum")

    close_side = "sell" if trade.side == "long" else "buy"
    try:
        fill = await client.market_order(trade.symbol, close_side, amount, reduce_only=True, price_hint=sig.price)
    except ccxt.InvalidOrder as e:
        msg = str(e).lower()
        if any(k in msg for k in ("reduce", "position", "no position", "110017", "-2022")):
            # position no longer exists on the exchange (closed manually / liquidated)
            trade.status, trade.closed_at, trade.exit_reason = "closed", utcnow(), "not_on_exchange"
            s.commit()
            return _error(s, acc, user, pair, "پوزیشن روی صرافی پیدا نشد (دستی بسته شده یا لیکویید شده). در سیستم بسته شد.")
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
    s.commit()

    pct = pnl / stake_part * 100 if stake_part else 0
    icon = "💰" if pnl >= 0 else "🔻"
    if is_final:
        total_pct = trade.realized_pnl / trade.stake * 100 if trade.stake else 0
        text = (f"{icon} پوزیشن بسته شد {'(آزمایشی)' if fill.simulated else ''}\n"
                f"<b>{esc(pair)}</b> {_side_txt(trade.side)} — {esc(_acc_name(acc))}\n"
                f"ورود: {fmt_num(trade.entry_price)} ← خروج: <b>{fmt_num(fill.price)}</b>\n"
                f"سود/زیان: <b>{trade.realized_pnl:+,.2f} {gs.stake_currency}</b> ({total_pct:+.2f}%)"
                + (f"\nدلیل: {esc(sig.exit_reason)}" if sig.exit_reason else ""))
        bot.notify(user, "exit_fill", text)
        return Outcome(acc.id, "closed", f"closed {trade.symbol} pnl={trade.realized_pnl:.4f}")
    text = (f"✂️ خروج بخشی ({sig.close_fraction:.0%}) {'(آزمایشی)' if fill.simulated else ''}\n"
            f"<b>{esc(pair)}</b> {_side_txt(trade.side)} — {esc(_acc_name(acc))}\n"
            f"قیمت: <b>{fmt_num(fill.price)}</b> | مقدار: {fmt_num(fill.amount * trade.contract_size)}\n"
            f"سود/زیان این بخش: <b>{pnl:+,.2f} {gs.stake_currency}</b> ({pct:+.2f}%)")
    bot.notify(user, "partial_exit", text)
    return Outcome(acc.id, "partial", f"partial {sig.close_fraction:.0%} {trade.symbol}")
