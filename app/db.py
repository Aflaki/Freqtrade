"""Database models (SQLite by default, same engine Freqtrade uses).

Every webhook, every exchange call and every Telegram message is stored, per user.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, String, Text, create_engine, event, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .settings import DATABASE_URL


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _):  # WAL = readers never block the trading engine
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


class Base(DeclarativeBase):
    pass


# Keys mirror Freqtrade's telegram notification_settings, plus our own
DEFAULT_NOTIFY: dict[str, bool] = {
    "entry_fill": True, "exit_fill": True, "partial_exit": True,
    "skipped": True, "errors": True, "daily_report": True,
    "bot_status": True, "bot_trades": True,  # admins only: Freqtrade events / bot trades + copy summary
    "monitoring": True,  # admins only: servers & bots health (down, restarted, stuck, Binance ban) - separate from
                         # Freqtrade's own chatter (bot_status), so muting that never silences these
    "phone_alerts": True,  # admins only: critical alerts (bot down / restarted) also to phone push + alert bot
}
ADMIN_NOTIFY = ("bot_status", "bot_trades", "monitoring", "phone_alerts")
SIZING_MODES = ("equal_split", "pair_weights", "custom_amounts", "wallet_pct")


# ============================================================================ users & accounts
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    lang: Mapped[str] = mapped_column(String(5), default="fa")
    theme: Mapped[str | None] = mapped_column(String(20), nullable=True)  # None = site default
    is_admin: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | suspended
    trading_enabled: Mapped[bool] = mapped_column(default=True)  # /pause /resume
    subscription_until: Mapped[dt.datetime | None] = mapped_column(nullable=True)  # None = unlimited
    admin_note: Mapped[str] = mapped_column(String(500), default="")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_username: Mapped[str] = mapped_column(String(64), default="")
    telegram_link_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notify: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: dict(DEFAULT_NOTIFY))
    # the most of their wallet this user may put to work (%); None = the site default (GlobalSettings)
    max_wallet_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_notice_date: Mapped[str] = mapped_column(String(10), default="")
    accepted_terms_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(64), default="")
    # profile (light, no KYC)
    country: Mapped[str] = mapped_column(String(2), default="")
    experience: Mapped[str] = mapped_column(String(20), default="")  # beginner | intermediate | pro
    referral_source: Mapped[str] = mapped_column(String(80), default="")
    marketing_opt_in: Mapped[bool] = mapped_column(default=False)
    profile_completed: Mapped[bool | None] = mapped_column(nullable=True)  # False only after social sign-up
    # security
    email_verified: Mapped[bool | None] = mapped_column(nullable=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(default=False)
    recovery_codes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # sha256 of unused codes
    session_version: Mapped[int] = mapped_column(default=1)  # bump = log out everywhere
    anti_phishing: Mapped[str] = mapped_column(String(32), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="")  # IANA name; "" = automatic (browser)
    email_code_hash: Mapped[str] = mapped_column(String(64), default="")  # e-mail confirmation code (hashed)
    email_code_expires: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    email_code_tries: Mapped[int] = mapped_column(default=0)

    accounts: Mapped[list[ExchangeAccount]] = relationship(back_populates="user", cascade="all, delete-orphan")
    identities: Mapped[list[Identity]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash) and not self.password_hash.startswith("!")

    def wants(self, kind: str) -> bool:
        return bool({**DEFAULT_NOTIFY, **(self.notify or {})}.get(kind, True))

    @property
    def subscription_active(self) -> bool:
        return self.subscription_until is None or self.subscription_until >= utcnow()

    @property
    def days_left(self) -> int | None:
        if self.subscription_until is None:
            return None
        return max(0, (self.subscription_until - utcnow()).days)


class Identity(Base):
    """A sign-in identity from an external provider (Google, Apple, Telegram, ...)."""
    __tablename__ = "identities"
    __table_args__ = (Index("uq_identity_provider_subject", "provider", "subject", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="identities")


class ExchangeAccount(Base):
    """A user's portfolio: either a real exchange API key (kind=exchange) or signals only (kind=signal)."""
    __tablename__ = "exchange_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="exchange")  # exchange | signal
    label: Mapped[str] = mapped_column(String(80), default="")
    exchange: Mapped[str] = mapped_column(String(40), default="")
    trading_mode: Mapped[str] = mapped_column(String(10), default="futures")  # spot | futures
    margin_mode: Mapped[str] = mapped_column(String(10), default="isolated")
    leverage: Mapped[int] = mapped_column(default=1)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # sha256 of exchange + API key: one key may be used by one portfolio only (the stored key itself is encrypted)
    api_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    out_ip: Mapped[str] = mapped_column(String(64), default="")  # outgoing IP of this portfolio (netips.py)
    sandbox: Mapped[bool] = mapped_column(default=False)
    dry_run: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    # money management (Freqtrade: available_capital, max_open_trades, pair_whitelist)
    capital: Mapped[float] = mapped_column(Float, default=0.0)
    sizing_mode: Mapped[str] = mapped_column(String(20), default="equal_split")
    max_open_trades: Mapped[int | None] = mapped_column(nullable=True)
    # {"BTC/USDT": value}: value = weight (pair_weights) or margin per trade (custom_amounts)
    pairs: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    bots: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)  # Freqtrade bots this portfolio follows
    pair_bot: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)  # {"BTC/USDT": bot_id}: take pair only from that bot
    # risk
    stoploss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # max loss % of margin
    daily_loss_limit_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # % of capital
    last_check_ok: Mapped[bool | None] = mapped_column(nullable=True)
    last_check_msg: Mapped[str] = mapped_column(String(500), default="")
    # positions found on the exchange that we did not open: those in the chosen pairs are taken over (wait for the
    # bot's exit); the others are listed here until the user decides to close them or keep them ("SYMBOL|side")
    # sizing "wallet_pct": each new trade = wallet_pct % of the wallet balance at that moment / number of pairs
    # (profits and losses compound), never below min_stake; wallet_balance = last balance read from the exchange
    wallet_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_stake: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_balance_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    foreign_positions: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    # bots vs exchange (reconcile.py): differences the user chose to leave as they are, and the last counts
    recon_ack: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    recon_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    kept_positions: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    positions_checked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="accounts")

    @property
    def undecided_positions(self) -> list[dict]:
        kept = set(self.kept_positions or [])
        return [p for p in (self.foreign_positions or []) if p.get("key") not in kept]

    @property
    def is_spot(self) -> bool:
        return self.trading_mode == "spot"

    @property
    def is_signal(self) -> bool:
        return self.kind == "signal"


# ============================================================================ trades
class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="CASCADE"), index=True)
    ft_trade_id: Mapped[int | None] = mapped_column(nullable=True)
    bot_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    pair: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str] = mapped_column(String(60))
    side: Mapped[str] = mapped_column(String(5))  # long | short
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # remaining, exchange units
    contract_size: Mapped[float] = mapped_column(Float, default=1.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    stake: Mapped[float] = mapped_column(Float, default=0.0)
    ft_stake: Mapped[float | None] = mapped_column(Float, nullable=True)
    scale: Mapped[float | None] = mapped_column(Float, nullable=True)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    closed_amount: Mapped[float] = mapped_column(Float, default=0.0)
    exit_value: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    sl_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enter_tag: Mapped[str] = mapped_column(String(80), default="")
    exit_reason: Mapped[str] = mapped_column(String(80), default="")
    dry_run: Mapped[bool] = mapped_column(default=False)
    is_signal: Mapped[bool] = mapped_column(default=False)
    adopted: Mapped[bool] = mapped_column(default=False)  # was already open on the exchange; we only manage the exit
    opened_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    closed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    account: Mapped[ExchangeAccount] = relationship()
    orders: Mapped[list[Order]] = relationship(back_populates="trade", cascade="all, delete-orphan")

    @property
    def exit_price(self) -> float | None:
        return self.exit_value / self.closed_amount if self.closed_amount else None

    @property
    def pnl_ratio(self) -> float:
        return self.realized_pnl / self.stake if self.stake else 0.0


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    exchange_order_id: Mapped[str] = mapped_column(String(80), default="")
    kind: Mapped[str] = mapped_column(String(10))  # entry | dca | exit | stop
    side: Mapped[str] = mapped_column(String(5))
    amount: Mapped[float] = mapped_column(Float)
    filled: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    trade: Mapped[Trade] = relationship(back_populates="orders")


class BotTrade(Base):
    """The Freqtrade bot's own trades: the public, verifiable track record."""
    __tablename__ = "bot_trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    __table_args__ = (Index("uq_bot_trades_bot_ft", "bot_id", "ft_trade_id", unique=True),)
    bot_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    ft_trade_id: Mapped[int] = mapped_column()
    pair: Mapped[str] = mapped_column(String(40), index=True)
    side: Mapped[str] = mapped_column(String(5))
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    open_date: Mapped[dt.datetime] = mapped_column(default=utcnow)
    close_date: Mapped[dt.datetime | None] = mapped_column(nullable=True, index=True)
    open_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)  # Freqtrade close_profit (incl. leverage)
    profit_abs: Mapped[float | None] = mapped_column(Float, nullable=True)  # Freqtrade close_profit_abs (stake currency)
    stake_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_open: Mapped[bool] = mapped_column(default=True)
    exit_reason: Mapped[str] = mapped_column(String(80), default="")
    enter_tag: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(10), default="webhook")  # webhook | api | sqlite

    @property
    def ratio_1x(self) -> float:
        """Profit without leverage: what a 1x position would have made."""
        return (self.profit_ratio or 0.0) / (self.leverage or 1.0)


# ============================================================================ logs
class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    bot_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    raw: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    msg_type: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(20), default="")  # accepted | ignored | duplicate | rejected
    action: Mapped[str] = mapped_column(String(20), default="")
    pair: Mapped[str] = mapped_column(String(40), default="")
    error: Mapped[str] = mapped_column(String(500), default="")
    accounts: Mapped[int] = mapped_column(default=0)
    summary: Mapped[str] = mapped_column(String(500), default="")
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class ExecutionLog(Base):
    """What we sent to an exchange for one user and what came back."""
    __tablename__ = "execution_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    webhook_event_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(nullable=True)
    trade_id: Mapped[int | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(20), default="")
    pair: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="")
    message: Mapped[str] = mapped_column(String(1000), default="")
    calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    duration_ms: Mapped[int] = mapped_column(default=0)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(30), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str] = mapped_column(String(500), default="")


# ============================================================================ freqtrade bots
class BotGroup(Base):
    """A set of bots whose results are shown combined (e.g. 'Version 1', 'Version 2')."""
    __tablename__ = "bot_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(500), default="")
    public: Mapped[bool] = mapped_column(default=True)
    sort: Mapped[int] = mapped_column(default=0)
    stats_capital: Mapped[float | None] = mapped_column(Float, nullable=True)  # starting capital of the whole group


def canon_pair(p: str) -> str:
    """'btcusdt', 'BTC/USDT:USDT', 'BTC-USDT' -> 'BTC/USDT' (USDT/USDC/BUSD/FDUSD/USD/BTC/ETH quotes)."""
    s = str(p).strip().upper().split(":")[0].replace("-", "/").replace("_", "/")
    if "/" in s:
        return s
    for q in ("FDUSD", "USDT", "USDC", "BUSD", "TUSD", "USD", "BTC", "ETH", "EUR"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)] + "/" + q
    return s


class Bot(Base):
    """One Freqtrade instance. It has its own webhook URL, so every message is attributed to it."""
    __tablename__ = "bots"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(500), default="")
    webhook_secret: Mapped[str] = mapped_column(String(80), unique=True, index=True,
                                                default=lambda: secrets.token_urlsafe(24))
    api_url: Mapped[str] = mapped_column(String(200), default="")
    api_user: Mapped[str] = mapped_column(String(80), default="")
    api_pass_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)       # accept its webhooks
    copy_enabled: Mapped[bool] = mapped_column(default=True)  # copy its signals to users
    public: Mapped[bool] = mapped_column(default=True)        # show on the website / selectable by users
    sort: Mapped[int] = mapped_column(default=0)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("bot_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    # pair filter: "none" | "whitelist" (only these pairs) | "blacklist" (all except these)
    pair_filter_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    pair_filter: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    stats_capital: Mapped[float | None] = mapped_column(Float, nullable=True)  # override of Freqtrade's starting_capital
    auto_restart: Mapped[bool] = mapped_column(default=True)  # watchdog may restart its process when it dies
    state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # monitor result
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    @property
    def st(self) -> dict[str, Any]:
        return self.state or {}

    @property
    def capital(self) -> float | None:
        """Starting capital used for % figures: manual value, else Freqtrade's /balance starting_capital."""
        v = self.stats_capital or (self.state or {}).get("starting_capital")
        try:
            return float(v) if v and float(v) > 0 else None
        except (TypeError, ValueError):
            return None

    def allows(self, pair: str) -> bool:
        """Is this pair part of the bot's results / signals (per its whitelist or blacklist)?"""
        mode, lst = self.pair_filter_mode or "none", {canon_pair(p) for p in (self.pair_filter or [])}
        if mode == "whitelist":
            return canon_pair(pair) in lst
        if mode == "blacklist":
            return canon_pair(pair) not in lst
        return True


# ============================================================================ market benchmarks
class PushSubscription(Base):
    """A browser / phone (installed web app) that receives Web Push notifications for a user."""
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    device: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str] = mapped_column(String(200), default="")


class WatchdogHost(Base):
    """A bot server running the watchdog agent (it reports every minute)."""
    __tablename__ = "watchdog_hosts"
    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(120), unique=True)
    ips: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    version: Mapped[str] = mapped_column(String(20), default="")
    last_seen: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # last report (bots, memory, load)
    silent_alerted: Mapped[bool] = mapped_column(default=False)
    ban: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # Binance IP ban / pause state
    alias: Mapped[str] = mapped_column(String(80), default="")  # the admin's own name for this server


class ServerSample(Base):
    """One measurement per watchdog report (every minute): the monitoring charts. Kept 7 days."""
    __tablename__ = "server_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(120), index=True)
    at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    load1: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # highest Binance weight use, % of limit
    bots_running: Mapped[int] = mapped_column(default=0)


class WatchdogEvent(Base):
    __tablename__ = "watchdog_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    host: Mapped[str] = mapped_column(String(120), index=True)
    bot_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    port: Mapped[int | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(20))  # gone | restarted | restart_failed | gave_up | recovered
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)


class PairOffer(Base):
    """A pair the admin offers to users from one bot (a pair may be offered from several bots; a user takes it from
    exactly one). Pair-level flags (mandatory) count if set on any offer of the pair."""
    __tablename__ = "pair_offers"
    __table_args__ = (Index("uq_pair_offer", "pair", "bot_id", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    pair: Mapped[str] = mapped_column(String(40), index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    enabled: Mapped[bool] = mapped_column(default=False)      # offered to users
    is_default: Mapped[bool] = mapped_column(default=False)   # pre-selected for new portfolios (this bot)
    mandatory: Mapped[bool] = mapped_column(default=False)    # every portfolio must include the pair
    min_capital: Mapped[float | None] = mapped_column(Float, nullable=True)  # minimum margin for this pair
    leverage: Mapped[int | None] = mapped_column(nullable=True)  # fixed leverage for this pair (None = portfolio's)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class BenchmarkPrice(Base):
    """Daily close of a market benchmark (BTC, ETH, top coins, TOTAL = whole crypto market cap)."""
    __tablename__ = "benchmark_prices"
    __table_args__ = (Index("uq_bench_symbol_day", "symbol", "day", unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    day: Mapped[dt.date] = mapped_column()
    close: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20), default="")


# ============================================================================ settings (admin panel)
class GlobalSettings(Base):
    __tablename__ = "global_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    # general
    site_name: Mapped[str] = mapped_column(String(80), default="Signal Relay")
    theme: Mapped[str] = mapped_column(String(20), default="aurora")
    default_lang: Mapped[str] = mapped_column(String(5), default="fa")
    default_timezone: Mapped[str] = mapped_column(String(64), default="UTC")  # for guests without browser info
    base_url: Mapped[str] = mapped_column(String(200), default="http://localhost:8000")
    hero: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"fa": {"title":..,"subtitle":..}, ...}
    public_performance: Mapped[bool] = mapped_column(default=True)
    signal_only_enabled: Mapped[bool] = mapped_column(default=True)
    require_approval: Mapped[bool] = mapped_column(default=True)
    trial_days: Mapped[int] = mapped_column(default=0)  # 0 = unlimited
    daily_report_hour_utc: Mapped[int] = mapped_column(default=20)
    last_daily_report: Mapped[str] = mapped_column(String(10), default="")
    # trading control & formula
    entries_enabled: Mapped[bool] = mapped_column(default=True)
    stake_currency: Mapped[str] = mapped_column(String(10), default="USDT")
    tradable_balance_ratio: Mapped[float] = mapped_column(Float, default=0.9)
    api_budget_pct: Mapped[float] = mapped_column(Float, default=80.0)
    outbound_ips: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # extra outgoing IPs of the site server  # share of each exchange's per-IP request limit we use
    sizing_modes: Mapped[list[str]] = mapped_column(JSON, default=lambda: list(SIZING_MODES))
    default_max_open_trades: Mapped[int] = mapped_column(default=5)
    max_leverage: Mapped[int] = mapped_column(default=5)
    min_capital: Mapped[float] = mapped_column(Float, default=50.0)
    max_stake_per_trade: Mapped[float | None] = mapped_column(Float, nullable=True)
    default_stoploss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    allowed_exchanges: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["binance", "bybit", "okx", "bitget", "kucoin", "gate"])
    allowed_pairs: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])
    # freqtrade connection
    webhook_secret: Mapped[str] = mapped_column(String(80), default=lambda: secrets.token_urlsafe(24))
    relay_on: Mapped[str] = mapped_column(String(10), default="fill")  # fill | order
    signal_source: Mapped[str] = mapped_column(String(10), default="freqtrade")  # freqtrade | generic
    allowed_ips: Mapped[list[str]] = mapped_column(JSON, default=list)
    dedupe_seconds: Mapped[int] = mapped_column(default=30)
    ft_api_url: Mapped[str] = mapped_column(String(200), default="")
    ft_api_user: Mapped[str] = mapped_column(String(80), default="")
    ft_api_pass_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    ft_db_path: Mapped[str] = mapped_column(String(300), default="")
    bot_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # last bot monitor result
    bench_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # benchmark updater status
    coingecko_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # optional free "demo" API key
    # sign-in & security (admin panel)
    auth_providers: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    captcha_provider: Mapped[str] = mapped_column(String(20), default="")  # "" | turnstile | hcaptcha | recaptcha
    captcha_site_key: Mapped[str] = mapped_column(String(200), default="")
    captcha_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_host: Mapped[str] = mapped_column(String(200), default="")
    smtp_port: Mapped[int] = mapped_column(default=587)
    smtp_user: Mapped[str] = mapped_column(String(200), default="")
    smtp_pass_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_from: Mapped[str] = mapped_column(String(200), default="")
    smtp_security: Mapped[str] = mapped_column(String(10), default="starttls")  # starttls | ssl | none
    require_email_verification: Mapped[bool] = mapped_column(default=False)
    email_confirm_required: Mapped[bool] = mapped_column(default=True)  # new users confirm their e-mail first
    require_2fa_admins: Mapped[bool] = mapped_column(default=False)
    password_min_length: Mapped[int] = mapped_column(default=10)
    check_breached_passwords: Mapped[bool] = mapped_column(default=True)
    login_alerts: Mapped[bool] = mapped_column(default=True)
    # telegram
    telegram_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # critical alerts: a second Telegram bot (kept unmuted) + Web Push to phones
    alert_bot_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    vapid_private_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    vapid_public: Mapped[str] = mapped_column(String(120), default="")
    # watchdog agents on the bot servers
    watchdog_token: Mapped[str] = mapped_column(String(64), default="")
    ban_auto_pause: Mapped[bool] = mapped_column(default=True)  # Binance bans a server's IP -> /stop its bots, /start later
    ban_resume_gap: Mapped[int] = mapped_column(default=30)  # seconds between bots when starting them again
    auto_register_bots: Mapped[bool] = mapped_column(default=True)  # bots found by the watchdog are added by themselves
    auto_webhooks: Mapped[bool] = mapped_column(default=True)  # the watchdog writes each bot's webhook into its config
    offers_migrated: Mapped[bool] = mapped_column(default=False)  # allowed_pairs -> pair_offers done once
    market_cache: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # exchange rules per pair
    market_widgets: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # home page market overview
    # user guides
    public_ip: Mapped[str] = mapped_column(String(200), default="")  # IP(s) users whitelist ("" = from site address)
    guide_videos: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # guide key -> video URL
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# ============================================================================ helpers
def get_global(s: Session) -> GlobalSettings:
    gs = s.get(GlobalSettings, 1)
    if gs is None:
        gs = GlobalSettings(id=1)
        s.add(gs)
        s.commit()
    return gs


def open_trade_for(s: Session, account_id: int, pair: str, side: str | None,
                   bot_id: int | None = None) -> Trade | None:
    q = select(Trade).where(Trade.account_id == account_id, Trade.pair == pair, Trade.status == "open")
    if side:
        q = q.where(Trade.side == side)
    if bot_id is not None:
        q = q.where(Trade.bot_id == bot_id)
    return s.scalars(q.order_by(Trade.id)).first()


def _auto_migrate() -> None:
    """Add columns introduced by newer versions to an existing database (no manual migrations)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                      f'{col.type.compile(engine.dialect)}'))
                # existing rows get the model default (a new column would otherwise be NULL there)
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if default is not None and not col.nullable and not col.primary_key:
                    conn.execute(text(f'UPDATE "{table.name}" SET "{col.name}" = :v WHERE "{col.name}" IS NULL'),
                                 {"v": default})


def _migrate_multi_bot() -> None:
    """v3 -> v4: single bot settings become 'Bot 1'; trades, events and portfolios are linked to it."""
    with engine.begin() as conn:
        idx = {i["name"]: i for i in inspect(conn).get_indexes("bot_trades")}
        old = idx.get("ix_bot_trades_ft_trade_id")
        if old and old.get("unique"):  # trade ids are only unique per bot now
            conn.execute(text("DROP INDEX ix_bot_trades_ft_trade_id"))
    for table in Base.metadata.sorted_tables:  # indexes added in newer versions
        for index in table.indexes:
            index.create(engine, checkfirst=True)
    with SessionLocal() as s:
        if s.scalar(select(Bot.id).limit(1)) is not None:
            return
        gs = get_global(s)
        bot = Bot(name="Bot 1", webhook_secret=gs.webhook_secret or secrets.token_urlsafe(24),
                  api_url=gs.ft_api_url or "", api_user=gs.ft_api_user or "", api_pass_enc=gs.ft_api_pass_enc,
                  state=gs.bot_state)
        s.add(bot)
        s.flush()
        for model in (BotTrade, Trade, WebhookEvent):
            s.execute(model.__table__.update().where(model.bot_id.is_(None)).values(bot_id=bot.id))
        for acc in s.scalars(select(ExchangeAccount)):
            if not acc.bots:
                acc.bots = [bot.id]
        s.commit()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _auto_migrate()
    with SessionLocal() as s:
        get_global(s)
    _migrate_multi_bot()
    from .engine.offers import migrate_offers
    migrate_offers()
    _backfill_key_hashes()


def key_hash(exchange: str, api_key: str) -> str:
    import hashlib
    return hashlib.sha256(f"{exchange}:{api_key.strip()}".encode()).hexdigest()


def _backfill_key_hashes() -> None:
    """Portfolios saved before keys had a fingerprint get one (needed to find a key used twice)."""
    from .security import decrypt
    with SessionLocal() as s:
        for acc in s.scalars(select(ExchangeAccount).where(ExchangeAccount.api_key_hash.is_(None),
                                                           ExchangeAccount.api_key_enc.is_not(None))):
            try:
                acc.api_key_hash = key_hash(acc.exchange, decrypt(acc.api_key_enc) or "")
            except RuntimeError:  # MASTER_KEY changed: can't read it
                continue
        s.commit()
