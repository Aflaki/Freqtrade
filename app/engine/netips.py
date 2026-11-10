"""Several outgoing IP addresses on the site server: exchanges limit requests per IP, so each extra IP adds a full
request budget (Binance futures: 2400 weight per minute). The admin lists the server's extra addresses; every
portfolio is given one of them for good (its REST calls and its WebSocket go out from it), new portfolios go to the
least used one. Users must allow ALL of them in their API key (the guides list them), so a portfolio can be moved.
"""
from __future__ import annotations

import ipaddress
import logging
import socket

from sqlalchemy import func, select

from ..db import ExchangeAccount, SessionLocal, get_global

log = logging.getLogger("relay.netips")


def configured() -> list[str]:
    with SessionLocal() as s:
        return [ip for ip in (get_global(s).outbound_ips or []) if ip]


def usable(ip: str) -> str:
    """'' if this server can send from the address, else why not."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "not an IP address"
    fam = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    try:
        with socket.socket(fam, socket.SOCK_STREAM) as sock:
            sock.bind((ip, 0))
    except OSError as e:
        return f"not on this server ({e.strerror or e})"
    return ""


def ip_for(acc: ExchangeAccount) -> str:
    """The outgoing IP of this portfolio ('' = the server's default route). Assigned once, least used first."""
    ips = configured()
    if not ips:
        return ""
    if acc.out_ip in ips:
        return acc.out_ip
    with SessionLocal() as s:
        counts = dict(s.execute(select(ExchangeAccount.out_ip, func.count(ExchangeAccount.id))
                                .where(ExchangeAccount.out_ip.in_(ips)).group_by(ExchangeAccount.out_ip)).all())
        pick = min(ips, key=lambda ip: (counts.get(ip, 0), ips.index(ip)))
        if acc.id:
            row = s.get(ExchangeAccount, acc.id)
            if row is not None:
                row.out_ip = pick
                s.commit()
    acc.out_ip = pick
    return pick


def bind(ex, ip: str) -> None:
    """Send this ccxt client's requests (REST and WebSocket) from `ip`. Call inside the running event loop."""
    if not ip:
        return
    import aiohttp
    conn = aiohttp.TCPConnector(ssl=getattr(ex, "ssl_context", None) or True, local_addr=(ip, 0),
                                family=socket.AF_INET6 if ":" in ip else socket.AF_INET)
    ex.tcp_connector = conn
    ex.session = aiohttp.ClientSession(connector=conn, trust_env=getattr(ex, "aiohttp_trust_env", False))
    # ccxt closes a session it considers its own: keep that true so close() frees ours too
    ex.own_session = True
    ex.out_ip = ip
