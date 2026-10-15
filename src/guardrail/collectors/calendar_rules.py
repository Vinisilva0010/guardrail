"""Events that follow from the calendar itself, not from a feed.

* Equity options expire on the third Friday of the month, or the business day
  before when that Friday is a market holiday.
* Monthly futures expire on the last business day of the month.
* B3 closed days come from a table versioned in the repository
  (reference/b3_holidays.json), sourced from B3's published calendar.

All of these are stored with source 'manual'. They are rules, so they are
generated, never fetched; the US holiday set that shifts them comes from the
`alpaca` events already in the database.
"""

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Final

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.collectors import us_calendar
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.events import (
    EventRecord,
    UpsertResult,
    calendar_key,
    drop_vanished,
    upsert_events,
)
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import (
    EventImpact,
    EventKind,
    EventScope,
    ScheduledEvent,
)
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

SOURCE: Final = "manual"
HEALTH_SOURCE: Final = "calendar.rules"
HORIZON: Final = timedelta(days=180)
B3_TABLE: Final = Path(__file__).parent / "reference" / "b3_holidays.json"

# Warn about a year the B3 table lacks once it is this close, so the table is
# extended before a missing holiday can be mistaken for a trading day.
B3_YEAR_WARNING: Final = timedelta(days=60)

QUARTER_MONTHS: Final = frozenset({3, 6, 9, 12})
OPTIONS_TITLE: Final = "Equity options expiry (third Friday)"
FUTURES_TITLE: Final = "Monthly futures expiry (last business day)"


def third_friday(year: int, month: int) -> date:
    """The third Friday of a month."""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _previous_open_day(day: date, closed: frozenset[date]) -> date:
    while day.weekday() >= 5 or day in closed:
        day -= timedelta(days=1)
    return day


def options_expiry(year: int, month: int, closed: frozenset[date]) -> date:
    """Third Friday, or the open day before it when the market is closed."""
    return _previous_open_day(third_friday(year, month), closed)


def futures_expiry(year: int, month: int, closed: frozenset[date]) -> date:
    """Last open weekday of the month."""
    next_month = date(year + month // 12, month % 12 + 1, 1)
    return _previous_open_day(next_month - timedelta(days=1), closed)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        year, month = year + month // 12, month % 12 + 1
    return out


def _record(
    kind: EventKind,
    scope: EventScope,
    title: str,
    day: date,
    impact: EventImpact,
    payload: dict[str, Any],
) -> EventRecord:
    starts_at = _midnight(day)
    return EventRecord(
        source=SOURCE,
        kind=kind,
        scope=scope,
        title=title,
        starts_at=starts_at,
        has_time=False,
        impact=impact,
        payload=payload,
        identity_key=calendar_key(SOURCE, kind, title, starts_at),
    )


def expiry_records(
    start: date, end: date, closed: frozenset[date]
) -> list[EventRecord]:
    """Options and futures expiries with a date in [start, end]."""
    records: list[EventRecord] = []
    for year, month in _months(start, end):
        options = options_expiry(year, month, closed)
        futures = futures_expiry(year, month, closed)
        if start <= options <= end:
            records.append(
                _record(
                    EventKind.OPTIONS_EXPIRY,
                    EventScope.US,
                    OPTIONS_TITLE,
                    options,
                    EventImpact.MEDIUM if month in QUARTER_MONTHS else EventImpact.LOW,
                    {
                        "rule": "third_friday",
                        "shifted": options != third_friday(year, month),
                    },
                )
            )
        if start <= futures <= end:
            records.append(
                _record(
                    EventKind.FUTURES_EXPIRY,
                    EventScope.US,
                    FUTURES_TITLE,
                    futures,
                    EventImpact.LOW,
                    {"rule": "last_business_day"},
                )
            )
    return records


def load_b3_table(path: Path = B3_TABLE) -> dict[int, list[tuple[date, str]]]:
    """The versioned B3 closed days, by year."""
    try:
        raw = json.loads(path.read_text())
        years = raw["years"]
        return {
            int(year): [(date.fromisoformat(d["date"]), str(d["name"])) for d in days]
            for year, days in years.items()
        }
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise UpstreamDataError(f"B3 holiday table unreadable: {exc}") from exc


def b3_records(
    table: dict[int, list[tuple[date, str]]], start: date, end: date
) -> tuple[list[EventRecord], list[int]]:
    """B3 closed days in the window, and the table years it still lacks.

    A year is reported only once it is close enough to matter.
    """
    records: list[EventRecord] = []
    for year in sorted(table):
        for day, name in table[year]:
            if start <= day <= end:
                records.append(
                    _record(
                        EventKind.HOLIDAY,
                        EventScope.BR,
                        f"B3 closed: {name}",
                        day,
                        EventImpact.LOW,
                        {"table": B3_TABLE.name},
                    )
                )
    missing = [
        year
        for year in range(start.year, end.year + 1)
        if year not in table and date(year, 1, 1) - start <= B3_YEAR_WARNING
    ]
    return records, missing


def us_closed_days(session: Session) -> frozenset[date]:
    """US market closed days already collected from Alpaca."""
    rows = session.execute(
        select(ScheduledEvent.starts_at).where(
            ScheduledEvent.source == us_calendar.SOURCE,
            ScheduledEvent.kind == EventKind.HOLIDAY,
            ScheduledEvent.scope == EventScope.US,
            ScheduledEvent.title == us_calendar.CLOSED_TITLE,
        )
    ).scalars()
    return frozenset(r.astimezone(UTC).date() for r in rows)


@dataclass(frozen=True, slots=True)
class RulesResult:
    """Outcome of one run."""

    expiries: int
    b3_holidays: int
    missing_b3_years: list[int]
    us_holidays_known: int
    upsert: UpsertResult
    vanished: int


def collect(
    session: Session,
    start: date,
    end: date,
    table: dict[int, list[tuple[date, str]]],
    now: datetime,
) -> RulesResult:
    """Generate and store every rule-based event in the window."""
    closed = us_closed_days(session)
    if not closed:
        log.warning("calendar_rules.no_us_holidays_known")
    expiries = expiry_records(start, end, closed)
    b3, missing = b3_records(table, start, end)
    records = expiries + b3

    upsert = upsert_events(session, records)
    vanished = drop_vanished(
        session,
        SOURCE,
        {r.identity_key for r in records},
        _midnight(start),
        _midnight(end),
        now,
    )
    return RulesResult(len(expiries), len(b3), missing, len(closed), upsert, vanished)


def run() -> RulesResult:
    """Entry point: generate six months of events and record health."""
    now = datetime.now(UTC)
    start = now.date()
    end = start + HORIZON
    try:
        table = load_b3_table()
        with session_scope() as session:
            result = collect(session, start, end, table, now)
            if result.missing_b3_years:
                record_failure(
                    session,
                    HEALTH_SOURCE,
                    f"B3 holiday table has no entry for {result.missing_b3_years}",
                )
            else:
                record_success(session, HEALTH_SOURCE)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, HEALTH_SOURCE, f"{type(exc).__name__}: {exc}")
        raise
    return result


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(description="Generate calendar-rule events.").parse_args()
    try:
        result = run()
    finally:
        dispose_engine()
    print(f"\nexpiries:           {result.expiries}")
    print(f"B3 holidays:        {result.b3_holidays}")
    print(f"US holidays known:  {result.us_holidays_known}")
    print(f"inserted:           {result.upsert.inserted}")
    print(f"vanished:           {result.vanished}")
    if result.missing_b3_years:
        print(f"WARNING: B3 table lacks {result.missing_b3_years}")
    return 1 if result.missing_b3_years else 0


if __name__ == "__main__":
    raise SystemExit(main())
