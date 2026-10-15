"""Macro calendar from the ForexFactory mirror (nfs.faireconomy.media).

One JSON document with the current week: central bank decisions and speeches,
inflation, jobs and activity releases for every major currency, with the
consensus ("forecast") and the previous value where the source publishes them.
Public, no key.

Holiday rows are skipped: the source stamps them with an hour in another time
zone, so the instant is not a reliable calendar date. Exchange holidays have
their own collector.
"""

import argparse
import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.events import (
    MAX_TITLE_CHARS,
    EventRecord,
    UpsertResult,
    calendar_key,
    classify,
    drop_vanished,
    load_patterns,
    upsert_events,
)
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import EventImpact, EventKind, EventScope
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

URL: Final = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE: Final = "forexfactory"
HEALTH_SOURCE: Final = "forexfactory.calendar"
HEADERS: Final = {"User-Agent": "Mozilla/5.0 (compatible; guardrail/1.0)"}

# The feed covers one week. A date far outside it means a corrupt item.
WINDOW: Final = timedelta(days=30)

IMPACT: Final = {
    "high": EventImpact.HIGH,
    "medium": EventImpact.MEDIUM,
    "low": EventImpact.LOW,
}

# Currency -> market. Anything else (EUR, GBP, JPY, "All") moves global markets
# without belonging to one of ours.
SCOPE_BY_CURRENCY: Final = {"USD": EventScope.US, "BRL": EventScope.BR}

# A published figure: optional sign, digits, optional unit suffix. Anything else
# ("3.80|1.6", "4.25-4.50%", "<0.1%") is not a single number and is not guessed.
_FIGURE: Final = re.compile(r"^(-?\d+(?:\.\d+)?)([%KMBT]?)$")


class _Item(BaseModel):
    """One calendar row as the source sends it."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    country: str = Field(min_length=1)
    date: datetime
    impact: str
    forecast: str | None = None
    previous: str | None = None

    @field_validator("date")
    @classmethod
    def _must_carry_an_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("date has no UTC offset")
        return value


@dataclass(frozen=True, slots=True)
class ParsedCalendar:
    """A parsed document and what was left out of it."""

    records: list[EventRecord]
    holidays_skipped: int
    invalid: int


@dataclass(frozen=True, slots=True)
class CollectResult:
    """Outcome of one collection run."""

    parsed: int
    holidays_skipped: int
    invalid: int
    upsert: UpsertResult
    vanished: int


def parse_figure(raw: str | None) -> tuple[Decimal | None, str | None]:
    """A published number and its unit suffix, or (None, None)."""
    if raw is None:
        return None, None
    match = _FIGURE.match(raw.strip())
    if match is None:
        return None, None
    return Decimal(match.group(1)), match.group(2) or None


def parse_calendar(
    payload: Any, patterns: list[tuple[re.Pattern[str], EventKind]], now: datetime
) -> ParsedCalendar:
    """Turn the document into records. Unreadable rows are counted, not raised."""
    if not isinstance(payload, list):
        raise UpstreamDataError(f"expected a list of events, got {type(payload)}")

    records: list[EventRecord] = []
    holidays = invalid = 0
    for raw in payload:
        try:
            item = _Item.model_validate(raw)
        except ValidationError:
            invalid += 1
            continue

        impact_name = item.impact.strip().lower()
        if impact_name == "holiday":
            holidays += 1
            continue
        impact = IMPACT.get(impact_name)
        starts_at = item.date.astimezone(UTC)
        if impact is None or abs(starts_at - now) > WINDOW:
            invalid += 1
            continue

        title = item.title.strip()[:MAX_TITLE_CHARS]
        currency = item.country.strip().upper()
        kind = classify(title, patterns)

        expected, expected_unit = parse_figure(item.forecast)
        previous, previous_unit = parse_figure(item.previous)
        # Two figures in different units cannot be compared; keep the forecast.
        if (
            previous is not None
            and expected is not None
            and previous_unit != expected_unit
        ):
            previous = None
        unit = expected_unit if expected is not None else previous_unit

        records.append(
            EventRecord(
                source=SOURCE,
                kind=kind,
                scope=SCOPE_BY_CURRENCY.get(currency, EventScope.GLOBAL),
                title=title,
                starts_at=starts_at,
                has_time=True,
                impact=impact,
                expected=expected,
                previous=previous,
                unit=unit,
                payload=dict(raw),
                # The currency tells apart "CPI m/m" for USD and for EUR.
                identity_key=calendar_key(
                    SOURCE, kind, f"{currency} {title}", starts_at
                ),
            )
        )
    return ParsedCalendar(records, holidays, invalid)


def collect(session: Session, payload: Any, now: datetime) -> CollectResult:
    """Store one document. Raises when it cannot be trusted as the schedule."""
    parsed = parse_calendar(payload, load_patterns(session), now)

    if not parsed.records:
        # An empty week is not a real calendar; treating it as one would show
        # "no events" on the day of a rate decision.
        raise UpstreamDataError(
            f"calendar carried no usable events ({parsed.invalid} unreadable)"
        )
    if parsed.invalid > len(parsed.records):
        raise UpstreamDataError(
            f"most calendar rows unreadable: {parsed.invalid} bad, "
            f"{len(parsed.records)} good"
        )

    upsert = upsert_events(session, parsed.records)

    vanished = 0
    if parsed.invalid == 0:
        # Only a fully readable document can say what is no longer scheduled.
        starts = [r.starts_at for r in parsed.records]
        vanished = drop_vanished(
            session,
            SOURCE,
            {r.identity_key for r in parsed.records},
            min(starts),
            max(starts),
            now,
        )
    else:
        log.warning("forexfactory.reconcile_skipped", invalid=parsed.invalid)

    return CollectResult(
        len(parsed.records), parsed.holidays_skipped, parsed.invalid, upsert, vanished
    )


async def fetch_calendar(http: httpx.AsyncClient) -> Any:
    """Download the current week."""
    response = await http.get(URL)
    response.raise_for_status()
    return response.json()


async def run() -> CollectResult:
    """Entry point: fetch, store, record the source's health."""
    try:
        async with httpx.AsyncClient(
            timeout=30.0, headers=HEADERS, follow_redirects=True
        ) as http:
            payload = await fetch_calendar(http)
        with session_scope() as session:
            result = collect(session, payload, datetime.now(UTC))
            record_success(session, HEALTH_SOURCE)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, HEALTH_SOURCE, f"{type(exc).__name__}: {exc}")
        raise
    log.info(
        "forexfactory.collected",
        parsed=result.parsed,
        inserted=result.upsert.inserted,
        updated=result.upsert.updated,
        vanished=result.vanished,
    )
    return result


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(description="Collect the macro calendar.").parse_args()
    try:
        result = asyncio.run(run())
    finally:
        dispose_engine()

    print(f"\nevents parsed:     {result.parsed}")
    print(f"inserted:          {result.upsert.inserted}")
    print(f"updated:           {result.upsert.updated}")
    print(f"start time moved:  {result.upsert.moved}")
    print(f"vanished dropped:  {result.vanished}")
    print(f"holidays skipped:  {result.holidays_skipped}")
    print(f"unreadable rows:   {result.invalid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
