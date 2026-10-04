"""Tests for market data persistence.

The property under test is idempotency: re-running a collector over a window it
already covered must insert nothing and raise nothing. Without it, recovering
from an interrupted backfill would require manual cleanup.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from guardrail.collectors.binance import Kline
from guardrail.collectors.binance_vision import MetricRow
from guardrail.collectors.store import (
    get_or_create_instrument,
    record_failure,
    record_success,
    store_klines,
    store_metrics,
)
from guardrail.db.models import AssetClass, Candle, DerivativeStat, SourceHealth
from tests.constants import TEST_SYMBOL, TEST_VENUE

START = datetime(2026, 1, 1, tzinfo=UTC)


def _kline(offset_hours: int, close: str = "100.5") -> Kline:
    open_time = START + timedelta(hours=offset_hours)
    return Kline(
        open_time=open_time,
        close_time=open_time + timedelta(hours=1) - timedelta(milliseconds=1),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal("1234.5"),
    )


def _count_candles(session: Session, instrument_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(Candle)
        .where(Candle.instrument_id == instrument_id)
    ).scalar_one()


def test_instrument_is_not_duplicated(session: Session) -> None:
    first = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    second = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )

    assert first.id == second.id


def test_stores_klines(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    klines = [_kline(i) for i in range(5)]

    inserted = store_klines(session, instrument.id, "1h", klines)

    assert inserted == 5
    assert _count_candles(session, instrument.id) == 5


def test_rerun_inserts_nothing_and_does_not_raise(session: Session) -> None:
    """The core idempotency guarantee: a repeated run is a no-op."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    klines = [_kline(i) for i in range(5)]
    store_klines(session, instrument.id, "1h", klines)

    inserted_again = store_klines(session, instrument.id, "1h", klines)

    assert inserted_again == 0
    assert _count_candles(session, instrument.id) == 5


def test_partial_overlap_inserts_only_new_rows(session: Session) -> None:
    """The realistic case: a scheduled run overlapping the previous window."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    store_klines(session, instrument.id, "1h", [_kline(i) for i in range(5)])

    inserted = store_klines(
        session, instrument.id, "1h", [_kline(i) for i in range(3, 8)]
    )

    assert inserted == 3
    assert _count_candles(session, instrument.id) == 8


def test_empty_batch_is_safe(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )

    assert store_klines(session, instrument.id, "1h", []) == 0


def test_timeframes_do_not_collide(session: Session) -> None:
    """Same instrument and timestamp across timeframes must coexist."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    store_klines(session, instrument.id, "1h", [_kline(0)])
    store_klines(session, instrument.id, "4h", [_kline(0)])

    assert _count_candles(session, instrument.id) == 2


def test_failure_counter_increments(session: Session) -> None:
    """A silently dying collector is this system's worst failure mode."""
    record_failure(session, "binance.klines", "connection reset")
    record_failure(session, "binance.klines", "connection reset")
    session.flush()

    health = session.get(SourceHealth, "binance.klines")
    assert health is not None
    assert health.consecutive_failures == 2
    assert health.last_error == "connection reset"


def test_success_resets_failure_counter(session: Session) -> None:
    record_failure(session, "binance.klines", "timeout")
    record_failure(session, "binance.klines", "timeout")
    record_success(session, "binance.klines")
    session.flush()

    health = session.get(SourceHealth, "binance.klines")
    assert health is not None
    assert health.consecutive_failures == 0
    assert health.last_success_at is not None


def _metric(offset_minutes: int, oi: str = "1000", ratios: bool = True) -> MetricRow:
    return MetricRow(
        ts=START + timedelta(minutes=offset_minutes),
        open_interest=Decimal(oi),
        open_interest_value=Decimal("50000"),
        toptrader_long_short_account_ratio=Decimal("2.5") if ratios else None,
        toptrader_long_short_position_ratio=Decimal("1.6") if ratios else None,
        taker_long_short_volume_ratio=Decimal("1.1") if ratios else None,
    )


def test_stores_metrics(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )

    written = store_metrics(session, instrument.id, [_metric(i * 5) for i in range(3)])

    assert written == 3


def test_rest_source_does_not_erase_dump_ratios(session: Session) -> None:
    """The reason this uses DO UPDATE instead of DO NOTHING.

    The REST endpoint carries no positioning ratios. If it writes after the dump,
    it must not blank what the dump already stored.
    """
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    store_metrics(session, instrument.id, [_metric(0)])

    store_metrics(session, instrument.id, [_metric(0, oi="999", ratios=False)])
    session.flush()
    session.expire_all()

    stored = session.get(DerivativeStat, (instrument.id, START))
    assert stored is not None
    assert stored.toptrader_long_short_account_ratio == Decimal("2.5")
    assert stored.open_interest == Decimal("1000")


def test_dump_fills_gaps_left_by_rest_source(session: Session) -> None:
    """And the reverse: the dump must fill ratios the REST row left null."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )
    store_metrics(session, instrument.id, [_metric(0, ratios=False)])

    store_metrics(session, instrument.id, [_metric(0)])
    session.flush()
    session.expire_all()

    stored = session.get(DerivativeStat, (instrument.id, START))
    assert stored is not None
    assert stored.toptrader_long_short_account_ratio == Decimal("2.5")


def test_empty_metrics_batch_is_safe(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    )

    assert store_metrics(session, instrument.id, []) == 0
