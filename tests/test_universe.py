"""Tests for universe construction.

The membership history is what makes a point-in-time backtest possible: without
it, testing against today's universe would only include symbols that survived to
today, and every strategy would look better than it is. These tests pin the two
behaviours that history depends on — no duplicate rows on a repeated run, and a
closed membership when a symbol stops qualifying.

They run inside the fixture's rolled-back transaction. That matters more than
usual here: apply_universe treats every symbol absent from its input as having
left the universe, so a test that wrote outside the transaction would close every
real membership and nothing would undo it.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import DailyBar
from guardrail.collectors.universe import (
    MIN_BARS,
    _median_dollar_volume,
    apply_universe,
)
from guardrail.db.models import Instrument, UniverseMembership

THRESHOLD = Decimal("20000000")
SYMBOL = "ZZTESTX"


def _bars(n: int, volume: str = "1000") -> list[DailyBar]:
    start = date(2026, 1, 1)
    price = Decimal("100")
    return [
        DailyBar(
            symbol=SYMBOL,
            day=start + timedelta(days=i),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal(volume),
        )
        for i in range(n)
    ]


def _memberships(session: Session, symbol: str) -> list[UniverseMembership]:
    return list(
        session.execute(
            select(UniverseMembership)
            .join(Instrument, Instrument.id == UniverseMembership.instrument_id)
            .where(Instrument.symbol == symbol)
        ).scalars()
    )


def test_median_needs_enough_bars() -> None:
    """A symbol that traded a handful of sessions has no stable liquidity."""
    assert _median_dollar_volume(_bars(MIN_BARS - 1)) is None
    assert _median_dollar_volume(_bars(MIN_BARS)) is not None


def test_median_ignores_a_single_outlier_session() -> None:
    """Median, not mean: one news day can multiply volume twenty times.

    A mean would admit an illiquid symbol on the strength of one session.
    """
    bars = _bars(MIN_BARS)
    spiked = list(bars)
    price = Decimal("100")
    spiked[0] = DailyBar(
        symbol=SYMBOL,
        day=bars[0].day,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000000"),
    )

    assert _median_dollar_volume(spiked) == _median_dollar_volume(bars)


def test_symbol_above_threshold_enters(session: Session) -> None:
    change = apply_universe(session, {SYMBOL: Decimal("50000000")}, THRESHOLD)

    assert change.added == 1
    assert change.passed == 1
    assert len(_memberships(session, SYMBOL)) == 1


def test_symbol_below_threshold_does_not_enter(session: Session) -> None:
    change = apply_universe(session, {SYMBOL: Decimal("1000000")}, THRESHOLD)

    assert change.added == 0
    assert change.passed == 0
    assert _memberships(session, SYMBOL) == []


def test_rerun_does_not_duplicate_membership(session: Session) -> None:
    """The core guarantee: one row per continuous membership."""
    liquidity = {SYMBOL: Decimal("50000000")}
    apply_universe(session, liquidity, THRESHOLD)
    change = apply_universe(session, liquidity, THRESHOLD)

    assert change.added == 0
    assert change.unchanged == 1

    total = session.execute(
        select(func.count())
        .select_from(UniverseMembership)
        .join(Instrument, Instrument.id == UniverseMembership.instrument_id)
        .where(Instrument.symbol == SYMBOL)
    ).scalar_one()
    assert total == 1


def test_symbol_losing_liquidity_is_closed_not_deleted(session: Session) -> None:
    """Exit is recorded, never erased: the backtest reads the past."""
    apply_universe(session, {SYMBOL: Decimal("50000000")}, THRESHOLD)
    change = apply_universe(session, {SYMBOL: Decimal("1000000")}, THRESHOLD)

    assert change.removed == 1

    rows = _memberships(session, SYMBOL)
    assert len(rows) == 1
    assert rows[0].exited_on is not None
    assert rows[0].exited_on > rows[0].entered_on
