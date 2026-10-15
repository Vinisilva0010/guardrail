"""Tests for the scheduled B3 closing refresh.

The refresh once spent 1,081 of the month's 15,000 requests in a few minutes
because it walked brapi's whole listing instead of the tickers already tracked.
These tests pin what it must never do again.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from guardrail.collectors import quota
from guardrail.collectors.b3 import (
    BASE_URL,
    MAX_REQUESTS_PER_RUN,
    QUOTA_SOURCE,
    BrapiClient,
    RefreshResult,
    b3_universe,
    check_ceiling,
    refresh_closes,
)
from guardrail.collectors.errors import UpstreamDataError
from guardrail.db.models import ApiQuota, AssetClass, Candle, Instrument

D = Decimal


def _bar(day: int, close: str = "30.0", volume: int = 1000) -> dict[str, Any]:
    ts = int(datetime(2026, 10, day, tzinfo=UTC).timestamp())
    return {
        "date": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def _payload(symbol: str, *bars: dict[str, Any]) -> dict[str, Any]:
    return {"results": [{"symbol": symbol, "historicalDataPrice": list(bars)}]}


def _url(symbol: str) -> str:
    return f"{BASE_URL}/quote/{symbol}"


def _universe(session: Session, *symbols: str) -> None:
    session.execute(text("TRUNCATE instrument CASCADE"))
    for symbol in symbols:
        session.add(
            Instrument(symbol=symbol, venue="b3", asset_class=AssetClass.EQUITY)
        )
    session.flush()


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cast(Any, BrapiClient._get).retry, "sleep", instant)


def _used(session: Session) -> int:
    session.expire_all()
    return session.execute(
        select(func.coalesce(func.sum(ApiQuota.used), 0)).where(
            ApiQuota.source == QUOTA_SOURCE
        )
    ).scalar_one()


async def _refresh(session: Session, symbols: list[str]) -> RefreshResult:
    async with httpx.AsyncClient() as http:
        return await refresh_closes(session, BrapiClient(http), symbols)


# --- the universe --------------------------------------------------------------


def test_universe_is_the_tracked_tickers_only_sorted_and_active(
    session: Session,
) -> None:
    _universe(session, "VALE3", "ABEV3", "PETR4")
    session.add(
        Instrument(symbol="AAPL", venue="alpaca", asset_class=AssetClass.EQUITY)
    )
    session.add(
        Instrument(
            symbol="OLD3", venue="b3", asset_class=AssetClass.EQUITY, is_active=False
        )
    )
    session.flush()

    assert b3_universe(session) == ["ABEV3", "PETR4", "VALE3"]


def test_ceiling_refuses_a_run_larger_than_the_budgeted_one() -> None:
    check_ceiling(MAX_REQUESTS_PER_RUN)  # exactly at the ceiling is allowed

    with pytest.raises(UpstreamDataError, match="per-run ceiling"):
        check_ceiling(MAX_REQUESTS_PER_RUN + 1)


def test_ceiling_is_a_fraction_of_the_monthly_allowance() -> None:
    """Twice a week, about 8.7 runs a month: must leave room for everything else."""
    assert MAX_REQUESTS_PER_RUN * 52 * 2 / 12 < 0.5 * 15_000 + 1_000


# --- requests ------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_one_request_per_tracked_ticker_and_none_for_any_other(
    session: Session,
) -> None:
    _universe(session, "ABEV3", "PETR4")
    routes = {
        s: respx.get(_url(s)).mock(
            return_value=httpx.Response(200, json=_payload(s, _bar(13)))
        )
        for s in ("ABEV3", "PETR4")
    }
    stray = respx.get(url__regex=rf"{BASE_URL}/(?!quote/(ABEV3|PETR4)$).*").mock(
        return_value=httpx.Response(200, json={})
    )
    before = _used(session)

    result = await _refresh(session, b3_universe(session))

    assert [r.call_count for r in routes.values()] == [1, 1]
    assert stray.call_count == 0
    assert (result.symbols, result.requested) == (2, 2)
    assert _used(session) - before == 2


@pytest.mark.asyncio
@respx.mock
async def test_recorded_bars_are_stored_and_a_second_run_adds_nothing(
    session: Session,
) -> None:
    _universe(session, "PETR4")
    respx.get(_url("PETR4")).mock(
        return_value=httpx.Response(
            200, json=_payload("PETR4", _bar(12), _bar(13, "31.5"))
        )
    )

    first = await _refresh(session, ["PETR4"])
    second = await _refresh(session, ["PETR4"])

    assert (first.inserted, second.inserted) == (2, 0)
    stored = session.execute(select(func.count()).select_from(Candle)).scalar_one()
    assert stored == 2


@pytest.mark.asyncio
@respx.mock
async def test_a_ticker_with_no_bars_is_not_a_failure(session: Session) -> None:
    _universe(session, "ABEV3")
    respx.get(_url("ABEV3")).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    result = await _refresh(session, ["ABEV3"])

    assert (result.failed, result.inserted) == (0, 0)
    assert result.ok


@pytest.mark.asyncio
@respx.mock
async def test_a_malformed_ticker_does_not_stop_the_others(session: Session) -> None:
    _universe(session, "ABEV3", "PETR4")
    respx.get(_url("ABEV3")).mock(
        return_value=httpx.Response(200, content=b'{"results": [{"hist')
    )
    respx.get(_url("PETR4")).mock(
        return_value=httpx.Response(200, json=_payload("PETR4", _bar(13)))
    )

    result = await _refresh(session, ["ABEV3", "PETR4"])

    assert (result.failed, result.inserted) == (1, 1)


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_is_retried_and_every_attempt_spends_budget(
    session: Session,
) -> None:
    _universe(session, "PETR4")
    route = respx.get(_url("PETR4")).mock(return_value=httpx.Response(429))
    before = _used(session)

    result = await _refresh(session, ["PETR4"])

    assert route.call_count == 3
    assert _used(session) - before == 3
    assert result.failed == 1


@pytest.mark.asyncio
@respx.mock
async def test_unknown_ticker_is_a_failure_not_a_crash(session: Session) -> None:
    _universe(session, "ZZZZ3")
    respx.get(_url("ZZZZ3")).mock(return_value=httpx.Response(404))

    result = await _refresh(session, ["ZZZZ3"])

    assert result.failed == 1


# --- the allowance -------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_a_refused_reservation_makes_no_call_and_keeps_earlier_bars(
    session: Session,
) -> None:
    _universe(session, "ABEV3", "PETR4", "VALE3")
    for s in ("ABEV3", "PETR4", "VALE3"):
        respx.get(_url(s)).mock(
            return_value=httpx.Response(200, json=_payload(s, _bar(13)))
        )
    # Closing calls stop at 95% of 15,000 = 14,250. Leave room for exactly one.
    quota.reserve(session, QUOTA_SOURCE, quota.CallKind.CLOSE)
    session.execute(
        text("UPDATE api_quota SET used = 14249 WHERE source = :s"), {"s": QUOTA_SOURCE}
    )

    result = await _refresh(session, ["ABEV3", "PETR4", "VALE3"])

    assert result.halted is not None and "brapi" in result.halted
    assert (result.requested, result.inserted) == (1, 1)
    assert sum(r.call_count for r in respx.routes) == 1
    assert not result.ok


@pytest.mark.asyncio
@respx.mock
async def test_over_the_ceiling_nothing_is_requested_at_all(session: Session) -> None:
    _universe(session, "ABEV3")
    route = respx.get(_url("ABEV3")).mock(
        return_value=httpx.Response(200, json=_payload("ABEV3", _bar(13)))
    )
    quota.reserve(session, QUOTA_SOURCE, quota.CallKind.CLOSE)
    session.execute(
        text("UPDATE api_quota SET used = 14250 WHERE source = :s"), {"s": QUOTA_SOURCE}
    )

    result = await _refresh(session, ["ABEV3"])

    assert route.call_count == 0
    assert result.halted is not None


def test_a_run_is_ok_only_when_it_finished_and_few_tickers_failed() -> None:
    assert RefreshResult(100, 100, 400, 400, 0).ok
    assert RefreshResult(100, 100, 400, 400, 20).ok  # exactly the allowed share
    assert not RefreshResult(100, 100, 400, 400, 21).ok
    assert not RefreshResult(100, 40, 160, 160, 0, halted="quota").ok
    assert not RefreshResult(0, 0, 0, 0, 0).ok
