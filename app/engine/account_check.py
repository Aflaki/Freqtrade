"""Check a user's exchange API key before trading with it: does it work, may it trade futures, are withdrawals off,
is it locked to an IP, which position mode, other open positions, how much money is there.
And check the chosen pairs against the exchange's rules (listed? order big enough?)."""
from __future__ import annotations

from typing import Any

from . import exchange as exmod
from . import markets

OK, WARN, ERROR = "ok", "warn", "error"


def _item(status: str, key: str, **params) -> dict:
    return {"status": status, "key": key, "params": params}


def _client(exchange: str, mode: str, api_key: str, secret: str, password: str = "", sandbox: bool = False):
    klass_id, default_type = exmod.ccxt_target(exchange, mode)
    klass = getattr(exmod.ccxt, klass_id, None)
    if klass is None:
        raise ValueError(f"exchange '{exchange}' is not supported")
    params: dict[str, Any] = {"enableRateLimit": True, "timeout": 15000, "options": exmod.options_for(klass_id, defaultType=default_type),
                              **exmod.credentials(exchange, api_key, secret, password)}
    params["options"]["fetchCurrencies"] = False
    ex = klass(params)
    if sandbox:
        ex.set_sandbox_mode(True)
    from . import netips
    from .ratelimit import governor
    ip = netips.ip_for(exmod.ExchangeAccount())  # least used outgoing IP (users allow all of them)
    netips.bind(ex, ip)
    governor.attach(ex, ip)
    return ex


async def check_keys(exchange: str, mode: str, api_key: str, secret: str, password: str = "", sandbox: bool = False,
                     currency: str = "USDT") -> dict:
    """{"ok": no blocking problem, "reachable": exchange answered, "items": [...], "balance": {...} | None,
    "positions": open positions already on the account (see positions.normalize)}"""
    from ..web.guides import explain
    from .ratelimit import prio
    tok = prio.set("check")
    try:
        return await _check_keys(exchange, mode, api_key, secret, password, sandbox, currency, explain)
    finally:
        prio.reset(tok)


async def _check_keys(exchange, mode, api_key, secret, password, sandbox, currency, explain) -> dict:
    items: list[dict] = []
    out: dict[str, Any] = {"ok": False, "reachable": True, "items": items, "balance": None, "positions": []}
    try:
        ex = _client(exchange, mode, api_key, secret, password, sandbox)
    except ValueError as e:
        items.append(_item(ERROR, "ck_unsupported", err=str(e)))
        return out
    try:
        # 1) the key works at all + balance of the futures (or spot) wallet
        try:
            bal = await ex.fetch_balance()
        except exmod.ccxt.NetworkError as e:  # includes timeouts and DDoS protection
            out["reachable"] = False
            items.append(_item(WARN, "ck_unreachable", err=f"{type(e).__name__}: {str(e)[:150]}"))
            return out
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:250]}"
            items.append(_item(ERROR, "ck_key_bad", err=msg, hint=explain(msg) or ""))
            return out
        free = float((bal.get("free") or {}).get(currency) or 0)
        total = float((bal.get("total") or {}).get(currency) or 0)
        out["balance"] = {"free": free, "total": total, "currency": currency,
                          "wallet": exmod.wallet_of(bal, currency)}
        items.append(_item(OK, "ck_key_ok"))
        items.append(_item(OK if total > 0 else WARN, "ck_balance", free=f"{free:,.2f}", total=f"{total:,.2f}", cur=currency))
        # 2) Binance: the key's permissions (withdrawals must be off, futures on, IP restricted)
        if exchange == "binance" and hasattr(ex, "sapi_get_account_apirestrictions"):
            try:
                r = await ex.sapi_get_account_apirestrictions()
                if r.get("enableWithdrawals"):
                    items.append(_item(ERROR, "ck_withdraw_on"))
                else:
                    items.append(_item(OK, "ck_withdraw_off"))
                if mode == "futures":
                    items.append(_item(OK, "ck_futures_on") if r.get("enableFutures") else _item(ERROR, "ck_futures_off"))
                items.append(_item(OK, "ck_ip_on") if r.get("ipRestrict") else _item(WARN, "ck_ip_off"))
            except Exception as e:
                items.append(_item(WARN, "ck_perm_unknown", err=f"{type(e).__name__}: {str(e)[:120]}"))
        # 3) futures account settings: one-way position mode, positions that are not ours
        if mode == "futures":
            if getattr(ex, "has", {}).get("fetchPositionMode"):
                try:
                    pm = await ex.fetch_position_mode()
                    items.append(_item(WARN, "ck_hedge") if pm.get("hedged") else _item(OK, "ck_oneway"))
                except Exception:
                    pass
            if getattr(ex, "has", {}).get("fetchPositions"):
                try:
                    from .positions import normalize
                    pos = normalize(await ex.fetch_positions(), quote=currency)
                    out["positions"] = pos
                    items.append(_item(OK, "ck_positions", n=len(pos), pairs=", ".join(
                        p["pair"] for p in pos[:6])) if pos else _item(OK, "ck_no_positions"))
                except Exception:
                    pass
        out["ok"] = not any(i["status"] == ERROR for i in items)
        return out
    finally:
        try:
            await ex.close()
        except Exception:
            pass


def check_pairs(exchange: str, mode: str, selection: dict[str, dict], offers: dict[str, dict] | None = None,
                quote: str = "", stake: str = "USDT") -> list[dict]:
    """selection: {pair: {"margin": per-trade margin, "leverage": L, "bot": id}}; offers: {pair: rules from offers}.
    Spot: the pair is traded in `quote` (BTC/USDT -> BTC/EUR); margins are in it, the admin's minimums (in `stake`)
    are converted. Returns problems per pair: not listed / inactive / below the exchange or the admin minimum."""
    out = []
    q = (quote or stake).upper()
    # spot, or futures settled in another coin than the bots' (BTC/USDT -> the BTC/USDC:USDC perpetual)
    spot_q = q if mode == "spot" or q != stake.upper() else ""
    cache = markets.all_info()
    for pair, sel in selection.items():
        if spot_q:
            info = markets.market_info(pair, spot_q, exchange, mode, cache) if exchange else None
            cache_known = markets.known(exchange, mode, cache) if exchange else False
            if exchange and cache_known and info is None:
                out.append(_item(ERROR, "ck_pair_not_in_quote", pair=pair, base=pair.split("/")[0], quote=spot_q,
                                 ex=exmod.EXCHANGES.get(exchange, exchange)))
                continue
        else:
            info = markets.info(pair, exchange) if exchange else None
            cache_known = bool(((cache.get(exchange) or {}).get("pairs"))) if exchange else False
            if exchange and cache_known and info is None:
                out.append(_item(ERROR, "ck_pair_not_listed", pair=pair))
                continue
        if info and not info.get("active", True):
            out.append(_item(ERROR, "ck_pair_inactive", pair=pair))
            continue
        lev = max(1, int(sel.get("leverage") or 1)) if mode == "futures" else 1
        mx = (info or {}).get("max_leverage") if mode == "futures" else None
        if mx and lev > mx:  # traded at the exchange's maximum: same margin, smaller position
            out.append(_item(WARN, "ck_lev_capped", pair=pair, lev=lev, max=int(mx), ex=exmod.EXCHANGES.get(exchange, exchange)))
            lev = int(mx)
        need = markets.min_margin(info, lev)
        margin = float(sel.get("margin") or 0)
        if need and margin and margin < need:
            out.append(_item(ERROR, "ck_pair_below_exchange", pair=pair, need=f"{need:,.2f}", have=f"{margin:,.2f}", lev=lev))
        rules = (offers or {}).get(pair) or {}
        admin_min = (rules.get("min_capital") or {}).get(sel.get("bot"))
        if admin_min and spot_q:
            admin_min = markets.convert(admin_min, stake, spot_q, cache) or admin_min
        if admin_min and margin and margin < admin_min:
            out.append(_item(ERROR, "ck_pair_below_admin", pair=pair, need=f"{admin_min:,.2f}", have=f"{margin:,.2f}"))
    return out


def position_items(positions: list[dict], selected: set[str]) -> list[dict]:
    """What will happen to each position already open on the account."""
    return [_item(OK, "ck_pos_adopt", pair=p["pair"], side=p["side"].upper()) if p["pair"] in selected
            else _item(WARN, "ck_pos_ask", pair=p["pair"], side=p["side"].upper()) for p in positions]
