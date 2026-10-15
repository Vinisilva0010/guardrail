"""The market clock shown in the header.

Only what can be stated without guessing. NYSE hours are stable and public
(09:30-16:00 New York, earlier on an early-close day that the Alpaca calendar
supplies). B3 intraday hours change with the US daylight-saving calendar and are
not asserted here: the header says whether B3 trades today, not at what minute.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.db.models import EventKind, EventScope, ScheduledEvent

NEW_YORK: Final = ZoneInfo("America/New_York")
SAO_PAULO: Final = ZoneInfo("America/Sao_Paulo")

NYSE_OPEN: Final = time(9, 30)
NYSE_CLOSE: Final = time(16, 0)
EARLY_CLOSE_PREFIX: Final = "US market early close ("
CLOSED_TITLE: Final = "US market closed"


@dataclass(frozen=True, slots=True)
class MarketClock:
    """The three header phrases."""

    nyse: str
    b3: str
    crypto: str

    @property
    def text(self) -> str:
        return f"{self.b3} · {self.nyse} · {self.crypto}"


def _closed_days(session: Session, scope: EventScope, around: date) -> dict[date, str]:
    rows = session.execute(
        select(ScheduledEvent.starts_at, ScheduledEvent.title).where(
            ScheduledEvent.kind == EventKind.HOLIDAY,
            ScheduledEvent.scope == scope,
            ScheduledEvent.starts_at
            >= datetime.combine(around, time(), tzinfo=UTC) - timedelta(days=1),
            ScheduledEvent.starts_at
            <= datetime.combine(around, time(), tzinfo=UTC) + timedelta(days=1),
        )
    ).all()
    return {starts.astimezone(UTC).date(): title for starts, title in rows}


def _early_close(title: str) -> time | None:
    """The close time from 'US market early close (13:00 ET)', or None."""
    if not title.startswith(EARLY_CLOSE_PREFIX):
        return None
    raw = title[len(EARLY_CLOSE_PREFIX) : len(EARLY_CLOSE_PREFIX) + 5]
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}" if hours else f"{rest} min"


def nyse_phrase(session: Session, now: datetime) -> str:
    """Whether the NYSE is open, and how long until it changes."""
    local = now.astimezone(NEW_YORK)
    today = local.date()
    events = _closed_days(session, EventScope.US, today)
    title = events.get(today)

    if local.weekday() >= 5 or title == CLOSED_TITLE:
        return "NYSE fechada hoje"
    close = (_early_close(title) if title else None) or NYSE_CLOSE
    opens = datetime.combine(today, NYSE_OPEN, tzinfo=NEW_YORK)
    closes = datetime.combine(today, close, tzinfo=NEW_YORK)
    if local < opens:
        return f"NYSE abre em {_span(opens - local)}"
    if local < closes:
        return f"NYSE fecha em {_span(closes - local)}"
    return "NYSE fechada"


def b3_phrase(session: Session, now: datetime) -> str:
    """Whether B3 trades today. Never claims an hour."""
    today = now.astimezone(SAO_PAULO).date()
    if today.weekday() >= 5:
        return "B3 fechada hoje (fim de semana)"
    name = _closed_days(session, EventScope.BR, today).get(today)
    if name is not None:
        return f"B3 fechada hoje ({name.removeprefix('B3 closed: ')})"
    return "B3 com pregão hoje"


def market_clock(session: Session, now: datetime) -> MarketClock:
    """The header phrases for this instant."""
    return MarketClock(
        nyse=nyse_phrase(session, now),
        b3=b3_phrase(session, now),
        crypto="cripto 24h",
    )
