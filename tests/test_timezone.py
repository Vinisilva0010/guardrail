"""Timezone guarantees.

A session running in a local zone makes date_trunc shift day boundaries by the
host offset. That produces no error and breaks no other test: it silently
regroups data, which in the backtest would mix bars from different days into a
plausible but wrong result. These tests pin the behaviour.
"""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from guardrail.db.session import session_scope


def test_application_session_runs_in_utc() -> None:
    """The engine must pin UTC regardless of the host server's zone."""
    with session_scope() as session:
        assert session.execute(text("SHOW timezone")).scalar_one() == "UTC"


def test_day_boundary_is_not_shifted_by_host_zone(session: Session) -> None:
    """Midnight UTC must truncate to the same calendar day, not the day before."""
    midnight = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    truncated = session.execute(
        text("SELECT date_trunc('day', :ts AT TIME ZONE 'UTC')"), {"ts": midnight}
    ).scalar_one()

    assert truncated.date() == midnight.date()
