"""Persistence helpers for ingested market data.

Writes are idempotent by design: a collector that re-runs over a window it has
already covered must be a no-op, not an error. That property is what makes it
safe to re-run a backfill after an interruption.
"""

from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.collectors.binance import Kline
from guardrail.collectors.binance_vision import MetricRow
from guardrail.collectors.funding import FundingRate
from guardrail.db.models import (
    AssetClass,
    Candle,
    DerivativeStat,
    Instrument,
    SourceHealth,
)

log = structlog.get_logger(__name__)

# Rows per INSERT. Large enough to keep round trips low, small enough that a
# three-year backfill does not build one enormous statement in memory.
BATCH_SIZE = 1000


def get_or_create_instrument(
    session: Session,
    symbol: str,
    venue: str,
    asset_class: AssetClass,
) -> Instrument:
    """Return the instrument row, inserting it if absent."""
    existing = session.execute(
        select(Instrument).where(Instrument.venue == venue, Instrument.symbol == symbol)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    instrument = Instrument(symbol=symbol, venue=venue, asset_class=asset_class)
    session.add(instrument)
    session.flush()
    log.info("instrument.created", symbol=symbol, venue=venue)
    return instrument


def _chunks(
    items: Sequence[dict[str, object]], size: int
) -> Iterator[Sequence[dict[str, object]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def store_klines(
    session: Session,
    instrument_id: int,
    timeframe: str,
    klines: Iterable[Kline],
) -> int:
    """Insert klines, skipping any already stored. Returns rows actually added.

    ON CONFLICT DO NOTHING lets the database decide what is new, in one
    statement, instead of reading existing rows first and racing against a
    concurrent writer.

    The return value is inserted rows, not submitted rows. The gap between the
    two is what the completeness check reads.
    """
    rows: list[dict[str, object]] = [
        {
            "instrument_id": instrument_id,
            "timeframe": timeframe,
            "ts": kline.open_time,
            "open": kline.open,
            "high": kline.high,
            "low": kline.low,
            "close": kline.close,
            "volume": kline.volume,
        }
        for kline in klines
    ]
    if not rows:
        return 0

    inserted = 0
    for chunk in _chunks(rows, BATCH_SIZE):
        # RETURNING, not rowcount: psycopg 3 reports -1 ("unknown") for a
        # multi-row INSERT, so the count has to come from the database itself.
        # RETURNING emits one row per row actually inserted, skipping conflicts.
        statement = (
            pg_insert(Candle)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["instrument_id", "timeframe", "ts"])
            .returning(Candle.ts)
        )
        inserted += len(session.execute(statement).fetchall())

    log.info(
        "candles.stored",
        instrument_id=instrument_id,
        timeframe=timeframe,
        submitted=len(rows),
        inserted=inserted,
    )
    return inserted


def record_success(session: Session, source: str) -> None:
    """Mark a source as healthy and reset its failure counter."""
    statement = (
        pg_insert(SourceHealth)
        .values(
            source=source,
            last_success_at=datetime.now(UTC),
            consecutive_failures=0,
        )
        .on_conflict_do_update(
            index_elements=["source"],
            set_={"last_success_at": datetime.now(UTC), "consecutive_failures": 0},
        )
    )
    session.execute(statement)


def record_failure(session: Session, source: str, error: str) -> None:
    """Record a failure and increment the consecutive failure counter.

    The counter is what the circuit breaker and the staleness alert read. A
    collector dying silently is the worst failure mode of this system, so the
    failure path must write just as reliably as the success path.
    """
    now = datetime.now(UTC)
    statement = (
        pg_insert(SourceHealth)
        .values(
            source=source,
            last_error_at=now,
            last_error=error[:2000],
            consecutive_failures=1,
        )
        .on_conflict_do_update(
            index_elements=["source"],
            set_={
                "last_error_at": now,
                "last_error": error[:2000],
                "consecutive_failures": SourceHealth.consecutive_failures + 1,
            },
        )
    )
    session.execute(statement)


def store_metrics(
    session: Session,
    instrument_id: int,
    rows: Iterable[MetricRow],
) -> int:
    """Insert derivative metrics, filling gaps left by other sources.

    Unlike candles, the same timestamp can legitimately arrive from two sources:
    the Binance Vision dump, which carries the positioning ratios, and the REST
    endpoint, which carries open interest only. DO NOTHING would permanently drop
    the ratios whenever the REST collector happened to write first.

    COALESCE on every column means each source fills what the other lacked and
    neither can overwrite a value already present.
    """
    values: list[dict[str, object]] = [
        {
            "instrument_id": instrument_id,
            "ts": row.ts,
            "open_interest": row.open_interest,
            "open_interest_value": row.open_interest_value,
            "toptrader_long_short_account_ratio": (
                row.toptrader_long_short_account_ratio
            ),
            "toptrader_long_short_position_ratio": (
                row.toptrader_long_short_position_ratio
            ),
            "taker_long_short_volume_ratio": row.taker_long_short_volume_ratio,
        }
        for row in rows
    ]
    if not values:
        return 0

    fillable = (
        "open_interest",
        "open_interest_value",
        "toptrader_long_short_account_ratio",
        "toptrader_long_short_position_ratio",
        "taker_long_short_volume_ratio",
    )

    written = 0
    for chunk in _chunks(values, BATCH_SIZE):
        insert_stmt = pg_insert(DerivativeStat).values(chunk)
        statement = insert_stmt.on_conflict_do_update(
            index_elements=["instrument_id", "ts"],
            set_={
                column: func.coalesce(
                    getattr(DerivativeStat, column),
                    getattr(insert_stmt.excluded, column),
                )
                for column in fillable
            },
        ).returning(DerivativeStat.ts)
        written += len(session.execute(statement).fetchall())

    log.info(
        "metrics.stored",
        instrument_id=instrument_id,
        submitted=len(values),
        written=written,
    )
    return written


def store_funding(
    session: Session,
    instrument_id: int,
    rows: Iterable[FundingRate],
) -> int:
    """Insert funding settlements into the shared derivative_stat table.

    Funding settles every 8 hours while open interest samples every 5 minutes, so
    most rows land on distinct timestamps and the rest coincide exactly on the
    8-hour marks. COALESCE keeps whichever value is already present, so neither
    collector can blank the other's columns regardless of write order.
    """
    values: list[dict[str, object]] = [
        {
            "instrument_id": instrument_id,
            "ts": row.funding_time,
            "funding_rate": row.funding_rate,
            "mark_price": row.mark_price,
        }
        for row in rows
    ]
    if not values:
        return 0

    written = 0
    for chunk in _chunks(values, BATCH_SIZE):
        insert_stmt = pg_insert(DerivativeStat).values(chunk)
        statement = insert_stmt.on_conflict_do_update(
            index_elements=["instrument_id", "ts"],
            set_={
                column: func.coalesce(
                    getattr(DerivativeStat, column),
                    getattr(insert_stmt.excluded, column),
                )
                for column in ("funding_rate", "mark_price")
            },
        ).returning(DerivativeStat.ts)
        written += len(session.execute(statement).fetchall())

    log.info(
        "funding.stored",
        instrument_id=instrument_id,
        submitted=len(values),
        written=written,
    )
    return written
