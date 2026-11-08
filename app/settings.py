"""Environment-based settings (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Environment variable {name} is required (see .env.example)")
    return v


@dataclass(frozen=True)
class Config:
    secret_key: str  # signs session cookies
    master_key: str  # Fernet key that encrypts users' exchange API keys
    webhook_secret: str  # Freqtrade puts this in the webhook URL
    database_url: str
    telegram_bot_token: str
    base_url: str
    secure_cookies: bool
    signal_source: str  # "freqtrade" | "generic"
    freqtrade_relay_on: str  # "fill" | "order"
    allowed_ips: tuple[str, ...]
    dedupe_seconds: int


def load_config() -> Config:
    webhook_secret = _req("WEBHOOK_SECRET")
    if len(webhook_secret) < 16:
        raise RuntimeError("WEBHOOK_SECRET must be at least 16 characters")
    source = os.getenv("SIGNAL_SOURCE", "freqtrade").lower()
    relay_on = os.getenv("FREQTRADE_RELAY_ON", "fill").lower()
    if source not in ("freqtrade", "generic"):
        raise RuntimeError("SIGNAL_SOURCE must be 'freqtrade' or 'generic'")
    if relay_on not in ("fill", "order"):
        raise RuntimeError("FREQTRADE_RELAY_ON must be 'fill' or 'order'")
    return Config(
        secret_key=_req("SECRET_KEY"),
        master_key=_req("MASTER_KEY"),
        webhook_secret=webhook_secret,
        database_url=os.getenv("DATABASE_URL", "sqlite:///./data/relay.db"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        secure_cookies=os.getenv("SECURE_COOKIES", "false").lower() in ("1", "true", "yes"),
        signal_source=source,
        freqtrade_relay_on=relay_on,
        allowed_ips=tuple(ip.strip() for ip in os.getenv("WEBHOOK_ALLOWED_IPS", "").split(",") if ip.strip()),
        dedupe_seconds=int(os.getenv("DEDUPE_SECONDS", "30")),
    )


config = load_config()
