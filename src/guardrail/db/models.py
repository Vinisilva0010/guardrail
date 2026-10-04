"""Database models for phase 1: market data ingestion.

Trading models (signal, trade_contract, position, post_mortem) are introduced in
phase 3 and deliberately not defined here.

All price and quantity columns use Numeric, which maps to Decimal in Python.
Floats are never used for money: binary floating point cannot represent decimal
fractions exactly, and position sizing compounds that error.
"""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from guardrail.db.base import Base

# Precision chosen to cover both high-value assets (BTC) and low-value tokens
# without loss: 28 total digits, 12 of them after the decimal point.
PRICE = Numeric(28, 12)
QTY = Numeric(28, 12)
RATE = Numeric(18, 12)


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Store enum values, not member names, in the database.

    SQLAlchemy defaults to persisting the member name ('CRYPTO'). Persisting the
    value ('crypto') keeps hand-written SQL consistent with the Python code; the
    default would silently return no rows for a lowercase literal.
    """
    return [str(member.value) for member in enum_cls]


class AssetClass(enum.StrEnum):
    """Market an instrument belongs to."""

    CRYPTO = "crypto"
    EQUITY = "equity"


ASSET_CLASS_ENUM = Enum(
    AssetClass,
    name="asset_class",
    native_enum=True,
    values_callable=_enum_values,
)


class Instrument(Base):
    """A tradable symbol on a specific venue."""

    __tablename__ = "instrument"
    __table_args__ = (
        UniqueConstraint("venue", "symbol"),
        Index("ix_instrument_asset_class_is_active", "asset_class", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[AssetClass] = mapped_column(ASSET_CLASS_ENUM, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    candles: Mapped[list["Candle"]] = relationship(back_populates="instrument")


class Candle(Base):
    """OHLCV bar.

    The composite primary key makes duplicate inserts impossible at the database
    level, so a collector that re-runs over the same window cannot corrupt the
    series.
    """

    __tablename__ = "candle"
    __table_args__ = (
        CheckConstraint("timeframe IN ('1h', '4h', '1d')", name="timeframe_allowed"),
        CheckConstraint("high >= low", name="high_ge_low"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        # Open and close must sit inside the bar's range. Without this, a bar
        # with a close above its high passes every other check and reaches the
        # backtest as a plausible value.
        CheckConstraint("open BETWEEN low AND high", name="open_within_range"),
        CheckConstraint("close BETWEEN low AND high", name="close_within_range"),
        # A zero price is a reporting gap, never a reading. Storing it would read
        # as a 100% move to any drawdown calculation.
        CheckConstraint("low > 0", name="prices_positive"),
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), primary_key=True
    )
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[Decimal] = mapped_column(QTY, nullable=False)

    instrument: Mapped["Instrument"] = relationship(back_populates="candles")


class DerivativeStat(Base):
    """Funding rate and open interest snapshot for a perpetual contract."""

    __tablename__ = "derivative_stat"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    funding_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    open_interest: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    open_interest_value: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)

    # Positioning ratios, present only in the Binance Vision daily dumps. The
    # REST endpoint does not return them, so rows written from the recent-window
    # collector leave these null. They are backtest candidates, not setup inputs.
    toptrader_long_short_account_ratio: Mapped[Decimal | None] = mapped_column(
        RATE, nullable=True
    )
    toptrader_long_short_position_ratio: Mapped[Decimal | None] = mapped_column(
        RATE, nullable=True
    )
    taker_long_short_volume_ratio: Mapped[Decimal | None] = mapped_column(
        RATE, nullable=True
    )


class Catalyst(Base):
    """A news item attached to an instrument.

    Used only as a boolean gate by the equity setup, never as a weighted score.
    dedupe_key is a hash of the source payload: news APIs repeat the same item
    across polls, and the unique constraint drops repeats at insert time.
    """

    __tablename__ = "catalyst"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index("ix_catalyst_instrument_id_ts", "instrument_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)


class SourceHealth(Base):
    """Last known state of each ingestion source.

    The worst failure mode of this system is a collector dying silently while the
    engine keeps firing signals on stale data. This table is what the freshness
    check and the Telegram heartbeat read.
    """

    __tablename__ = "source_health"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
