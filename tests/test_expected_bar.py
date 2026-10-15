"""Which bar the free feeds can deliver, and which one the screen should expect.

Dates worked out by hand. 15 Oct 2026 is a Thursday; New York is UTC-4 until the
first Sunday of November (1 Nov 2026), UTC-5 after.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import latest_available_day
from guardrail.db.models import EventImpact, EventKind, EventScope, ScheduledEvent
from guardrail.scheduler.jobs import all_jobs
from guardrail.web.queries import B3_COLLECTION_WEEKDAYS, expected_last_bar


def _at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 02:28 UTC is 22:28 in New York: still the 14th there, so the 13th is
        # the newest day the free plan serves. The UTC date would say the 14th
        # and be refused with 403.
        (_at(15, 2, 28), date(2026, 10, 13)),
        # 05:00 UTC is 01:00 on the 15th in New York: the 14th session is served.
        (_at(15, 5), date(2026, 10, 14)),
        (_at(15, 14), date(2026, 10, 14)),
        # 04:30 UTC on 2 Nov is 23:30 EST on 1 Nov, after the clocks went back.
        (_at(2, 4, 30, month=11), date(2026, 10, 31)),
    ],
)
def test_alpaca_serves_bars_through_new_york_yesterday(
    now: datetime, expected: date
) -> None:
    assert latest_available_day(now) == expected


def test_the_old_evening_slot_asked_for_a_day_the_feed_refuses() -> None:
    """00:40 UTC on the 16th is 20:40 on the 15th in New York.

    The old rule took the UTC date minus one, the 15th: the session still in
    progress in New York, which the free plan answers with 403. The New York rule
    asks for the 14th. At the new 05:30 UTC slot the 15th has become "yesterday".
    """
    old_slot = _at(16, 0, 40)

    assert (old_slot.date() - timedelta(days=1)) == date(2026, 10, 15)  # old rule
    assert latest_available_day(old_slot) == date(2026, 10, 14)
    assert latest_available_day(_at(16, 5, 30)) == date(2026, 10, 15)


def test_the_us_closing_job_runs_after_new_york_midnight() -> None:
    job = next(j for j in all_jobs() if j.name == "us_bars")

    # 04:00 UTC is midnight in New York at its earliest (EST would be 05:00).
    assert job.schedule == {"hour": 5, "minute": 30}


def _blank(session: Session) -> None:
    session.execute(text("TRUNCATE scheduled_event CASCADE"))


@pytest.mark.parametrize(
    ("market", "now", "expected"),
    [
        ("crypto", _at(15, 14), date(2026, 10, 14)),
        ("us", _at(15, 14), date(2026, 10, 14)),
        ("us", _at(15, 2, 28), date(2026, 10, 13)),
        # Monday: yesterday is Sunday, so the previous Friday.
        ("us", _at(19, 14), date(2026, 10, 16)),
        # Thursday: the latest Tuesday or Friday before today is Tuesday the 13th.
        ("br", _at(15, 14), date(2026, 10, 13)),
        ("br", _at(14, 14), date(2026, 10, 13)),
        # On Tuesday itself the closing run has not happened yet: Friday the 9th.
        ("br", _at(13, 14), date(2026, 10, 9)),
        ("br", _at(17, 14), date(2026, 10, 16)),  # Saturday
        ("br", _at(19, 14), date(2026, 10, 16)),  # Monday
    ],
)
def test_expected_last_bar(
    session: Session, market: str, now: datetime, expected: date
) -> None:
    _blank(session)

    assert expected_last_bar(session, market, now) == expected


def test_a_us_holiday_is_skipped(session: Session) -> None:
    _blank(session)
    session.add(
        ScheduledEvent(
            source="test-source",
            kind=EventKind.HOLIDAY,
            scope=EventScope.US,
            title="US market closed",
            starts_at=datetime(2026, 11, 26, tzinfo=UTC),
            has_time=False,
            impact=EventImpact.LOW,
            payload={},
            identity_key="a" * 32,
        )
    )
    session.flush()

    # Friday after Thanksgiving: yesterday was the closed Thursday, so Wednesday.
    assert expected_last_bar(session, "us", _at(27, 14, month=11)) == date(2026, 11, 25)


def test_a_b3_holiday_on_a_collection_day_falls_back_to_the_session_before(
    session: Session,
) -> None:
    _blank(session)
    session.add(
        ScheduledEvent(
            source="test-source",
            kind=EventKind.HOLIDAY,
            scope=EventScope.BR,
            title="B3 closed: Tiradentes",
            starts_at=datetime(2026, 4, 21, tzinfo=UTC),
            has_time=False,
            impact=EventImpact.LOW,
            payload={},
            identity_key="b" * 32,
        )
    )
    session.flush()

    # Thursday 23 Apr: the latest Tuesday is the 21st, closed; so Monday the 20th.
    assert expected_last_bar(
        session, "br", datetime(2026, 4, 23, 14, tzinfo=UTC)
    ) == date(2026, 4, 20)


def test_the_screen_and_the_b3_job_agree_on_the_collection_days() -> None:
    job = next(j for j in all_jobs() if j.name == "b3_close")
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    days = [names.index(d) for d in str(job.schedule["day_of_week"]).split(",")]

    assert tuple(days) == B3_COLLECTION_WEEKDAYS
