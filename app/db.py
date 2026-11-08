"""Database models. SQLite by default; set DATABASE_URL to Postgres for production."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .settings import config


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


_is_sqlite = config.database_url.startswith("sqlite")
if _is_sqlite and ":///./" in config.database_url:
    Path(config.database_url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    config.database_url,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# Same keys as Freqtrade's telegram notification_settings, plus our own
DEFAULT_NOTIFY: dict[str, bool] = {
    "entry_fill": True,   # position opened on your exchange
    "exit_fill": True,    # position closed (full)
    "partial_exit": True, # part of a position closed
    "skipped": True,      # signal skipped (no balance, limit reached, ...)
    "errors": True,       # exchange / API errors
}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | active | suspended
    trading_enabled: Mapped[bool] = mapped_column(default=True)  # user's own pause switch (/pause)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_link_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    notify: Mapped[dict[str, Any]] = mapped_column(JSON, default=lambda: dict(DEFAULT_NOTIFY))
    accepted_terms_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    accounts: Mapped[list[ExchangeAccount]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def wants(self, kind: str) -> bool:
        return bool({**DEFAULT_NOTIFY, **(self.notify or {})}.get(kind, True))


class ExchangeAccount(Base):
    """One exchange API key of a user, with its own capital and pair whitelist."""
    __tablename__ = "exchange_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(80), default="")
    exchange: Mapped[str] = mapped_column(String(40))
    trading_mode: Mapped[str] = mapped_column(String(10), default="futures")  # spot | futures
    margin_mode: Mapped[str] = mapped_column(String(10), default="isolated")  # isolated | cross
    leverage: Mapped[int] = mapped_column(default=1)
    api_key_enc: Mapped[str] = mapped_column(Text)
    secret_enc: Mapped[str] = mapped_column(Text)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox: Mapped[bool] = mapped_column(default=False)
    dry_run: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    # Freqtrade "available_capital": the money this user puts on this account (stake currency)
    capital: Mapped[float] = mapped_column(Float, default=0.0)
    max_open_trades: Mapped[int | None] = mapped_column(nullable=True)
    # Freqtrade "pair_whitelist" + optional weights: {"BTC/USDT": 2, "ETH/USDT": 1}
    pairs: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    last_check_ok: Mapped[bool | None] = mapped_column(nullable=True)
    last_check_msg: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="accounts")

    @property
    def is_spot(self) -> bool:
        return self.trading_mode == "spot"


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("exchange_accounts.id", ondelete="CASCADE"), index=True)
    ft_trade_id: Mapped[int | None] = mapped_column(nullable=True)
    pair: Mapped[str] = mapped_column(String(40))  # canonical "BTC/USDT"
    symbol: Mapped[str] = mapped_column(String(60))  # exchange symbol "BTC/USDT:USDT"
    side: Mapped[str] = mapped_column(String(5))  # long | short
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open | closed
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # remaining, exchange units (contracts or base)
    contract_size: Mapped[float] = mapped_column(Float, default=1.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)  # weighted average
    stake: Mapped[float] = mapped_column(Float, default=0.0)  # user margin used
    ft_stake: Mapped[float | None] = mapped_column(Float, nullable=True)  # Freqtrade margin at entry
    scale: Mapped[float | None] = mapped_column(Float, nullable=True)  # stake / ft_stake, reused for DCA
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    closed_amount: Mapped[float] = mapped_column(Float, default=0.0)
    exit_value: Mapped[float] = mapped_column(Float, default=0.0)  # sum(exit_price * amount)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    enter_tag: Mapped[str] = mapped_column(String(80), default="")
    exit_reason: Mapped[str] = mapped_column(String(80), default="")
    dry_run: Mapped[bool] = mapped_column(default=False)
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
    kind: Mapped[str] = mapped_column(String(10))  # entry | dca | exit
    side: Mapped[str] = mapped_column(String(5))  # buy | sell
    amount: Mapped[float] = mapped_column(Float)
    filled: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=utcnow)

    trade: Mapped[Trade] = relationship(back_populates="orders")


class GlobalSettings(Base):
    """Admin-controlled formula and limits. Single row (id=1)."""
    __tablename__ = "global_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    entries_enabled: Mapped[bool] = mapped_column(default=True)  # kill switch for NEW entries
    require_approval: Mapped[bool] = mapped_column(default=True)
    # equal_split : stake per trade = capital * ratio / max_open_trades   (Freqtrade "unlimited" stake)
    # pair_weights: budget per pair = capital * ratio * weight / sum(weights)
    sizing_formula: Mapped[str] = mapped_column(String(20), default="equal_split")
    tradable_balance_ratio: Mapped[float] = mapped_column(Float, default=0.9)
    default_max_open_trades: Mapped[int] = mapped_column(default=5)
    max_leverage: Mapped[int] = mapped_column(default=5)
    min_capital: Mapped[float] = mapped_column(Float, default=50.0)
    max_stake_per_trade: Mapped[float | None] = mapped_column(Float, nullable=True)
    stake_currency: Mapped[str] = mapped_column(String(10), default="USDT")
    allowed_exchanges: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["binance", "bybit", "okx", "bitget", "kucoin", "gate"])
    allowed_pairs: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])
    updated_at: Mapped[dt.datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SignalLog(Base):
    __tablename__ = "signal_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[dt.datetime] = mapped_column(default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(20))
    pair: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(String(500), default="")


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


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        get_global(s)
