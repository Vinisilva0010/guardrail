"""Tests for the Binance funding rate client.

Three behaviours here come from verification against the live endpoint rather
than from the documentation, and each would be a silent data defect if dropped:
an error body served with HTTP 200, an empty markPrice on historical records,
and startTime=0 being ignored by the venue.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.funding import (
    BASE_URL,
    FUNDING_PATH,
    MAX_LIMIT,
    FundingClient,
)

URL = f"{BASE_URL}{FUNDING_PATH}"
START = datetime(2023, 10, 3, tzinfo=UTC)


def _record(ts: datetime, rate: str = "0.0001", mark: str = "") -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": int(ts.timestamp() * 1000),
        "fundingRate": rate,
        "markPrice": mark,
        "rateType": "Regular",
    }


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


@pytest.mark.asyncio
@respx.mock
async def test_returns_settlements(client: httpx.AsyncClient) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _record(START),
                _record(START + timedelta(hours=8), rate="-0.0002"),
            ],
        )
    )

    result = await FundingClient(client).fetch_funding("BTCUSDT", START)

    assert len(result) == 2
    assert result[1].funding_rate == Decimal("-0.0002")


@pytest.mark.asyncio
@respx.mock
async def test_empty_mark_price_becomes_null(client: httpx.AsyncClient) -> None:
    """Historical records carry an empty markPrice; empty means absent."""
    respx.get(URL).mock(return_value=httpx.Response(200, json=[_record(START)]))

    result = await FundingClient(client).fetch_funding("BTCUSDT", START)

    assert result[0].mark_price is None


@pytest.mark.asyncio
@respx.mock
async def test_present_mark_price_is_parsed(client: httpx.AsyncClient) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=[_record(START, mark="85212.9")])
    )

    result = await FundingClient(client).fetch_funding("BTCUSDT", START)

    assert result[0].mark_price == Decimal("85212.9")


@pytest.mark.asyncio
@respx.mock
async def test_error_body_with_http_200_is_rejected(client: httpx.AsyncClient) -> None:
    """The venue signals failure with a 200 and an object body."""
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ERROR",
                "code": "99099990",
                "errorData": "illegal params.",
            },
        )
    )

    with pytest.raises(UpstreamDataError, match="expected a list"):
        await FundingClient(client).fetch_funding("BTCUSDT", START)


@pytest.mark.asyncio
async def test_epoch_start_is_rejected(client: httpx.AsyncClient) -> None:
    """startTime=0 is ignored upstream, which then returns the newest page.

    Accepting it would silently store recent data labelled as the oldest.
    """
    with pytest.raises(ValueError, match="after the epoch"):
        await FundingClient(client).fetch_funding(
            "BTCUSDT", datetime.fromtimestamp(0, tz=UTC)
        )


@pytest.mark.asyncio
@respx.mock
async def test_unsettled_record_is_dropped(client: httpx.AsyncClient) -> None:
    """A settlement whose time has not arrived must not be stored."""
    now = datetime.now(UTC)
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _record(now - timedelta(hours=8)),
                _record(now + timedelta(hours=8)),
            ],
        )
    )

    result = await FundingClient(client).fetch_funding(
        "BTCUSDT", now - timedelta(days=1)
    )

    assert len(result) == 1
    assert result[0].funding_time < now


@pytest.mark.asyncio
@respx.mock
async def test_pagination_terminates_on_repeated_page(
    client: httpx.AsyncClient,
) -> None:
    page = [_record(START + timedelta(hours=8 * i)) for i in range(MAX_LIMIT)]
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=page))

    result = await FundingClient(client).fetch_funding(
        "BTCUSDT", START, START + timedelta(days=4000)
    )

    assert route.call_count < 5, "pagination did not terminate"
    assert len(result) == MAX_LIMIT


@pytest.mark.asyncio
@respx.mock
async def test_malformed_record_is_rejected(client: httpx.AsyncClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=[{"symbol": "BTCUSDT"}]))

    with pytest.raises(UpstreamDataError, match="malformed funding record"):
        await FundingClient(client).fetch_funding("BTCUSDT", START)
