"""Tests for the event impact statistics.

This is the function whose output the screen shows as a "projection", so the
expected figures are worked out by hand here, not read back from the code.

Hand calculation for the ten d1 returns used below, sorted:
    -0.10 -0.04 -0.02 -0.01 0.00 0.01 0.02 0.03 0.05 0.09
Position of a quantile q is (n - 1) * q = 9q, interpolated between neighbours.
    median   9 * 0.5 = 4.5 -> halfway between 0.00 and 0.01          =  0.005
    p10      9 * 0.1 = 0.9 -> -0.10 + 0.9 * (-0.04 - -0.10)          = -0.046
    p90      9 * 0.9 = 8.1 ->  0.05 + 0.1 * ( 0.09 -  0.05)          =  0.054
    share positive: 0.01 0.02 0.03 0.05 0.09 are > 0 (0.00 is not)   =  5/10
    absolute values sorted: 0 .01 .01 .02 .02 .03 .04 .05 .09 .10
    median of those 4.5 -> halfway between 0.02 and 0.03             =  0.025
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from guardrail.analysis.event_impact import (
    HORIZONS,
    MAX_BAR_GAP_DAYS,
    MIN_SAMPLE,
    Bar,
    Skip,
    Stats,
    is_displayable,
    measure,
    quantile,
    recompute,
    summarize,
)
from guardrail.db.models import (
    AssetClass,
    Candle,
    CorporateAction,
    CorporateActionType,
    EventImpact,
    EventImpactStat,
    EventKind,
    EventReferenceAsset,
    EventScope,
    Instrument,
    ScheduledEvent,
)
from tests.constants import TEST_SYMBOL, TEST_VENUE

D = Decimal
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
D1_RETURNS = [
    D(x)
    for x in [
        "-0.10",
        "-0.04",
        "-0.02",
        "-0.01",
        "0.00",
        "0.01",
        "0.02",
        "0.03",
        "0.05",
        "0.09",
    ]
]


# --- summary statistics ------------------------------------------------------


def test_summary_matches_the_hand_calculation() -> None:
    stats = summarize(D1_RETURNS)

    assert stats == Stats(
        n=10,
        median_signed=D("0.005"),
        median_abs=D("0.025"),
        p10=D("-0.046"),
        p90=D("0.054"),
        pct_positive=D("0.5"),
    )


def test_summary_does_not_depend_on_input_order() -> None:
    assert summarize(list(reversed(D1_RETURNS))) == summarize(D1_RETURNS)


def test_a_single_sample_is_every_quantile() -> None:
    stats = summarize([D("-0.03")])

    assert (stats.median_signed, stats.p10, stats.p90) == (D("-0.03"),) * 3
    assert (stats.median_abs, stats.pct_positive) == (D("0.03"), D("0"))


def test_empty_sample_has_no_figures() -> None:
    assert summarize([]) == Stats(0, None, None, None, None, None)


def test_two_samples_interpolate_between_them() -> None:
    # n=2: position (2-1)*0.5 = 0.5 -> halfway between 0.02 and 0.06.
    assert summarize([D("0.06"), D("0.02")]).median_signed == D("0.04")


@given(st.lists(st.decimals(-1, 1, places=6), min_size=1, max_size=40))
def test_quantiles_are_ordered_and_bounded(values: list[Decimal]) -> None:
    ordered = sorted(values)
    stats = summarize(values)

    assert stats.p10 is not None and stats.p90 is not None
    assert stats.median_signed is not None
    assert ordered[0] - D("1e-8") <= stats.p10 <= stats.median_signed + D("1e-8")
    assert stats.median_signed - D("1e-8") <= stats.p90 <= ordered[-1] + D("1e-8")


def test_quantile_rejects_an_empty_sample() -> None:
    with pytest.raises(ValueError):
        quantile([], D("0.5"))


def test_sample_size_threshold() -> None:
    assert MIN_SAMPLE == 8
    assert not is_displayable(7)
    assert is_displayable(8)


# --- measuring one event -----------------------------------------------------

EVENT = date(2026, 3, 10)  # a Tuesday


def _bars(*pairs: tuple[str, str]) -> list[Bar]:
    return [Bar(date.fromisoformat(d), D(c)) for d, c in pairs]


# Base is Monday 100; after the event: Wed 104, Thu 110, Fri 95, Mon 99, Tue 120.
CLEAN = _bars(
    ("2026-03-06", "98"),
    ("2026-03-09", "100"),
    ("2026-03-10", "101"),
    ("2026-03-11", "104"),
    ("2026-03-12", "110"),
    ("2026-03-13", "95"),
    ("2026-03-16", "99"),
    ("2026-03-17", "120"),
)


def test_horizons_use_the_first_third_and_fifth_bar_after_the_event() -> None:
    assert HORIZONS == {"d1": 1, "d3": 3, "w1": 5}
    assert measure(CLEAN, EVENT, 1).ret == D("0.04")  # 104 / 100 - 1
    assert measure(CLEAN, EVENT, 3).ret == D("-0.05")  # 95 / 100 - 1
    assert measure(CLEAN, EVENT, 5).ret == D("0.2")  # 120 / 100 - 1


def test_event_on_a_weekend_uses_friday_as_base_and_monday_as_first_after() -> None:
    bars = _bars(("2026-03-06", "100"), ("2026-03-09", "103"), ("2026-03-10", "104"))

    assert measure(bars, date(2026, 3, 7), 1).ret == D("0.03")


def test_event_before_the_first_bar_has_no_base() -> None:
    sample = measure(CLEAN, date(2026, 3, 1), 1)

    assert (sample.ret, sample.skip) == (None, Skip.NO_BASE)


def test_not_enough_bars_after_the_event_has_no_target() -> None:
    assert measure(CLEAN, date(2026, 3, 16), 3).skip is Skip.NO_TARGET
    assert measure(CLEAN, date(2026, 3, 17), 1).skip is Skip.NO_TARGET


def test_corporate_action_inside_the_window_drops_the_sample() -> None:
    assert measure(CLEAN, EVENT, 1, {date(2026, 3, 11)}).skip is Skip.CORPORATE_ACTION
    # On the base day itself: still inside the window, dropped on purpose.
    assert measure(CLEAN, EVENT, 1, {date(2026, 3, 9)}).skip is Skip.CORPORATE_ACTION


def test_corporate_action_outside_the_window_is_ignored() -> None:
    assert measure(CLEAN, EVENT, 1, {date(2026, 3, 6), date(2026, 3, 12)}).ret == D(
        "0.04"
    )


def test_a_move_above_eight_times_is_dropped_but_exactly_eight_is_kept() -> None:
    kept = _bars(("2026-03-09", "10"), ("2026-03-10", "10"), ("2026-03-11", "80"))
    dropped = _bars(("2026-03-09", "10"), ("2026-03-10", "10"), ("2026-03-11", "80.01"))
    fall = _bars(("2026-03-09", "80"), ("2026-03-10", "80"), ("2026-03-11", "9.99"))

    assert measure(kept, EVENT, 1).ret == D("7")
    assert measure(dropped, EVENT, 1).skip is Skip.PRICE_JUMP
    assert measure(fall, EVENT, 1).skip is Skip.PRICE_JUMP


def test_a_jump_on_the_event_day_itself_is_caught() -> None:
    """The event-day bar is not an endpoint but lies inside the window."""
    bars = _bars(("2026-03-09", "10"), ("2026-03-10", "200"), ("2026-03-11", "200"))

    assert measure(bars, EVENT, 1).skip is Skip.PRICE_JUMP


def test_gap_of_six_days_is_normal_and_seven_is_a_hole() -> None:
    six = _bars(("2026-03-03", "100"), ("2026-03-09", "100"), ("2026-03-11", "101"))
    seven = _bars(("2026-03-09", "100"), ("2026-03-10", "100"), ("2026-03-17", "101"))

    assert MAX_BAR_GAP_DAYS == 6
    assert measure(six, EVENT, 1).ret == D("0.01")
    assert measure(seven, EVENT, 1).skip is Skip.HOLE


def test_a_code_that_came_back_after_over_180_days_is_dormant() -> None:
    back = _bars(("2026-03-09", "100"), ("2026-03-10", "100"), ("2026-09-07", "101"))
    edge = _bars(("2026-03-09", "100"), ("2026-03-10", "100"), ("2026-09-06", "101"))

    assert measure(back, EVENT, 1).skip is Skip.DORMANT  # 181 days
    assert measure(edge, EVENT, 1).skip is Skip.HOLE  # 180 days: a hole, not dormancy


# --- the stored table --------------------------------------------------------

BLOCK = 14  # days between events, so no two windows share a bar
FIRST_EVENT = date(2026, 1, 12)


def _instrument(session: Session, symbol: str = TEST_SYMBOL) -> Instrument:
    instrument = Instrument(
        symbol=symbol, venue=TEST_VENUE, asset_class=AssetClass.CRYPTO
    )
    session.add(instrument)
    session.flush()
    return instrument


def _bar(session: Session, instrument: Instrument, day: date, price: Decimal) -> None:
    session.add(
        Candle(
            instrument_id=instrument.id,
            timeframe="1d",
            ts=datetime(day.year, day.month, day.day, tzinfo=UTC),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=D("1"),
        )
    )


def _event(
    day: date,
    kind: EventKind,
    key: str,
    instrument_id: int | None = None,
    scope: EventScope = EventScope.US,
    impact: EventImpact = EventImpact.HIGH,
) -> ScheduledEvent:
    return ScheduledEvent(
        source="test-source",
        kind=kind,
        scope=scope,
        instrument_id=instrument_id,
        title=f"event {key}",
        starts_at=datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
        has_time=True,
        impact=impact,
        payload={},
        identity_key=key,
    )


def _build(session: Session, count: int = 10) -> tuple[Instrument, list[date]]:
    """Ten events; around event i the close moves by r_i, 2 r_i and 3 r_i.

    Base (day before) is always 100, so d1 / d3 / w1 returns are r, 2r and 3r.
    """
    session.execute(delete(ScheduledEvent))
    session.execute(delete(EventReferenceAsset))
    instrument = _instrument(session)
    days: list[date] = []
    for i, r in enumerate(D1_RETURNS[:count]):
        event = FIRST_EVENT + timedelta(days=BLOCK * i)
        days.append(event)
        _bar(session, instrument, event - timedelta(days=1), D(100))
        _bar(session, instrument, event, D(100))
        _bar(session, instrument, event + timedelta(days=1), 100 * (1 + r))
        _bar(session, instrument, event + timedelta(days=2), 100 * (1 + r))
        _bar(session, instrument, event + timedelta(days=3), 100 * (1 + 2 * r))
        _bar(session, instrument, event + timedelta(days=4), 100 * (1 + 2 * r))
        _bar(session, instrument, event + timedelta(days=5), 100 * (1 + 3 * r))
        session.add(_event(event, EventKind.EARNINGS, f"{i:032x}", instrument.id))
    session.flush()
    return instrument, days


def _stats(
    session: Session, instrument: Instrument
) -> dict[tuple[str, str], EventImpactStat]:
    session.expire_all()
    rows = session.execute(
        select(EventImpactStat).where(EventImpactStat.instrument_id == instrument.id)
    ).scalars()
    return {(r.kind.value, r.horizon): r for r in rows}


def test_table_matches_the_hand_calculation_for_every_horizon(session: Session) -> None:
    instrument, days = _build(session)

    report = recompute(session, NOW)

    rows = _stats(session, instrument)
    assert set(rows) == {("earnings", "d1"), ("earnings", "d3"), ("earnings", "w1")}
    for horizon, factor in (("d1", 1), ("d3", 2), ("w1", 3)):
        row = rows[("earnings", horizon)]
        assert row.n == 10
        assert row.median_signed == D("0.005") * factor
        assert row.p10 == D("-0.046") * factor
        assert row.p90 == D("0.054") * factor
        assert row.median_abs == D("0.025") * factor
        assert row.pct_positive == D("0.5")
        assert (row.window_start, row.window_end) == (days[0], days[-1])
        assert row.computed_at == NOW
    assert report.samples == 30


def test_recomputation_is_idempotent(session: Session) -> None:
    instrument, _ = _build(session)

    recompute(session, NOW)
    first = {
        k: (r.n, r.median_signed, r.p10, r.p90)
        for k, r in _stats(session, instrument).items()
    }
    recompute(session, NOW)
    second = {
        k: (r.n, r.median_signed, r.p10, r.p90)
        for k, r in _stats(session, instrument).items()
    }

    assert first == second
    assert len(second) == 3


def test_small_sample_is_stored_and_marked_insufficient(session: Session) -> None:
    instrument, _ = _build(session, count=5)

    recompute(session, NOW)

    row = _stats(session, instrument)[("earnings", "d1")]
    assert row.n == 5
    assert not is_displayable(row.n)


def test_two_events_on_one_day_count_once(session: Session) -> None:
    """CPI and Core CPI come out together; that is one market reaction."""
    instrument, days = _build(session)
    session.add(_event(days[0], EventKind.EARNINGS, "f" * 32, instrument.id))
    session.flush()

    recompute(session, NOW)

    assert _stats(session, instrument)[("earnings", "d1")].n == 10


def test_a_window_with_a_corporate_action_is_left_out(session: Session) -> None:
    instrument, days = _build(session)
    session.add(
        CorporateAction(
            instrument_id=instrument.id,
            effective_on=datetime(days[3].year, days[3].month, days[3].day, tzinfo=UTC),
            action_type=CorporateActionType.FORWARD_SPLIT,
            ratio=D("2"),
        )
    )
    session.flush()

    report = recompute(session, NOW)

    assert _stats(session, instrument)[("earnings", "d1")].n == 9
    assert report.skipped["corporate_action"] == 3  # one per horizon


def test_future_events_are_not_measured(session: Session) -> None:
    instrument, _ = _build(session)
    session.add(
        _event(
            NOW.date() + timedelta(days=3), EventKind.EARNINGS, "e" * 32, instrument.id
        )
    )
    session.flush()

    recompute(session, NOW)

    assert _stats(session, instrument)[("earnings", "d1")].n == 10


def test_market_wide_events_are_measured_against_the_reference_assets(
    session: Session,
) -> None:
    instrument, days = _build(session)
    session.execute(delete(ScheduledEvent))
    session.add_all(
        [
            EventReferenceAsset(venue=TEST_VENUE, symbol=TEST_SYMBOL),
            EventReferenceAsset(venue="fx", symbol="ZZZZZZ"),
        ]
    )
    # High impact, US: measured. The other three must be ignored.
    session.add(_event(days[0], EventKind.INFLATION_PRINT, "a" * 32))
    session.add(
        _event(days[1], EventKind.INFLATION_PRINT, "b" * 32, impact=EventImpact.LOW)
    )
    session.add(
        _event(days[2], EventKind.INFLATION_PRINT, "c" * 32, scope=EventScope.GLOBAL)
    )
    session.add(_event(days[3], EventKind.HOLIDAY, "d" * 32))
    session.flush()

    report = recompute(session, NOW)

    rows = _stats(session, instrument)
    assert rows[("inflation_print", "d1")].n == 1
    assert rows[("inflation_print", "d1")].median_signed == D("-0.10")
    assert not any(kind == "holiday" for kind, _ in rows)
    assert report.absent_reference == ["fx:ZZZZZZ"]


def test_empty_database_gives_an_empty_table(session: Session) -> None:
    session.execute(delete(ScheduledEvent))
    session.execute(delete(EventReferenceAsset))

    report = recompute(session, NOW)

    assert (report.rows, report.samples) == (0, 0)
    assert session.execute(select(EventImpactStat)).first() is None
