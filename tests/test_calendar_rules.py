"""Tests for rule-based events: expiries and B3 closed days.

Dates are worked out by hand from the calendar (2026-10-01 is a Thursday, so the
Fridays of October 2026 are the 2nd, 9th, 16th, 23rd and 30th).
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from guardrail.collectors import calendar_rules, us_calendar
from guardrail.collectors.calendar_rules import (
    b3_records,
    collect,
    expiry_records,
    futures_expiry,
    load_b3_table,
    options_expiry,
    third_friday,
    us_closed_days,
)
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.events import EventRecord, upsert_events
from guardrail.db.models import EventImpact, EventKind, EventScope, ScheduledEvent

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
NONE: frozenset[date] = frozenset()


@pytest.fixture(autouse=True)
def _private_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calendar_rules, "SOURCE", "manual-test")
    monkeypatch.setattr(us_calendar, "SOURCE", "alpaca-test")


def _mine() -> Select[ScheduledEvent]:
    return select(ScheduledEvent).where(ScheduledEvent.source == "manual-test")


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2026, 10, date(2026, 10, 16)),
        (2026, 11, date(2026, 11, 20)),
        (2026, 12, date(2026, 12, 18)),
        (2027, 1, date(2027, 1, 15)),
    ],
)
def test_third_friday(year: int, month: int, expected: date) -> None:
    assert third_friday(year, month) == expected
    assert expected.weekday() == 4


def test_options_expiry_moves_back_when_the_friday_is_closed() -> None:
    assert options_expiry(2026, 10, frozenset({date(2026, 10, 16)})) == date(
        2026, 10, 15
    )


def test_options_expiry_skips_a_closed_thursday_too() -> None:
    closed = frozenset({date(2026, 10, 16), date(2026, 10, 15)})

    assert options_expiry(2026, 10, closed) == date(2026, 10, 14)


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2026, 10, date(2026, 10, 30)),  # the 31st is a Saturday
        (2026, 11, date(2026, 11, 30)),  # a Monday
        (2026, 12, date(2026, 12, 31)),  # a Thursday
        (2026, 9, date(2026, 9, 30)),
    ],
)
def test_futures_expiry_is_the_last_business_day(
    year: int, month: int, expected: date
) -> None:
    assert futures_expiry(year, month, NONE) == expected


def test_futures_expiry_steps_over_a_closed_last_day() -> None:
    assert futures_expiry(2026, 12, frozenset({date(2026, 12, 31)})) == date(
        2026, 12, 30
    )


def test_futures_expiry_handles_the_year_boundary() -> None:
    assert futures_expiry(2026, 12, NONE).year == 2026


def test_window_of_six_months_lists_each_expiry_once() -> None:
    records = expiry_records(date(2026, 10, 15), date(2027, 4, 13), NONE)

    options = [
        r.starts_at.date() for r in records if r.kind is EventKind.OPTIONS_EXPIRY
    ]
    futures = [
        r.starts_at.date() for r in records if r.kind is EventKind.FUTURES_EXPIRY
    ]
    # April's third Friday (the 16th) falls after the window closes.
    assert options == [
        date(2026, 10, 16),
        date(2026, 11, 20),
        date(2026, 12, 18),
        date(2027, 1, 15),
        date(2027, 2, 19),
        date(2027, 3, 19),
    ]
    assert futures == [
        date(2026, 10, 30),
        date(2026, 11, 30),
        date(2026, 12, 31),
        date(2027, 1, 29),
        date(2027, 2, 26),
        date(2027, 3, 31),
    ]
    assert len({r.identity_key for r in records}) == len(records)


def test_expiry_outside_the_window_is_left_out() -> None:
    # October's third Friday (16th) is before the window opens.
    records = expiry_records(date(2026, 10, 17), date(2026, 10, 31), NONE)

    assert [r.kind for r in records] == [EventKind.FUTURES_EXPIRY]


def test_quarterly_options_expiry_weighs_more() -> None:
    records = expiry_records(date(2026, 9, 1), date(2026, 10, 31), NONE)

    by_month = {
        r.starts_at.month: r.impact
        for r in records
        if r.kind is EventKind.OPTIONS_EXPIRY
    }
    assert by_month == {9: EventImpact.MEDIUM, 10: EventImpact.LOW}


def test_expiry_records_are_date_only_us_events() -> None:
    record = expiry_records(date(2026, 10, 15), date(2026, 10, 31), NONE)[0]

    assert (record.has_time, record.scope, record.source) == (
        False,
        EventScope.US,
        "manual-test",
    )
    assert record.starts_at == datetime(2026, 10, 16, tzinfo=UTC)


# --- B3 table ----------------------------------------------------------------


def test_shipped_b3_table_is_readable_and_sorted_sane() -> None:
    table = load_b3_table()

    days = [d for d, _ in table[2026]]
    assert len(days) == 14
    assert all(d.year == 2026 for d in days)
    assert days == sorted(days)
    assert date(2026, 12, 25) in days


def test_b3_holidays_in_the_window_become_events() -> None:
    records, missing = b3_records(
        load_b3_table(), date(2026, 10, 15), date(2026, 12, 31)
    )

    assert [r.starts_at.date() for r in records] == [
        date(2026, 11, 2),
        date(2026, 11, 20),
        date(2026, 12, 24),
        date(2026, 12, 25),
        date(2026, 12, 31),
    ]
    assert all(
        r.scope is EventScope.BR and r.kind is EventKind.HOLIDAY for r in records
    )
    assert missing == []


def test_missing_year_is_reported_only_when_close() -> None:
    table = load_b3_table()

    _, early = b3_records(table, date(2026, 10, 15), date(2027, 4, 13))
    _, late = b3_records(table, date(2026, 11, 2), date(2027, 4, 13))

    assert early == []
    assert late == [2027]


def test_unreadable_b3_table_raises(tmp_path: Path) -> None:
    bad = tmp_path / "t.json"
    bad.write_text("{not json")
    with pytest.raises(UpstreamDataError, match="unreadable"):
        load_b3_table(bad)

    bad.write_text(
        json.dumps({"years": {"2026": [{"date": "2026-13-45", "name": "x"}]}})
    )
    with pytest.raises(UpstreamDataError):
        load_b3_table(bad)

    with pytest.raises(UpstreamDataError):
        load_b3_table(tmp_path / "absent.json")


# --- storage -----------------------------------------------------------------


def _us_closed(session: Session, day: date) -> None:
    starts = datetime(day.year, day.month, day.day, tzinfo=UTC)
    upsert_events(
        session,
        [
            EventRecord(
                source="alpaca-test",
                kind=EventKind.HOLIDAY,
                scope=EventScope.US,
                title=us_calendar.CLOSED_TITLE,
                starts_at=starts,
                has_time=False,
                impact=EventImpact.LOW,
                payload={},
                identity_key="b" * 32,
            )
        ],
    )


def test_collect_is_idempotent(session: Session) -> None:
    table = load_b3_table()
    first = collect(session, date(2026, 10, 15), date(2027, 4, 13), table, NOW)
    second = collect(session, date(2026, 10, 15), date(2027, 4, 13), table, NOW)

    assert first.upsert.inserted == first.expiries + first.b3_holidays
    assert second.upsert.inserted == 0
    assert second.vanished == 0
    assert len(session.execute(_mine()).scalars().all()) == first.upsert.inserted


def test_known_us_holiday_shifts_the_expiry(session: Session) -> None:
    _us_closed(session, date(2026, 10, 16))
    assert us_closed_days(session) == frozenset({date(2026, 10, 16)})

    collect(session, date(2026, 10, 15), date(2026, 10, 31), {}, NOW)

    days = [
        r.starts_at.date()
        for r in session.execute(_mine()).scalars()
        if r.kind is EventKind.OPTIONS_EXPIRY
    ]
    assert days == [date(2026, 10, 15)]


def test_a_holiday_learned_later_moves_the_event_without_a_phantom(
    session: Session,
) -> None:
    collect(session, date(2026, 10, 1), date(2026, 10, 31), {}, NOW.replace(day=1))
    _us_closed(session, date(2026, 10, 16))

    result = collect(
        session, date(2026, 10, 1), date(2026, 10, 31), {}, NOW.replace(day=1)
    )

    options = [
        r.starts_at.date()
        for r in session.execute(_mine()).scalars()
        if r.kind is EventKind.OPTIONS_EXPIRY
    ]
    assert options == [date(2026, 10, 15)]
    assert result.vanished == 1
