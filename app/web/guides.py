"""Step-by-step guides for users: connecting Telegram, creating exchange API keys (Binance sub-account + futures)."""
from __future__ import annotations

import re
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

from ..db import SessionLocal, get_global
from ..engine.exchange import EXCHANGES, NEEDS_PASSWORD
from .common import render

router = APIRouter()
_ip_cache: dict[str, str] = {}

# Each step: t = title key, b = text key, path = menu path as shown in the exchange (its own English labels),
# (a path item starting with _ is a translation key: our own menus; the exchange's menus stay as shown there)
# perms = [(label, tick?)] checkbox mock-up, ip = show our server IP with a copy button, warn = warning key.
GUIDES: dict[str, dict] = {
    "telegram": {"icon": "✈️", "title": "g_tg_title", "intro": "g_tg_intro", "steps": [
        {"t": "g_tg_s1_t", "b": "g_tg_s1_b", "path": ["_nav_settings", "Telegram", "_btn_link_tg"]},
        {"t": "g_tg_s2_t", "b": "g_tg_s2_b", "path": ["Telegram", "START"]},
        {"t": "g_tg_s3_t", "b": "g_tg_s3_b"},
        {"t": "g_tg_s4_t", "b": "g_tg_s4_b", "warn": "g_tg_s4_w"},
    ], "admin_steps": [
        {"t": "g_tga_s1_t", "b": "g_tga_s1_b", "path": ["@BotFather", "/newbot"]},
        {"t": "g_tga_s2_t", "b": "g_tga_s2_b", "path": ["_nav_admin", "_adm_settings", "Telegram"], "warn": "g_tga_s2_w"},
        {"t": "g_tga_s3_t", "b": "g_tga_s3_b", "path": ["@BotFather", "/newbot", "_nav_admin", "_alertbot_title"]},
        {"t": "g_tga_s4_t", "b": "g_tga_s4_b", "path": ["@BotFather", "/setdomain"]},
    ]},
    "binance": {"icon": "🟡", "title": "g_bn_title", "intro": "g_bn_intro", "steps": [
        {"t": "g_bn_s0_t", "b": "g_bn_s0_b", "path": ["Profile", "Account", "Identification · Security (2FA)"]},
        {"t": "g_bn_s1_t", "b": "g_bn_s1_b", "path": ["Profile", "Sub Accounts", "Account Management", "Create Sub Account"],
         "warn": "g_bn_s1_w"},
        {"t": "g_bn_s2_t", "b": "g_bn_s2_b", "path": ["Sub Accounts", "Account Management", "⋯", "Futures: Enable"],
         "warn": "g_bn_s2_w"},
        {"t": "g_bn_s3_t", "b": "g_bn_s3_b", "path": ["Sub Accounts", "Asset Management", "Transfer"]},
        {"t": "g_bn_s4_t", "b": "g_bn_s4_b", "path": ["Sub Accounts", "API Management", "Create API", "System generated"]},
        {"t": "g_bn_s5_t", "b": "g_bn_s5_b", "ip": True, "warn": "g_bn_s5_w",
         "perms": [("Enable Reading", True), ("Enable Futures", True), ("Enable Spot & Margin Trading", False),
                   ("Enable Withdrawals", False), ("Enable Internal Transfer", False),
                   ("Restrict access to trusted IPs only", True)]},
        {"t": "g_bn_s6_t", "b": "g_bn_s6_b", "path": ["_nav_portfolios", "_btn_add_exchange", "Binance · Futures"]},
        {"t": "g_bn_s7_t", "b": "g_bn_s7_b", "path": ["_nav_portfolios", "_btn_test"]},
    ], "errors": [("-2015", "g_err_2015"), ("-1022", "g_err_1022"), ("-2014 / -2008", "g_err_2014"),
                  ("-4061", "g_err_mode")]},
    "exchanges": {"icon": "🏦", "title": "g_ex_title", "intro": "g_ex_intro", "steps": [
        {"t": "g_ex_s1_t", "b": "g_ex_s1_b"},
        {"t": "g_ex_s2_t", "b": "g_ex_s2_b", "path": ["Profile / Account", "API Management", "Create API key"]},
        {"t": "g_ex_s3_t", "b": "g_ex_s3_b", "ip": True,
         "perms": [("Read", True), ("Trade · Futures / Contracts", True), ("Withdraw", False), ("Transfer", False),
                   ("IP whitelist / bind IP", True)]},
        {"t": "g_ex_s4_t", "b": "g_ex_s4_b"},
        {"t": "g_ex_s5_t", "b": "g_ex_s5_b", "path": ["_nav_portfolios", "_btn_test"]},
    ], "notes": ["bybit", "okx", "bitget", "kucoin", "gate", "mexc", "htx", "bingx"]},
}
ORDER = ("telegram", "binance", "exchanges")


def server_ips() -> list[str]:
    """IPs users must allow in their API keys. Admin setting (several while moving servers), else the site's address."""
    with SessionLocal() as s:
        gs = get_global(s)
        manual, base = (gs.public_ip or "").strip(), gs.base_url or ""
        extra = [ip for ip in (gs.outbound_ips or []) if ip]
    if manual:
        out = [x for x in re.split(r"[\s,;]+", manual) if x]
    else:
        ip = ip_of_url(base)
        out = [ip] if ip else []
    return out + [ip for ip in extra if ip not in out]


def server_ip() -> str:
    return ", ".join(server_ips())


def ip_of_url(base: str) -> str:
    host = urlparse(base).hostname or ""
    if not host:
        return ""
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        return host
    m = re.match(r"^(\d+)-(\d+)-(\d+)-(\d+)\.(sslip|nip)\.io$", host)  # 65-109-188-248.sslip.io
    if m:
        return ".".join(m.groups()[:4])
    if host not in _ip_cache:
        try:
            _ip_cache[host] = socket.gethostbyname(host)
        except OSError:
            _ip_cache[host] = ""
    return _ip_cache[host]


def video_embed(url: str) -> dict | None:
    """YouTube / Aparat / direct video file -> what the template needs to show it."""
    url = (url or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
    m = re.search(r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([\w-]{6,20})", url)
    if m:
        return {"kind": "iframe", "src": f"https://www.youtube-nocookie.com/embed/{m.group(1)}"}
    m = re.search(r"aparat\.com/v/([\w-]+)", url)
    if m:
        return {"kind": "iframe", "src": f"https://www.aparat.com/video/video/embed/videohash/{m.group(1)}/vt/frame"}
    if re.search(r"\.(mp4|webm|mov)(\?|$)", url, re.I):
        return {"kind": "video", "src": url}
    return {"kind": "link", "src": url}


@router.get("/guides")
async def guides_index(request: Request):
    return render(request, "guides.html", guides=[(k, GUIDES[k]) for k in ORDER])


@router.get("/guides/{key}")
async def guide(key: str, request: Request):
    if key not in GUIDES:
        raise HTTPException(404)
    from ..telegram import bot
    with SessionLocal() as s:
        gs = get_global(s)
        videos = dict(gs.guide_videos or {})
        min_cap, cur = gs.min_capital, gs.stake_currency
    return render(request, "guide.html", key=key, g=GUIDES[key], others=[k for k in ORDER if k != key],
                  ip=server_ip(), ips=server_ips(), video=video_embed(videos.get(key, "")), tg_bot=bot.username, min_cap=min_cap,
                  cur=cur, EX=EXCHANGES, NEEDS_PASSWORD=NEEDS_PASSWORD,
                  GUIDE_TITLES={k: GUIDES[k]["title"] for k in ORDER})


# ------------------------------------------------------------------ readable exchange errors
_HINTS = [
    (r"-2015|Invalid API-key, IP, or permissions", "g_err_2015"),
    (r"-1022|Signature for this request is not valid|signature", "g_err_1022"),
    (r"-2014|-2008|API-key format invalid|Invalid Api-Key ID|apikey.*(invalid|not exist)", "g_err_2014"),
    (r"-4061|position side|dual side", "g_err_mode"),
    (r"passphrase", "g_err_passphrase"),
    (r"(?i)ip.*(not|white)|whitelist", "g_err_ip"),
]


def explain(msg: str) -> str | None:
    """i18n key of a plain-language explanation for a failed connection test (None = no idea)."""
    for pattern, key in _HINTS:
        if re.search(pattern, msg or "", re.I):
            return key
    return None
