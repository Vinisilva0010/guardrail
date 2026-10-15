"""Tests for the shared event layer: identity keys and the upsert."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from guardrail.collectors.events import (
    MINUTE_KINDS,
    MONTHLY_KINDS,
    EventRecord,
    calendar_key,
    classify,
    drop_vanished,
    earnings_key,
    load_patterns,
    normalize_title,
    unlock_key,
    upsert_events,
)
from guardrail.db.models import EventImpact, EventKind, EventScope, ScheduledEvent

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

moments = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 1, 1),
    timezones=st.just(UTC),
)
titles = st.text(min_size=1, max_size=60).filter(lambda t: normalize_title(t) != "")
monthly = st.sampled_from(sorted(MONTHLY_KINDS, key=str))
daily = st.sampled_from(
    [k for k in EventKind if k not in MONTHLY_KINDS and k not in MINUTE_KINDS]
)


def _record(
    key: str,
    starts_at: datetime = NOW + timedelta(days=3),
    source: str = "test-source",
    title: str = "Rate decision",
    expected: Decimal | None = None,
    previous: Decimal | None = None,
    actual: Decimal | None = None,
    unit: str | None = None,
) -> EventRecord:
    return EventRecord(
        source=source,
        kind=EventKind.RATE_DECISION,
        scope=EventScope.US,
        title=title,
        starts_at=starts_at,
        has_time=True,
        impact=EventImpact.HIGH,
        payload={},
        identity_key=key,
        expected=expected,
        previous=previous,
        actual=actual,
        unit=unit,
    )


# --- identity keys -----------------------------------------------------------


@given(titles, moments, monthly)
def test_key_is_stable(title: str, when: datetime, kind: EventKind) -> None:
    assert calendar_key("s", kind, title, when) == calendar_key("s", kind, title, when)


@given(titles, moments, monthly)
def test_monthly_key_ignores_the_day(
    title: str, when: datetime, kind: EventKind
) -> None:
    """A postponement inside the month must update the row, not add one."""
    first = when.replace(day=1)
    last = when.replace(day=28)

    assert calendar_key("s", kind, title, first) == calendar_key("s", kind, title, last)


@given(titles, moments, daily)
def test_daily_key_follows_the_day(title: str, when: datetime, kind: EventKind) -> None:
    """Weekly releases and speeches repeat inside a month and must stay apart."""
    first = when.replace(day=1, hour=9)
    next_day = first + timedelta(days=1)

    assert calendar_key("s", kind, title, first) != calendar_key(
        "s", kind, title, next_day
    )
    assert calendar_key("s", kind, title, first) == calendar_key(
        "s", kind, title, first.replace(hour=20)
    )


@given(titles, moments)
def test_speech_key_follows_the_minute(title: str, when: datetime) -> None:
    """The same official can speak twice in a day; both must survive."""
    kind = EventKind.CENTRAL_BANK_SPEECH
    early = when.replace(hour=2, minute=10)
    late = when.replace(hour=8, minute=30)

    assert calendar_key("s", kind, title, early) != calendar_key("s", kind, title, late)
    assert calendar_key("s", kind, title, early) == calendar_key(
        "s", kind, title, early.replace(second=45)
    )


@given(titles, moments, monthly)
def test_key_changes_with_every_identifying_part(
    title: str, when: datetime, kind: EventKind
) -> None:
    base = calendar_key("s", kind, title, when)

    assert calendar_key("other", kind, title, when) != base
    assert calendar_key("s", kind, title + "x", when) != base
    assert calendar_key("s", EventKind.OTHER, title, when) != base
    assert calendar_key("s", kind, title, when + timedelta(days=62)) != base


@given(titles, moments, daily)
def test_key_is_32_lowercase_hex(title: str, when: datetime, kind: EventKind) -> None:
    key = calendar_key("s", kind, title, when)

    assert len(key) == 32
    assert set(key) <= set("0123456789abcdef")


def test_earnings_key_ignores_the_date_and_follows_the_instrument() -> None:
    assert earnings_key(1, "2026Q3") == earnings_key(1, "2026Q3")
    assert earnings_key(1, "2026Q3") != earnings_key(2, "2026Q3")
    assert earnings_key(1, "2026Q3") != earnings_key(1, "2026Q4")


@given(moments, st.integers(1, 10_000))
def test_unlock_key_is_per_instrument_and_day(when: datetime, instrument: int) -> None:
    noon = when.replace(hour=12)

    assert unlock_key(instrument, noon.replace(hour=1)) == unlock_key(
        instrument, noon.replace(hour=23)
    )
    assert unlock_key(instrument, noon) != unlock_key(instrument + 1, noon)


def test_normalize_title() -> None:
    assert (
        normalize_title("  Decisão   de JUROS\t(Copom) ") == "decisao de juros (copom)"
    )


@given(st.text(max_size=80))
def test_normalize_title_is_idempotent(title: str) -> None:
    once = normalize_title(title)

    assert normalize_title(once) == once


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Federal Funds Rate", EventKind.RATE_DECISION),
        ("FOMC Statement", EventKind.RATE_DECISION),
        ("FOMC Member Waller Speaks", EventKind.CENTRAL_BANK_SPEECH),
        ("Fed Chair Press Conference", EventKind.CENTRAL_BANK_SPEECH),
        ("Core CPI m/m", EventKind.INFLATION_PRINT),
        ("Core PCE Price Index m/m", EventKind.INFLATION_PRINT),
        ("Non-Farm Employment Change", EventKind.JOBS_PRINT),
        ("Unemployment Claims", EventKind.JOBS_PRINT),
        ("Prelim GDP q/q", EventKind.GDP_PRINT),
        ("Retail Sales m/m", EventKind.ACTIVITY_PRINT),
        ("German 30-y Bond Auction", EventKind.AUCTION),
        ("IMF Meetings", EventKind.OTHER),
        ("", EventKind.OTHER),
    ],
)
def test_classify_uses_the_seeded_patterns(
    session: Session, title: str, kind: EventKind
) -> None:
    assert classify(title, load_patterns(session)) is kind


def test_a_speech_is_never_a_rate_decision(session: Session) -> None:
    """Lower priority wins, so a rate word in a speech title cannot reclassify it."""
    patterns = load_patterns(session)

    assert (
        classify("ECB President Lagarde Speaks on Interest Rate", patterns)
        is EventKind.CENTRAL_BANK_SPEECH
    )


# --- upsert ------------------------------------------------------------------


def _mine() -> Select[ScheduledEvent]:
    """Only this module's rows: the live database also holds real events."""
    return select(ScheduledEvent).where(ScheduledEvent.source == "test-source")


def _count(session: Session) -> int:
    return len(session.execute(_mine()).scalars().all())


def test_upsert_inserts_then_updates(session: Session) -> None:
    key = "1" * 32

    first = upsert_events(session, [_record(key)])
    second = upsert_events(session, [_record(key)])

    assert (first.inserted, first.updated) == (1, 0)
    assert (second.inserted, second.updated) == (0, 1)
    assert _count(session) == 1


def test_upsert_empty_list_is_a_no_op(session: Session) -> None:
    assert upsert_events(session, []).inserted == 0


def test_missing_field_never_blanks_a_filled_one(session: Session) -> None:
    key = "2" * 32
    upsert_events(
        session,
        [_record(key, expected=Decimal("4.0"), previous=Decimal("4.25"), unit="%")],
    )

    upsert_events(session, [_record(key)])

    row = session.execute(_mine()).scalar_one()
    session.refresh(row)
    assert (row.expected, row.previous, row.unit) == (
        Decimal("4.0"),
        Decimal("4.25"),
        "%",
    )


def test_new_value_replaces_old_one(session: Session) -> None:
    key = "3" * 32
    upsert_events(session, [_record(key, expected=Decimal("4.0"))])

    upsert_events(
        session, [_record(key, expected=Decimal("3.75"), actual=Decimal("3.75"))]
    )

    row = session.execute(_mine()).scalar_one()
    session.refresh(row)
    assert (row.expected, row.actual) == (Decimal("3.75"), Decimal("3.75"))


def test_changed_start_time_updates_in_place_and_is_counted(session: Session) -> None:
    key = "4" * 32
    upsert_events(session, [_record(key, NOW + timedelta(days=3))])

    result = upsert_events(session, [_record(key, NOW + timedelta(days=5))])

    row = session.execute(_mine()).scalar_one()
    session.refresh(row)
    assert result.moved == 1
    assert row.starts_at == NOW + timedelta(days=5)
    assert _count(session) == 1


def test_unchanged_start_time_is_not_counted_as_moved(session: Session) -> None:
    key = "5" * 32
    upsert_events(session, [_record(key)])

    assert upsert_events(session, [_record(key)]).moved == 0


def test_duplicate_keys_inside_one_batch_are_merged(session: Session) -> None:
    key = "6" * 32

    result = upsert_events(
        session,
        [
            _record(key, expected=Decimal("1.0"), unit="%"),
            _record(key, previous=Decimal("0.9")),
        ],
    )

    row = session.execute(_mine()).scalar_one()
    assert result.inserted == 1
    assert (row.expected, row.previous, row.unit) == (
        Decimal("1.0"),
        Decimal("0.9"),
        "%",
    )


def test_title_is_truncated_on_storage(session: Session) -> None:
    upsert_events(session, [_record("7" * 32, title="x" * 5000)])

    assert len(session.execute(_mine()).scalar_one().title) == 300


# --- dropping vanished events ------------------------------------------------


def test_vanished_future_events_of_the_source_are_dropped(session: Session) -> None:
    keep, gone = "a" * 32, "b" * 32
    upsert_events(session, [_record(keep), _record(gone)])

    dropped = drop_vanished(
        session, "test-source", {keep}, NOW, NOW + timedelta(days=10), NOW
    )

    assert dropped == 1
    assert session.execute(_mine()).scalar_one().identity_key == keep


def test_drop_never_touches_the_past_other_sources_or_outside_the_window(
    session: Session,
) -> None:
    keep = "c" * 32
    past = _record("d" * 32, NOW - timedelta(days=2))
    other_source = _record("e" * 32, source="other-source")
    outside = _record("f" * 32, NOW + timedelta(days=40))
    upsert_events(session, [_record(keep), past, other_source, outside])

    dropped = drop_vanished(
        session,
        "test-source",
        {keep},
        NOW - timedelta(days=5),
        NOW + timedelta(days=10),
        NOW,
    )

    assert dropped == 0
    assert _count(session) == 3
    assert (
        session.execute(
            select(func.count())
            .select_from(ScheduledEvent)
            .where(ScheduledEvent.source == "other-source")
        ).scalar_one()
        == 1
    )


def test_drop_refuses_an_empty_keep_set(session: Session) -> None:
    """An empty or failed feed must not be able to wipe the agenda."""
    upsert_events(session, [_record("9" * 32)])

    with pytest.raises(ValueError, match="empty keep set"):
        drop_vanished(session, "test-source", set(), NOW, NOW + timedelta(days=10), NOW)

    assert _count(session) == 1
