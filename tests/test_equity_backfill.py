"""Tests for the equity backfill.

resume_points is the piece that decides what gets fetched. Its first version
looked only at the newest stored bar, so after any short run every symbol
appeared up to date and the earlier history was never fetched — no error, no
failing test, just a backfill that quietly did nothing.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import DailyBar
from guardrail.collectors.equity_backfill import (
    TIMEFRAME,
    _bar_to_kline,
    load_universe,
    resume_points,
)
from guardrail.collectors.store import get_or_create_instrument, store_klines
from guardrail.collectors.universe import apply_universe
from guardrail.db.models import AssetClass
from tests.constants import TEST_SYMBOL, TEST_VENUE

DEFAULT_START = date(2023, 10, 1)


def _bar(day: date) -> DailyBar:
    price = Decimal("100")
    return DailyBar(
        symbol=TEST_SYMBOL,
        day=day,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
    )


def _store(session: Session, instrument_id: int, days: list[date]) -> None:
    store_klines(
        session, instrument_id, TIMEFRAME, [_bar_to_kline(_bar(d)) for d in days]
    )


def test_bar_converts_to_a_full_day_kline() -> None:
    """The stored bar must span the whole UTC day and keep exact decimals."""
    kline = _bar_to_kline(_bar(date(2026, 3, 15)))

    assert kline.open_time == datetime(2026, 3, 15, tzinfo=UTC)
    assert kline.close_time.date() == date(2026, 3, 15)
    assert kline.close_time.hour == 23
    assert kline.close == Decimal("100")


def test_instrument_without_bars_starts_from_default(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )

    points = resume_points(session, [instrument.id], DEFAULT_START)

    assert points[instrument.id] == DEFAULT_START


def test_instrument_with_full_history_resumes_after_newest(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )
    _store(session, instrument.id, [DEFAULT_START, DEFAULT_START + timedelta(days=1)])
    session.flush()

    points = resume_points(session, [instrument.id], DEFAULT_START)

    assert points[instrument.id] == DEFAULT_START + timedelta(days=2)


def test_gap_behind_oldest_restarts_from_default(session: Session) -> None:
    """The bug this test exists for.

    A short run writes only recent bars. Resuming from the newest one would skip
    everything before them, permanently.
    """
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )
    recent = date(2026, 9, 1)
    _store(session, instrument.id, [recent, recent + timedelta(days=1)])
    session.flush()

    points = resume_points(session, [instrument.id], DEFAULT_START)

    assert points[instrument.id] == DEFAULT_START


def test_load_universe_returns_only_open_memberships(session: Session) -> None:
    apply_universe(session, {TEST_SYMBOL: Decimal("50000000")}, Decimal("20000000"))
    session.flush()

    assert TEST_SYMBOL in load_universe(session)

    apply_universe(session, {TEST_SYMBOL: Decimal("1000000")}, Decimal("20000000"))
    session.flush()

    assert TEST_SYMBOL not in load_universe(session)
