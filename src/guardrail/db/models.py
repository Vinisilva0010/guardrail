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
        # The primary key is (instrument_id, timeframe, ts). Cross-instrument
        # scans — "20-day high for every symbol in the universe", which is what
        # the equity setup runs daily — do not filter on instrument_id, so the
        # planner cannot seek with that key and walks the whole index instead.
        # This index puts the filtered columns first so those scans can seek.
        Index("ix_candle_timeframe_ts", "timeframe", "ts"),
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


class UniverseMembership(Base):
    """Which instruments belonged to the tradable universe, and when.

    Point-in-time by design. Backtesting a setup against today's universe would
    only include instruments that survived to today: the ones that were delisted
    or lost liquidity disappear from the sample, and every strategy looks better
    than it is. The engine asks "who was in the universe on this date" instead.

    threshold_usd is stored per row so a later backtest can tell whether a result
    came from a different cut.
    """

    __tablename__ = "universe_membership"
    __table_args__ = (
        Index("ix_universe_membership_entered_exited", "entered_on", "exited_on"),
        CheckConstraint(
            "exited_on IS NULL OR exited_on > entered_on", name="exit_after_entry"
        ),
        CheckConstraint("median_dollar_volume >= 0", name="volume_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False
    )
    entered_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    exited_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    median_dollar_volume: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    threshold_usd: Mapped[Decimal] = mapped_column(PRICE, nullable=False)


class CorporateActionType(enum.StrEnum):
    """Event types that break price continuity."""

    REVERSE_SPLIT = "reverse_split"
    FORWARD_SPLIT = "forward_split"
    UNIT_SPLIT = "unit_split"
    SPIN_OFF = "spin_off"
    STOCK_MERGER = "stock_merger"


CORPORATE_ACTION_TYPE_ENUM = Enum(
    CorporateActionType,
    name="corporate_action_type",
    native_enum=True,
    values_callable=_enum_values,
)


class CorporateAction(Base):
    """A corporate action that breaks the price series.

    Recorded from the venue's published events, never inferred from price. A
    120-to-1 reverse split looks exactly like an 18x breakout in the bar data,
    and a failed clinical trial looks exactly like a spin-off: trying to tell
    them apart by price and volume thresholds misclassifies both. The events are
    published, so they are fetched rather than guessed.

    ratio is new shares per old share: 0.008352 for a 120-to-1 reverse split,
    4.0 for a 4-for-1 forward split. Null where the event changes the series
    without a single conversion factor, such as a spin-off.
    """

    __tablename__ = "corporate_action"
    __table_args__ = (
        UniqueConstraint("instrument_id", "effective_on", "action_type"),
        Index("ix_corporate_action_effective_on", "effective_on"),
        CheckConstraint("ratio IS NULL OR ratio > 0", name="ratio_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False
    )
    effective_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    action_type: Mapped[CorporateActionType] = mapped_column(
        CORPORATE_ACTION_TYPE_ENUM, nullable=False
    )
    ratio: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


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
