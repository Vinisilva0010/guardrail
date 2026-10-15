"""Tests for the request budget.

The property that matters: a collector over its ceiling makes no HTTP call at
all. Counting calls in respx proves that, rather than trusting the counter.
"""

from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from guardrail.collectors import quota
from guardrail.collectors.b3 import BASE_URL, QUOTA_SOURCE, BrapiClient
from guardrail.collectors.quota import (
    CallKind,
    Period,
    QuotaExceeded,
    period_start,
    reserve,
)
from guardrail.db.models import ApiQuota

SOURCE = "test-source"
DAY = date(2026, 10, 14)
BUDGET = 100


@pytest.fixture(autouse=True)
def _test_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(quota.BUDGETS, SOURCE, (BUDGET, Period.MONTH))


def _used(session: Session, source: str = SOURCE, today: date = DAY) -> int:
    period = period_start(quota.BUDGETS[source][1], today)
    return session.execute(
        select(ApiQuota.used).where(
            ApiQuota.source == source, ApiQuota.period == period
        )
    ).scalar_one()


def _set_used(session: Session, source: str, today: date, used: int) -> None:
    period = period_start(quota.BUDGETS[source][1], today)
    session.execute(
        text("UPDATE api_quota SET used = :u WHERE source = :s AND period = :p"),
        {"u": used, "s": source, "p": period},
    )


def test_period_start() -> None:
    assert period_start(Period.MONTH, date(2026, 10, 14)) == date(2026, 10, 1)
    assert period_start(Period.DAY, date(2026, 10, 14)) == date(2026, 10, 14)


def test_reserve_counts_each_call(session: Session) -> None:
    assert reserve(session, SOURCE, today=DAY) == 1
    assert reserve(session, SOURCE, today=DAY) == 2
    assert reserve(session, SOURCE, cost=5, today=DAY) == 7
    assert _used(session) == 7


def test_new_month_starts_from_zero(session: Session) -> None:
    reserve(session, SOURCE, cost=50, today=DAY)

    assert reserve(session, SOURCE, today=date(2026, 11, 2)) == 1
    assert _used(session) == 50


def test_other_calls_stop_at_80_percent(session: Session) -> None:
    reserve(session, SOURCE, cost=80, today=DAY)

    with pytest.raises(QuotaExceeded):
        reserve(session, SOURCE, CallKind.OTHER, today=DAY)
    assert _used(session) == 80


def test_closing_calls_pass_between_80_and_95_percent(session: Session) -> None:
    reserve(session, SOURCE, cost=80, today=DAY)

    assert reserve(session, SOURCE, CallKind.CLOSE, cost=15, today=DAY) == 95


def test_nothing_passes_above_95_percent(session: Session) -> None:
    reserve(session, SOURCE, CallKind.CLOSE, cost=95, today=DAY)

    with pytest.raises(QuotaExceeded):
        reserve(session, SOURCE, CallKind.CLOSE, today=DAY)
    with pytest.raises(QuotaExceeded):
        reserve(session, SOURCE, CallKind.OTHER, today=DAY)
    assert _used(session) == 95


def test_refused_reservation_leaves_counter_untouched(session: Session) -> None:
    reserve(session, SOURCE, cost=78, today=DAY)

    with pytest.raises(QuotaExceeded):
        reserve(session, SOURCE, cost=5, today=DAY)

    assert _used(session) == 78


def test_unknown_source_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="no request budget"):
        reserve(session, "nobody-knows", today=DAY)


def test_cost_must_be_positive(session: Session) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        reserve(session, SOURCE, cost=0, today=DAY)


def test_changed_budget_is_followed_without_losing_usage(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserve(session, SOURCE, cost=10, today=DAY)
    monkeypatch.setitem(quota.BUDGETS, SOURCE, (200, Period.MONTH))

    reserve(session, SOURCE, today=DAY)

    row = session.execute(
        select(ApiQuota).where(ApiQuota.source == SOURCE)
    ).scalar_one()
    assert (row.used, row.budget) == (11, 200)


def test_database_rejects_negative_usage(session: Session) -> None:
    reserve(session, SOURCE, today=DAY)

    with pytest.raises(IntegrityError), session.begin_nested():
        _set_used(session, SOURCE, DAY, -1)


# --- the HTTP client ---------------------------------------------------------

QUOTE_URL = f"{BASE_URL}/quote/AAAA3"


@pytest.mark.asyncio
@respx.mock
async def test_client_over_ceiling_makes_no_http_call(session: Session) -> None:
    route = respx.get(QUOTE_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    reserve(session, QUOTA_SOURCE)
    today = date.today()
    _set_used(session, QUOTA_SOURCE, today, quota.BUDGETS[QUOTA_SOURCE][0])

    async with httpx.AsyncClient() as http:
        client = BrapiClient(http)
        with pytest.raises(QuotaExceeded):
            await client.fetch_history(session, "AAAA3")

    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_client_spends_one_request_per_call(session: Session) -> None:
    route = respx.get(QUOTE_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    reserve(session, QUOTA_SOURCE)
    before = _used(session, QUOTA_SOURCE, date.today())

    async with httpx.AsyncClient() as http:
        await BrapiClient(http).fetch_history(session, "AAAA3")

    assert route.call_count == 1
    assert _used(session, QUOTA_SOURCE, date.today()) == before + 1
