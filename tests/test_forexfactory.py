"""Tests for the ForexFactory calendar collector.

The recorded payload in tests/fixtures is a real slice of the feed. Tests run
under a private source name so they can neither collide with nor delete rows the
real collector wrote.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from guardrail.collectors import forexfactory
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.events import EventRecord, load_patterns
from guardrail.collectors.forexfactory import (
    URL,
    collect,
    fetch_calendar,
    parse_calendar,
    parse_figure,
)
from guardrail.db.models import EventImpact, EventKind, EventScope, ScheduledEvent

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "ff_calendar_sample.json"


@pytest.fixture(autouse=True)
def _private_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forexfactory, "SOURCE", "forexfactory-test")


def _payload() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    return loaded


def _row(title: str, **override: Any) -> dict[str, Any]:
    return {
        "title": title,
        "country": "USD",
        "date": "2026-10-15T08:30:00-04:00",
        "impact": "High",
        "forecast": "0.2%",
        "previous": "0.3%",
        **override,
    }


def _by_title(
    records: list[EventRecord], title: str, scope: EventScope | None = None
) -> EventRecord:
    return next(
        r for r in records if r.title == title and (scope is None or r.scope == scope)
    )


def _mine() -> Select[ScheduledEvent]:
    """Only this module's rows: the live database also holds real events."""
    return select(ScheduledEvent).where(ScheduledEvent.source == "forexfactory-test")


def _count(session: Session) -> int:
    return len(session.execute(_mine()).scalars().all())


# --- figures -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.2%", (Decimal("0.2"), "%")),
        ("-0.4%", (Decimal("-0.4"), "%")),
        ("197K", (Decimal("197"), "K")),
        ("-21.0B", (Decimal("-21.0"), "B")),
        ("98.1", (Decimal("98.1"), None)),
        ("3.80|1.6", (None, None)),
        ("4.25-4.50%", (None, None)),
        ("<0.1%", (None, None)),
        ("n/a", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_figure(
    raw: str | None, expected: tuple[Decimal | None, str | None]
) -> None:
    assert parse_figure(raw) == expected


# --- recorded payload --------------------------------------------------------


def test_recorded_payload_is_parsed(session: Session) -> None:
    parsed = parse_calendar(_payload(), load_patterns(session), NOW)

    assert len(parsed.records) == 14
    assert parsed.holidays_skipped == 1
    assert parsed.invalid == 0


def test_us_inflation_print_carries_consensus_and_previous(session: Session) -> None:
    parsed = parse_calendar(_payload(), load_patterns(session), NOW)

    core = _by_title(parsed.records, "Core CPI m/m")

    assert core.kind is EventKind.INFLATION_PRINT
    assert core.scope is EventScope.US
    assert core.impact is EventImpact.HIGH
    assert (core.expected, core.previous, core.unit) == (
        Decimal("0.2"),
        Decimal("0.3"),
        "%",
    )
    # 08:30 at -04:00 is 12:30 UTC.
    assert core.starts_at == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert core.has_time is True
    assert core.payload["country"] == "USD"


def test_foreign_events_are_global_and_speeches_are_recognised(
    session: Session,
) -> None:
    parsed = parse_calendar(_payload(), load_patterns(session), NOW)

    lagarde = _by_title(parsed.records, "ECB President Lagarde Speaks")
    employment = _by_title(parsed.records, "Employment Change")

    assert lagarde.scope is EventScope.GLOBAL
    assert lagarde.kind is EventKind.CENTRAL_BANK_SPEECH
    assert employment.kind is EventKind.JOBS_PRINT
    assert (employment.expected, employment.unit) == (Decimal("19.6"), "K")


def test_unreadable_previous_is_dropped_not_guessed(session: Session) -> None:
    parsed = parse_calendar(_payload(), load_patterns(session), NOW)

    auction = _by_title(parsed.records, "German 30-y Bond Auction")

    assert auction.kind is EventKind.AUCTION
    assert auction.previous is None


def test_all_scope_events_on_different_days_are_distinct(session: Session) -> None:
    parsed = parse_calendar(_payload(), load_patterns(session), NOW)

    imf = [r for r in parsed.records if r.title == "IMF Meetings"]

    assert len(imf) == 2
    assert len({r.identity_key for r in imf}) == 2


def test_same_speaker_twice_in_a_day_keeps_both(session: Session) -> None:
    """Seen on the live feed: one official, two appearances, one UTC day."""
    payload = [
        _row(
            "ECB President Lagarde Speaks",
            country="EUR",
            date="2026-10-13T22:10:00-04:00",
        ),
        _row(
            "ECB President Lagarde Speaks",
            country="EUR",
            date="2026-10-14T04:30:00-04:00",
        ),
    ]

    result = collect(session, payload, NOW)

    assert result.upsert.inserted == 2


def test_same_title_in_two_currencies_does_not_collide(session: Session) -> None:
    payload = [
        _row("CPI m/m", country="USD"),
        _row("CPI m/m", country="EUR"),
    ]

    parsed = parse_calendar(payload, load_patterns(session), NOW)

    assert len({r.identity_key for r in parsed.records}) == 2


def test_figures_in_different_units_keep_only_the_forecast(session: Session) -> None:
    parsed = parse_calendar(
        [_row("Retail Sales m/m", forecast="1.2M", previous="980K")],
        load_patterns(session),
        NOW,
    )

    record = parsed.records[0]
    assert (record.expected, record.previous, record.unit) == (
        Decimal("1.2"),
        None,
        "M",
    )


# --- storage -----------------------------------------------------------------


def test_second_run_inserts_nothing(session: Session) -> None:
    first = collect(session, _payload(), NOW)
    second = collect(session, _payload(), NOW)

    assert first.upsert.inserted == 14
    assert second.upsert.inserted == 0
    assert second.upsert.updated == 14
    assert second.vanished == 0
    assert _count(session) == 14


def test_monthly_event_moved_inside_the_month_updates_in_place(
    session: Session,
) -> None:
    collect(session, _payload(), NOW)
    moved = _payload()
    for row in moved:
        if row["title"] == "Core CPI m/m":
            row["date"] = "2026-10-16T08:30:00-04:00"

    result = collect(session, moved, NOW)

    assert result.upsert.moved == 1
    assert result.upsert.inserted == 0
    assert _count(session) == 14


def test_weekly_event_moved_a_day_leaves_no_phantom(session: Session) -> None:
    """The day is in its key, so the new date is a new row and the old must go."""
    collect(session, _payload(), NOW)
    moved = _payload()
    for row in moved:
        if row["title"] == "Unemployment Claims":
            row["date"] = "2026-10-16T08:30:00-04:00"

    result = collect(session, moved, NOW)

    assert result.upsert.inserted == 1
    assert result.vanished == 1
    assert _count(session) == 14


def test_blank_forecast_does_not_erase_a_stored_one(session: Session) -> None:
    collect(session, [_row("Core CPI m/m")], NOW)

    collect(session, [_row("Core CPI m/m", forecast="", previous="")], NOW)

    row = session.execute(_mine()).scalar_one()
    session.refresh(row)
    assert (row.expected, row.previous) == (Decimal("0.2"), Decimal("0.3"))


def test_released_figure_is_recorded_when_the_source_publishes_it(
    session: Session,
) -> None:
    collect(session, [_row("Core CPI m/m")], NOW)

    collect(session, [_row("Core CPI m/m", forecast="0.3%")], NOW)

    row = session.execute(_mine()).scalar_one()
    session.refresh(row)
    assert row.expected == Decimal("0.3")


# --- hostile and broken input ------------------------------------------------


def test_payload_that_is_not_a_list_is_rejected(session: Session) -> None:
    with pytest.raises(UpstreamDataError, match="expected a list"):
        collect(session, {"error": "rate limited"}, NOW)


def test_empty_week_is_a_failure_not_an_empty_agenda(session: Session) -> None:
    """Silent "no events" on the day of a rate decision is the worst outcome."""
    collect(session, _payload(), NOW)

    with pytest.raises(UpstreamDataError, match="no usable events"):
        collect(session, [], NOW)

    assert _count(session) == 14


def test_feed_of_only_holidays_is_a_failure(session: Session) -> None:
    with pytest.raises(UpstreamDataError):
        collect(session, [_row("Bank Holiday", impact="Holiday")], NOW)


def test_mostly_unreadable_feed_is_a_failure(session: Session) -> None:
    payload = [_row("Core CPI m/m"), {"junk": 1}, {"junk": 2}, "text", None]

    with pytest.raises(UpstreamDataError, match="most calendar rows unreadable"):
        collect(session, payload, NOW)

    assert _count(session) == 0


def test_partial_response_keeps_good_rows_and_skips_reconciliation(
    session: Session,
) -> None:
    collect(session, _payload(), NOW)
    partial = [r for r in _payload() if r["title"] != "Unemployment Claims"][:10]
    partial.append({"title": "broken"})

    result = collect(session, partial, NOW)

    assert result.invalid == 1
    assert result.vanished == 0
    # The rows the partial document did not list are still there.
    assert _count(session) == 14


@pytest.mark.parametrize(
    "override",
    [
        {"date": "2036-10-15T08:30:00-04:00"},
        {"date": "2016-10-15T08:30:00-04:00"},
        {"date": "2026-12-25T08:30:00-04:00"},
        {"date": "2026-10-15T08:30:00"},
        {"date": "not a date"},
        {"date": None},
        {"impact": "Catastrophic"},
        {"impact": None},
        {"title": ""},
        {"title": None},
        {"country": ""},
    ],
)
def test_adversarial_row_is_counted_as_invalid(
    session: Session, override: dict[str, Any]
) -> None:
    parsed = parse_calendar(
        [_row("Core CPI m/m"), {**_row("Retail Sales m/m"), **override}],
        load_patterns(session),
        NOW,
    )

    assert len(parsed.records) == 1
    assert parsed.invalid == 1


def test_text_in_a_figure_field_becomes_null(session: Session) -> None:
    parsed = parse_calendar(
        [_row("Core CPI m/m", forecast="soon", previous="DROP TABLE")],
        load_patterns(session),
        NOW,
    )

    assert (parsed.records[0].expected, parsed.records[0].previous) == (None, None)


def test_hostile_title_is_stored_as_inert_text(session: Session) -> None:
    nasty = "<script>alert(1)</script> {{ 7*7 }} '; DROP TABLE scheduled_event;--"

    collect(session, [_row(nasty)], NOW)

    assert session.execute(_mine()).scalar_one().title == nasty


def test_very_long_title_is_truncated(session: Session) -> None:
    parsed = parse_calendar([_row("x" * 5000)], load_patterns(session), NOW)

    assert len(parsed.records[0].title) == 300


# --- network -----------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_returns_the_recorded_document() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_payload()))

    async with httpx.AsyncClient() as http:
        assert await fetch_calendar(http) == _payload()


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_raises() -> None:
    respx.get(URL).mock(return_value=httpx.Response(429))

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_calendar(http)


@pytest.mark.asyncio
@respx.mock
async def test_truncated_json_raises() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b'[{"title": "CPI m/m", "cou')
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError):
            await fetch_calendar(http)


def test_window_constant_is_a_few_weeks() -> None:
    """A feed that jumps by months is corrupt; this bound is what catches it."""
    assert timedelta(days=7) <= forexfactory.WINDOW <= timedelta(days=60)
