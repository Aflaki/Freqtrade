"""Database models (SQLite by default, same engine Freqtrade uses).

Every webhook, every exchange call and every Telegram message is stored, per user.
"""
from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, String, Text, create_engine, event, inspect, select, text
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
}
ADMIN_NOTIFY = ("bot_status", "bot_trades")
SIZING_MODES = ("equal_split", "pair_weights", "custom_amounts")


# ============================================================================ users & accounts
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    lang: Mapped[str] = mapped_column(String(5), default="fa")
    is_admin: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | suspended
    trading_enabled: Mapped[bool] = mapped_column(default=True)  # /pause /resume
    subscription_until: Mapped[dt.datetime | None] = mapped_column(nullable=True)  # None = unlimited
    admin_note: Mapped[str] = mapped_column(String(500), default="")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_username: Mapped[str] = mapped_column(String(64), default="")
    telegram_link_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notify: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: dict(DEFAULT_NOTIFY))
    last_notice_date: Mapped[str] = mapped_column(String(10), default="")
    accepted_terms_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    accounts: Mapped[list[ExchangeAccount]] = relationship(back_populates="user", cascade="all, delete-orphan")

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
    sandbox: Mapped[bool] = mapped_column(default=False)
    dry_run: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    # money management (Freqtrade: available_capital, max_open_trades, pair_whitelist)
    capital: Mapped[float] = mapped_column(Float, default=0.0)
    sizing_mode: Mapped[str] = mapped_column(String(20), default="equal_split")
    max_open_trades: Mapped[int | None] = mapped_column(nullable=True)
    # {"BTC/USDT": value}: value = weight (pair_weights) or margin per trade (custom_amounts)
    pairs: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    # risk
    stoploss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # max loss % of margin
    daily_loss_limit_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # % of capital
    last_check_ok: Mapped[bool | None] = mapped_column(nullable=True)
    last_check_msg: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="accounts")

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
    ft_trade_id: Mapped[int] = mapped_column(unique=True, index=True)
    pair: Mapped[str] = mapped_column(String(40), index=True)
    side: Mapped[str] = mapped_column(String(5))
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    open_date: Mapped[dt.datetime] = mapped_column(default=utcnow)
    close_date: Mapped[dt.datetime | None] = mapped_column(nullable=True, index=True)
    open_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)  # as Freqtrade reports (incl. leverage)
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


# ============================================================================ settings (admin panel)
class GlobalSettings(Base):
    __tablename__ = "global_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    # general
    site_name: Mapped[str] = mapped_column(String(80), default="Signal Relay")
    default_lang: Mapped[str] = mapped_column(String(5), default="fa")
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
    # telegram
    telegram_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# ============================================================================ helpers
def get_global(s: Session) -> GlobalSettings:
    gs = s.get(GlobalSettings, 1)
    if gs is None:
        gs = GlobalSettings(id=1)
        s.add(gs)
        s.commit()
    return gs


def open_trade_for(s: Session, account_id: int, pair: str, side: str | None) -> Trade | None:
    q = select(Trade).where(Trade.account_id == account_id, Trade.pair == pair, Trade.status == "open")
    if side:
        q = q.where(Trade.side == side)
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


def init_db() -> None:
    Base.metadata.create_all(engine)
    _auto_migrate()
    with SessionLocal() as s:
        get_global(s)
