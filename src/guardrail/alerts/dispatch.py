"""Collect due alerts, drop the ones already sent, send, remember.

`dispatch` takes the caller's session (SPEC §12.1) and a plain `send` function, so
it can be tested without a network and without opening a session of its own. A
message is recorded as sent only after Telegram accepted it; a failed delivery
stops the pass and is retried whole on the next one.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.alerts import rules
from guardrail.alerts.rules import Alert, EventInfo, ReviewInfo, SourceInfo, VolumeInfo
from guardrail.alerts.telegram import TelegramClient, TelegramError
from guardrail.collectors.store import record_failure, record_success
from guardrail.config import get_settings
from guardrail.db.models import (
    AlertSent,
    EventImpact,
    EventKind,
    EventScope,
    Instrument,
    ScheduledEvent,
    SourceHealth,
    Watchlist,
)
from guardrail.db.session import dispose_engine, session_scope
from guardrail.web import health, queries

log = structlog.get_logger(__name__)

HEALTH_SOURCE: Final = "alerts.telegram"
# Events this close are candidates; the rules decide whether a threshold is crossed.
EVENT_LOOKAHEAD: Final = timedelta(hours=25)
DATE_ONLY_LOOKAHEAD_DAYS: Final = 2

Send = Callable[[str], Awaitable[None]]


class ReviewSource(Protocol):
    """Where overdue position reviews come from."""

    def overdue(self, session: Session, today: datetime) -> list[ReviewInfo] | None:
        """Positions past review, or None when positions are not tracked at all."""
        ...


class NoPositionTables:
    """The review source until positions exist (SPEC phase 3 is not built).

    Returns None, not an empty list: "nothing is overdue" and "nothing is being
    watched" are different statements, and the pass says which one it is.
    """

    def overdue(self, session: Session, today: datetime) -> list[ReviewInfo] | None:
        return None


@dataclass(slots=True)
class DispatchResult:
    """What one pass did."""

    configured: bool = True
    candidates: int = 0
    sent: int = 0
    already_sent: int = 0
    failed: str | None = None
    reviews_monitored: bool = False
    texts: list[str] = field(default_factory=list)


def configured() -> bool:
    """Whether a bot token and a chat id are set."""
    settings = get_settings()
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


# --- reading the database -------------------------------------------------------------


def _watchlist_ids(session: Session) -> set[int]:
    return set(session.execute(select(Watchlist.instrument_id)).scalars())


def event_candidates(
    session: Session, now: datetime, watched: set[int]
) -> list[EventInfo]:
    """High-impact events about to happen: market-wide, or on a watched asset."""
    horizon = now + max(EVENT_LOOKAHEAD, timedelta(days=DATE_ONLY_LOOKAHEAD_DAYS))
    rows = session.execute(
        select(ScheduledEvent, Instrument.symbol, Instrument.venue)
        .outerjoin(Instrument, Instrument.id == ScheduledEvent.instrument_id)
        .where(
            ScheduledEvent.impact == EventImpact.HIGH,
            ScheduledEvent.kind != EventKind.HOLIDAY,
            ScheduledEvent.starts_at <= horizon,
            queries.not_yet_over(now),
        )
        .order_by(ScheduledEvent.starts_at)
    ).all()
    affects = tuple(queries.reference_labels(session).values())
    out: list[EventInfo] = []
    for event, symbol, venue in rows:
        market_wide = event.instrument_id is None and event.scope in (
            EventScope.US,
            EventScope.BR,
        )
        on_watched = event.instrument_id in watched
        if not (market_wide or on_watched):
            continue
        out.append(
            EventInfo(
                identity_key=event.identity_key,
                title=event.title,
                kind=event.kind.value,
                starts_at=event.starts_at,
                has_time=event.has_time,
                expected=event.expected,
                previous=event.previous,
                unit=event.unit,
                symbol=queries.display_symbol(symbol, venue) if symbol else None,
                affects=affects if event.instrument_id is None else (),
            )
        )
    return out


def volume_candidates(
    session: Session, now: datetime, watched: set[int]
) -> list[VolumeInfo]:
    """Watched assets whose newest bar is current and unusually heavy."""
    if not watched:
        return []
    out: list[VolumeInfo] = []
    for market in ("crypto", "us", "br"):
        expected = queries.expected_last_bar(session, market, now)
        for snap in queries.market_snapshot(session, market, now):
            if snap.instrument_id not in watched or snap.rvol is None:
                continue
            if snap.last_day < expected:
                continue  # an old bar is not today's move
            change = (
                snap.last_close / snap.prev_close - 1
                if snap.prev_close and snap.prev_close > 0
                else None
            )
            out.append(
                VolumeInfo(
                    snap.instrument_id,
                    queries.display_symbol(snap.symbol, snap.venue),
                    snap.last_day,
                    snap.rvol,
                    change,
                )
            )
    return out


def source_candidates(session: Session, now: datetime) -> list[SourceInfo]:
    """Sources the header would show red.

    Job rows and the heartbeat are left out: the collector rows say the same thing
    with the real error, and a dead scheduler cannot alert about itself.
    """
    out: list[SourceInfo] = []
    for row in session.execute(select(SourceHealth)).scalars():
        if row.source.startswith(("job.", "scheduler.")):
            continue
        group = next(
            (g for g in health.GROUPS if row.source.startswith(g.prefixes)), None
        )
        if group is None:
            continue
        state, _ = health.classify(row, group.max_age, now)
        if state is not health.State.FAILED:
            continue
        if (
            row.last_success_at is None
            and row.consecutive_failures < health.RED_FAILURES
        ):
            continue
        out.append(
            SourceInfo(
                row.source,
                group.label,
                row.last_success_at,
                row.consecutive_failures,
                row.last_error,
            )
        )
    return out


# --- the pass ---------------------------------------------------------------


def collect(
    session: Session, now: datetime, reviews: ReviewSource
) -> tuple[list[Alert], list[str], bool]:
    """Every alert that is due, the keys to mark silently, and whether reviews
    are being monitored."""
    watched = _watchlist_ids(session)
    events = event_candidates(session, now, watched)
    volumes = volume_candidates(session, now, watched)
    sources = source_candidates(session, now)
    overdue = reviews.overdue(session, now)

    # Dedupe against everything that could be a key of this pass.
    event_keys = [
        f"event:{e.identity_key}:{label}"
        for e in events
        for label in ("24h", "1h", "day")
    ]
    volume_keys = [
        f"volume:{v.instrument_id}:{v.last_day.isoformat()}" for v in volumes
    ]
    source_keys = [
        f"source:{s.source}:"
        + (s.last_success_at.isoformat() if s.last_success_at else "never")
        for s in sources
    ]
    review_keys = [
        f"review:{r.position_key}:{r.review_by.isoformat()}" for r in overdue or []
    ]
    wanted = event_keys + volume_keys + source_keys + review_keys
    already: set[str] = (
        set(
            session.execute(
                select(AlertSent.dedupe_key).where(AlertSent.dedupe_key.in_(wanted))
            ).scalars()
        )
        if wanted
        else set()
    )

    event_alerts, superseded = rules.event_alerts(events, now, already)
    alerts = [
        *event_alerts,
        *rules.source_alerts(sources, now, already),
        *rules.review_alerts(overdue or [], now.date(), already),
        *rules.volume_alerts(volumes, already),
    ]
    return alerts, superseded, overdue is not None


async def dispatch(
    session: Session,
    now: datetime,
    send: Send,
    reviews: ReviewSource | None = None,
    dry_run: bool = False,
) -> DispatchResult:
    """One pass: send every due alert not sent before. Never raises on delivery.

    A delivery error ends the pass and is reported in the result; the alerts
    already delivered stay recorded and the rest go out on the next pass.
    """
    alerts, superseded, monitored = collect(session, now, reviews or NoPositionTables())
    result = DispatchResult(
        candidates=len(alerts),
        reviews_monitored=monitored,
        texts=[a.text for a in alerts],
    )
    if dry_run:
        return result

    for key in superseded:
        session.add(AlertSent(dedupe_key=key, kind="event", text="(superseded)"))
    for alert in alerts:
        try:
            await send(alert.text)
        except TelegramError as exc:
            result.failed = str(exc)
            log.warning(
                "alerts.send_failed", error=str(exc), retry_after=exc.retry_after
            )
            break
        session.add(AlertSent(dedupe_key=alert.key, kind=alert.kind, text=alert.text))
        result.sent += 1
    session.flush()
    return result


async def run(dry_run: bool = False) -> DispatchResult:
    """Entry point: one pass with the real bot. Inert without a token and chat id."""
    if not configured():
        return DispatchResult(configured=False)
    settings = get_settings()
    assert settings.telegram_bot_token is not None and settings.telegram_chat_id
    async with httpx.AsyncClient(timeout=20.0) as http:
        client = TelegramClient(
            http, settings.telegram_bot_token, settings.telegram_chat_id
        )
        with session_scope() as session:
            result = await dispatch(
                session, datetime.now(UTC), client.send, dry_run=dry_run
            )
            if not dry_run:
                if result.failed:
                    record_failure(session, HEALTH_SOURCE, result.failed)
                else:
                    record_success(session, HEALTH_SOURCE)
    return result


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Send due Telegram alerts.")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be sent; send nothing"
    )
    args = parser.parse_args()

    async def preview() -> DispatchResult:
        # A dry run needs no bot: it only reads the database.
        async def never(_text: str) -> None:
            raise AssertionError("a dry run must not send")

        with session_scope() as session:
            return await dispatch(session, datetime.now(UTC), never, dry_run=True)

    try:
        result = asyncio.run(preview() if args.dry_run else run())
    finally:
        dispose_engine()

    if not result.configured:
        print("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return 0
    print(f"candidates: {result.candidates}  sent: {result.sent}")
    if not result.reviews_monitored:
        print("position reviews: not monitored (no position tables yet)")
    for text in result.texts if args.dry_run else []:
        print("---")
        print(text)
    if result.failed:
        print(f"delivery failed: {result.failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
