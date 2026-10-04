"""Tests for the completeness checks.

Each test plants the exact defect a check looks for. An audit that has never
been shown a real defect proves nothing: every one of these checks exists
because the corresponding problem was found in live data, silently, after the
data had already been loaded.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from guardrail.collectors.store import get_or_create_instrument, record_failure
from guardrail.db.models import AssetClass, Candle, CorporateAction, CorporateActionType
from guardrail.quality.completeness import (
    MAX_DORMANT_DAYS,
    Report,
    check_source_freshness,
    check_suspect_discontinuities,
    check_symbol_reuse,
)
from tests.constants import TEST_SYMBOL, TEST_VENUE

BASE = datetime(2025, 1, 6, tzinfo=UTC)


def _equity(session: Session, symbol: str = TEST_SYMBOL) -> int:
    instrument = get_or_create_instrument(
        session, symbol, TEST_VENUE, AssetClass.EQUITY
    )
    session.flush()
    return instrument.id


def _candle(session: Session, instrument_id: int, ts: datetime, close: str) -> None:
    price = Decimal(close)
    session.add(
        Candle(
            instrument_id=instrument_id,
            timeframe="1d",
            ts=ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("1000"),
        )
    )


def _findings(report: Report, check: str) -> list[str]:
    return [f.detail for f in report.findings if f.check == check]


def test_price_jump_without_corporate_action_is_reported(session: Session) -> None:
    """A split-sized gap with nothing on record."""
    iid = _equity(session)
    _candle(session, iid, BASE, "120.00")
    _candle(session, iid, BASE + timedelta(days=1), "10.00")
    session.flush()

    report = Report()
    check_suspect_discontinuities(session, report)

    assert any(TEST_SYMBOL in d for d in _findings(report, "suspect_discontinuity"))


def test_price_jump_with_corporate_action_is_not_reported(session: Session) -> None:
    """A recorded split explains the gap and must not raise an alarm.

    Without this, every legitimate split would be flagged and the check would
    become noise that nobody reads.
    """
    iid = _equity(session)
    _candle(session, iid, BASE, "120.00")
    _candle(session, iid, BASE + timedelta(days=1), "10.00")
    session.add(
        CorporateAction(
            instrument_id=iid,
            effective_on=BASE + timedelta(days=1),
            action_type=CorporateActionType.REVERSE_SPLIT,
            ratio=Decimal("0.2"),
            detail="reverse_splits",
        )
    )
    session.flush()

    report = Report()
    check_suspect_discontinuities(session, report)

    assert not any(TEST_SYMBOL in d for d in _findings(report, "suspect_discontinuity"))


def test_ordinary_move_is_not_reported(session: Session) -> None:
    """A real 82% drop on a failed trial must survive the filter.

    AMLX fell from 18.97 to 3.36 in one session on trial results. An earlier
    threshold of 3x flagged it, which would have hidden from the engine exactly
    the kind of move it exists to find.
    """
    iid = _equity(session)
    _candle(session, iid, BASE, "18.97")
    _candle(session, iid, BASE + timedelta(days=1), "3.36")
    session.flush()

    report = Report()
    check_suspect_discontinuities(session, report)

    assert not any(TEST_SYMBOL in d for d in _findings(report, "suspect_discontinuity"))


def test_dormant_then_resumed_symbol_is_reported(session: Session) -> None:
    """The JAN case: 616 days without bars, then a different company."""
    iid = _equity(session)
    _candle(session, iid, BASE, "10")
    _candle(session, iid, BASE + timedelta(days=MAX_DORMANT_DAYS + 30), "10")
    session.flush()

    report = Report()
    check_symbol_reuse(session, report)

    assert any(TEST_SYMBOL in d for d in _findings(report, "possible_symbol_reuse"))


def test_normal_weekend_gap_is_not_reported(session: Session) -> None:
    iid = _equity(session)
    _candle(session, iid, BASE, "10")
    _candle(session, iid, BASE + timedelta(days=3), "10")
    session.flush()

    report = Report()
    check_symbol_reuse(session, report)

    assert not any(TEST_SYMBOL in d for d in _findings(report, "possible_symbol_reuse"))


def test_failing_source_is_an_error(session: Session) -> None:
    """A collector dying quietly is this system's worst failure mode."""
    record_failure(session, "test.source", "connection refused")
    session.flush()

    report = Report()
    check_source_freshness(session, report)

    assert any("test.source" in f.detail for f in report.errors)
