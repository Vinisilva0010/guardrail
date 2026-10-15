"""Shared handling of scheduled events: identity keys, bounds, upsert.

Every event collector builds EventRecord values and hands them to
upsert_events. Nothing here talks to the network.

Identity keys (PLANO_ANTECIPACAO §4) leave the exact date out where a date can
move, so a postponement updates the row instead of adding a second one. Two
refinements to the plan's formula, both because the plain formula would merge
distinct events and lose one of them silently:

* the key text includes whatever tells two events with the same title apart
  (the collector decides; ForexFactory adds the currency), because "CPI m/m"
  is a different event for USD and for EUR;
* the month granularity only applies to kinds that occur at most once a month
  per title. Weekly releases (jobless claims, oil inventories) and auctions
  repeat inside a month and keep the day in the key; speeches can repeat inside
  a day (the same official twice, measured on the live feed) and keep the
  minute.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import structlog
from sqlalchemy import Boolean, delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.db.models import (
    EventImpact,
    EventKind,
    EventKindPattern,
    EventScope,
    ScheduledEvent,
)

log = structlog.get_logger(__name__)

BATCH_SIZE: Final = 1000

# Kinds that happen at most once a month per title; see the module docstring.
MONTHLY_KINDS: Final = frozenset(
    {EventKind.RATE_DECISION, EventKind.INFLATION_PRINT, EventKind.GDP_PRINT}
)

# Kinds that can repeat within a day under one title.
MINUTE_KINDS: Final = frozenset({EventKind.CENTRAL_BANK_SPEECH})

# A start time outside this window is a corrupt payload, not a schedule.
MAX_PAST: Final = timedelta(days=365 * 5)
MAX_FUTURE: Final = timedelta(days=365 * 2)

MAX_TITLE_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One scheduled event, ready to store."""

    source: str
    kind: EventKind
    scope: EventScope
    title: str
    starts_at: datetime
    has_time: bool
    impact: EventImpact
    payload: dict[str, Any]
    identity_key: str
    instrument_id: int | None = None
    date_is_estimated: bool = False
    expected: Decimal | None = None
    previous: Decimal | None = None
    actual: Decimal | None = None
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """What an upsert did."""

    inserted: int
    updated: int
    moved: int


def normalize_title(title: str) -> str:
    """Lower case, no accents, single spaces."""
    decomposed = unicodedata.normalize("NFKD", title)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def earnings_key(instrument_id: int, fiscal_period: str) -> str:
    """Company results: one per instrument and fiscal period, whatever the date."""
    return _digest(f"earnings|{instrument_id}|{fiscal_period}")


def unlock_key(instrument_id: int, starts_at: datetime) -> str:
    """Token unlock: one per instrument and day."""
    return _digest(f"unlock|{instrument_id}|{starts_at.astimezone(UTC).date()}")


def calendar_key(source: str, kind: EventKind, title: str, starts_at: datetime) -> str:
    """Calendar event key. `title` should already carry any disambiguator."""
    when = starts_at.astimezone(UTC)
    if kind in MONTHLY_KINDS:
        granularity = when.strftime("%Y-%m")
    elif kind in MINUTE_KINDS:
        granularity = when.strftime("%Y-%m-%dT%H:%M")
    else:
        granularity = when.strftime("%Y-%m-%d")
    return _digest(f"{source}|{kind.value}|{normalize_title(title)}|{granularity}")


def plausible_start(starts_at: datetime, now: datetime) -> bool:
    """Whether a start time is believable for a schedule."""
    return now - MAX_PAST <= starts_at <= now + MAX_FUTURE


def load_patterns(session: Session) -> list[tuple[re.Pattern[str], EventKind]]:
    """Title patterns from the database, lowest priority number first."""
    rows = session.execute(
        select(EventKindPattern.pattern, EventKindPattern.kind).order_by(
            EventKindPattern.priority, EventKindPattern.id
        )
    ).all()
    return [(re.compile(p, re.IGNORECASE), k) for p, k in rows]


def classify(
    title: str, patterns: list[tuple[re.Pattern[str], EventKind]]
) -> EventKind:
    """Kind of an event from its title. Unmatched titles are 'other'."""
    for pattern, kind in patterns:
        if pattern.search(title):
            return kind
    return EventKind.OTHER


def _merge(older: EventRecord, newer: EventRecord) -> EventRecord:
    """Two records with one key in a batch: newer wins, gaps filled from older."""
    return replace(
        newer,
        expected=newer.expected if newer.expected is not None else older.expected,
        previous=newer.previous if newer.previous is not None else older.previous,
        actual=newer.actual if newer.actual is not None else older.actual,
        unit=newer.unit if newer.unit is not None else older.unit,
    )


def _row(record: EventRecord) -> dict[str, Any]:
    return {
        "source": record.source,
        "kind": record.kind,
        "scope": record.scope,
        "instrument_id": record.instrument_id,
        "title": record.title[:MAX_TITLE_CHARS],
        "starts_at": record.starts_at,
        "has_time": record.has_time,
        "date_is_estimated": record.date_is_estimated,
        "impact": record.impact,
        "expected": record.expected,
        "previous": record.previous,
        "actual": record.actual,
        "unit": record.unit,
        "payload": record.payload,
        "identity_key": record.identity_key,
    }


def upsert_events(session: Session, records: list[EventRecord]) -> UpsertResult:
    """Insert new events and update known ones, by identity key.

    A field the source does not publish never erases what another source (or an
    earlier run) filled: expected, previous, actual and unit keep their old value
    when the new one is null. A changed start time is logged with both values.
    """
    unique: dict[str, EventRecord] = {}
    for record in records:
        older = unique.get(record.identity_key)
        unique[record.identity_key] = _merge(older, record) if older else record
    if len(unique) < len(records):
        log.warning(
            "events.duplicate_keys_in_batch", dropped=len(records) - len(unique)
        )
    if not unique:
        return UpsertResult(0, 0, 0)

    keys = list(unique)
    previous_starts: dict[str, datetime] = {}
    for start in range(0, len(keys), BATCH_SIZE):
        previous_starts.update(
            session.execute(
                select(ScheduledEvent.identity_key, ScheduledEvent.starts_at).where(
                    ScheduledEvent.identity_key.in_(keys[start : start + BATCH_SIZE])
                )
            ).all()
        )

    inserted = 0
    for start in range(0, len(keys), BATCH_SIZE):
        rows = [_row(unique[k]) for k in keys[start : start + BATCH_SIZE]]
        insert = pg_insert(ScheduledEvent).values(rows)
        excluded = insert.excluded
        table = ScheduledEvent.__table__.c
        updates: dict[str, Any] = {
            # Always published, so the new value simply replaces the old.
            "starts_at": excluded.starts_at,
            "impact": excluded.impact,
            "date_is_estimated": excluded.date_is_estimated,
            "payload": excluded.payload,
            "updated_at": func.now(),
            # Optional: a source that omits one must not blank another's.
            "expected": func.coalesce(excluded.expected, table.expected),
            "previous": func.coalesce(excluded.previous, table.previous),
            "actual": func.coalesce(excluded.actual, table.actual),
            "unit": func.coalesce(excluded.unit, table.unit),
        }
        statement = insert.on_conflict_do_update(
            index_elements=["identity_key"], set_=updates
        ).returning(
            ScheduledEvent.identity_key,
            literal_column("(xmax = 0)", Boolean).label("was_inserted"),
        )
        inserted += sum(1 for _key, fresh in session.execute(statement) if fresh)

    moved = 0
    for key, old in previous_starts.items():
        new = unique[key].starts_at
        if old != new:
            moved += 1
            log.info(
                "events.starts_at_changed",
                title=unique[key].title,
                old=old.isoformat(),
                new=new.isoformat(),
            )
    return UpsertResult(inserted, len(unique) - inserted, moved)


def drop_vanished(
    session: Session,
    source: str,
    keep_keys: set[str],
    window_start: datetime,
    window_end: datetime,
    now: datetime,
) -> int:
    """Delete future events a source no longer lists inside the window it covers.

    A postponed event with the day in its key appears under a new key; without
    this the old row would stay on the agenda as a phantom. Only future rows go:
    history is never touched. An empty keep set is refused, because an empty or
    failed feed must not be able to wipe the agenda.
    """
    if not keep_keys:
        raise ValueError("refusing to drop events against an empty keep set")
    result = session.execute(
        delete(ScheduledEvent)
        .where(
            ScheduledEvent.source == source,
            ScheduledEvent.starts_at > now,
            ScheduledEvent.starts_at >= window_start,
            ScheduledEvent.starts_at <= window_end,
            ScheduledEvent.identity_key.not_in(keep_keys),
        )
        .returning(ScheduledEvent.id)
    )
    dropped = len(result.fetchall())
    if dropped:
        log.info("events.vanished_dropped", source=source, dropped=dropped)
    return dropped
