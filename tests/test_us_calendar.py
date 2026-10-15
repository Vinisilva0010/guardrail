"""Tests for US market holidays derived from the Alpaca calendar.

Absence is the signal in this feed, so most tests are about answers that merely
look complete.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from guardrail.collectors import us_calendar
from guardrail.collectors.alpaca import TRADING_URL, AlpacaClient
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.us_calendar import (
    build_records,
    collect,
    derive_calendar,
)
from guardrail.db.models import EventKind, EventScope, ScheduledEvent

START, END = date(2026, 10, 1), date(2026, 12, 31)
FIXTURE = Path(__file__).parent / "fixtures" / "alpaca_calendar_q4_2026.json"
CALENDAR_URL = f"{TRADING_URL}/v2/calendar"


@pytest.fixture(autouse=True)
def _private_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us_calendar, "SOURCE", "alpaca-test")


def _payload() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    return loaded


def _mine() -> Select[ScheduledEvent]:
    return select(ScheduledEvent).where(ScheduledEvent.source == "alpaca-test")


def test_recorded_calendar_yields_thanksgiving_and_christmas() -> None:
    calendar = derive_calendar(_payload(), START, END)

    assert calendar.holidays == [date(2026, 11, 26), date(2026, 12, 25)]
    assert calendar.early_closes == {
        date(2026, 11, 27): "13:00",
        date(2026, 12, 24): "13:00",
    }


def test_records_are_date_only_us_holidays() -> None:
    records = build_records(derive_calendar(_payload(), START, END))

    assert len(records) == 4
    assert {r.kind for r in records} == {EventKind.HOLIDAY}
    assert {r.scope for r in records} == {EventScope.US}
    assert not any(r.has_time for r in records)
    assert {r.title for r in records} == {
        "US market closed",
        "US market early close (13:00 ET)",
    }
    assert len({r.identity_key for r in records}) == 4


def test_rows_outside_the_window_are_ignored() -> None:
    extra = {"date": "2027-03-01", "close": "16:00"}

    calendar = derive_calendar([*_payload(), extra], START, END)

    assert calendar.holidays == [date(2026, 11, 26), date(2026, 12, 25)]


def test_answer_that_stops_halfway_is_refused() -> None:
    """Cut off in mid-November it would otherwise list every later weekday closed."""
    half = [d for d in _payload() if d["date"] < "2026-11-15"]

    with pytest.raises(UpstreamDataError, match="does not reach both ends"):
        derive_calendar(half, START, END)


def test_answer_with_a_hole_is_refused() -> None:
    holed = [d for d in _payload() if not ("2026-11-02" <= d["date"] <= "2026-11-20")]

    with pytest.raises(UpstreamDataError, match="looks truncated"):
        derive_calendar(holed, START, END)


def test_empty_answer_is_refused() -> None:
    with pytest.raises(UpstreamDataError):
        derive_calendar([], START, END)


@pytest.mark.parametrize(
    "payload", [{}, "x", None, [{"date": "bad"}], [None], [{"close": "16:00"}]]
)
def test_unreadable_answers_are_refused(payload: Any) -> None:
    with pytest.raises(UpstreamDataError):
        derive_calendar(payload, START, END)


def test_collect_is_idempotent(session: Session) -> None:
    first = collect(session, _payload(), START, END)
    second = collect(session, _payload(), START, END)

    assert (first.holidays, first.early_closes, first.upsert.inserted) == (2, 2, 4)
    assert second.upsert.inserted == 0
    assert len(session.execute(_mine()).scalars().all()) == 4


def test_a_day_that_reopens_is_removed(session: Session) -> None:
    collect(session, _payload(), START, END)
    reopened = [*_payload(), {"date": "2026-11-26", "close": "16:00"}]

    result = collect(session, reopened, START, END)

    assert result.vanished == 1
    titles = [r.title for r in session.execute(_mine()).scalars()]
    assert titles.count("US market closed") == 1


@pytest.mark.asyncio
@respx.mock
async def test_client_returns_the_recorded_days() -> None:
    route = respx.get(CALENDAR_URL).mock(
        return_value=httpx.Response(200, json=_payload())
    )

    async with httpx.AsyncClient() as http:
        days = await AlpacaClient(http).fetch_market_calendar(START, END)

    assert days == _payload()
    assert route.calls.last.request.url.params["start"] == "2026-10-01"


@pytest.mark.asyncio
@respx.mock
async def test_client_rejects_a_non_list() -> None:
    respx.get(CALENDAR_URL).mock(return_value=httpx.Response(200, json={"days": []}))

    async with httpx.AsyncClient() as http:
        with pytest.raises(UpstreamDataError, match="expected a list"):
            await AlpacaClient(http).fetch_market_calendar(START, END)


@pytest.mark.asyncio
@respx.mock
async def test_client_gives_up_on_rate_limit_after_retrying() -> None:
    route = respx.get(CALENDAR_URL).mock(return_value=httpx.Response(429))

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await AlpacaClient(http).fetch_market_calendar(START, END)

    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_client_truncated_json_raises() -> None:
    respx.get(CALENDAR_URL).mock(
        return_value=httpx.Response(200, content=b'[{"date": "2026')
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError):
            await AlpacaClient(http).fetch_market_calendar(START, END)


def test_horizon_covers_the_sixty_day_requirement() -> None:
    assert us_calendar.HORIZON.days >= 60
