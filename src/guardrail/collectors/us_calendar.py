"""US market holidays and early closes, from the Alpaca trading calendar.

The endpoint lists only the days the market is open. A weekday missing from the
answer is a holiday and a close before 16:00 is an early close. Because absence
is the signal, a truncated answer would invent holidays; the checks below refuse
an answer that does not look complete rather than store it.

The calendar carries no holiday names, so the title says what is known: the
market is closed.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import AlpacaClient
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.events import (
    EventRecord,
    UpsertResult,
    calendar_key,
    drop_vanished,
    upsert_events,
)
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import EventImpact, EventKind, EventScope
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

SOURCE: Final = "alpaca"
HEALTH_SOURCE: Final = "alpaca.calendar"
HORIZON: Final = timedelta(days=180)
REGULAR_CLOSE: Final = "16:00"

# More closed weekdays than this in the horizon means the answer is cut short:
# the market closes about ten days a year.
MAX_HOLIDAYS: Final = 8
# The answer must reach into the first and last week of the window.
EDGE_DAYS: Final = 7

CLOSED_TITLE: Final = "US market closed"


class _Day(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: date
    close: str


@dataclass(frozen=True, slots=True)
class UsCalendar:
    """Closed days and early closes inside a window."""

    holidays: list[date]
    early_closes: dict[date, str]


def derive_calendar(payload: Any, start: date, end: date) -> UsCalendar:
    """Closed weekdays and early closes from the list of open days."""
    if not isinstance(payload, list):
        raise UpstreamDataError(f"expected a list of days, got {type(payload)}")
    days: dict[date, str] = {}
    for raw in payload:
        try:
            day = _Day.model_validate(raw)
        except ValidationError:
            raise UpstreamDataError(f"unreadable calendar row: {raw!r}") from None
        if start <= day.date <= end:
            days[day.date] = day.close

    if not any(d <= start + timedelta(days=EDGE_DAYS) for d in days) or not any(
        d >= end - timedelta(days=EDGE_DAYS) for d in days
    ):
        raise UpstreamDataError("calendar does not reach both ends of the window")

    holidays = [
        start + timedelta(days=n)
        for n in range((end - start).days + 1)
        if (start + timedelta(days=n)).weekday() < 5
        and (start + timedelta(days=n)) not in days
    ]
    if len(holidays) > MAX_HOLIDAYS:
        raise UpstreamDataError(
            f"{len(holidays)} closed weekdays in {(end - start).days} days: "
            "the calendar looks truncated"
        )
    early = {d: c for d, c in days.items() if c != REGULAR_CLOSE}
    return UsCalendar(holidays, early)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def build_records(calendar: UsCalendar) -> list[EventRecord]:
    """One date-only event per closed day and per early close."""
    records: list[EventRecord] = []
    for day in calendar.holidays:
        records.append(
            _record(day, CLOSED_TITLE, {"derived": "weekday missing from /v2/calendar"})
        )
    for day, close in calendar.early_closes.items():
        records.append(
            _record(day, f"US market early close ({close} ET)", {"close": close})
        )
    return records


def _record(day: date, title: str, payload: dict[str, Any]) -> EventRecord:
    starts_at = _midnight(day)
    return EventRecord(
        source=SOURCE,
        kind=EventKind.HOLIDAY,
        scope=EventScope.US,
        title=title,
        starts_at=starts_at,
        has_time=False,
        impact=EventImpact.LOW,
        payload=payload,
        identity_key=calendar_key(SOURCE, EventKind.HOLIDAY, title, starts_at),
    )


@dataclass(frozen=True, slots=True)
class CollectResult:
    """Outcome of one run."""

    holidays: int
    early_closes: int
    upsert: UpsertResult
    vanished: int


def collect(session: Session, payload: Any, start: date, end: date) -> CollectResult:
    """Store the window described by one calendar answer."""
    calendar = derive_calendar(payload, start, end)
    records = build_records(calendar)
    upsert = upsert_events(session, records)
    vanished = 0
    if records:
        vanished = drop_vanished(
            session,
            SOURCE,
            {r.identity_key for r in records},
            _midnight(start),
            _midnight(end),
            datetime.now(UTC),
        )
    return CollectResult(
        len(calendar.holidays), len(calendar.early_closes), upsert, vanished
    )


async def run() -> CollectResult:
    """Entry point: fetch the next six months, store, record health."""
    start = datetime.now(UTC).date()
    end = start + HORIZON
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            payload = await AlpacaClient(http).fetch_market_calendar(start, end)
        with session_scope() as session:
            result = collect(session, payload, start, end)
            record_success(session, HEALTH_SOURCE)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, HEALTH_SOURCE, f"{type(exc).__name__}: {exc}")
        raise
    return result


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(description="Collect US market holidays.").parse_args()
    try:
        result = asyncio.run(run())
    finally:
        dispose_engine()
    print(f"\nclosed days:    {result.holidays}")
    print(f"early closes:   {result.early_closes}")
    print(f"inserted:       {result.upsert.inserted}")
    print(f"vanished:       {result.vanished}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
