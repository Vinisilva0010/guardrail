"""Tests for the Alpaca equities client.

Two behaviours here are load-bearing and would fail silently if changed: the
consolidated feed, because the single-venue feed reports a fraction of real
volume and would skew the liquidity filter without raising; and the end-date
clamp, because the free plan refuses same-day data with a 403.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from guardrail.collectors.alpaca import (
    DATA_URL,
    FEED,
    TRADING_URL,
    AlpacaClient,
    latest_available_day,
)
from guardrail.collectors.errors import UpstreamDataError

BARS_URL = f"{DATA_URL}/v2/stocks/bars"
ASSETS_URL = f"{TRADING_URL}/v2/assets"
START = date(2026, 9, 1)


def _bar(day: date, close: float = 100.5, volume: float = 1_000_000) -> dict[str, Any]:
    return {
        "t": datetime(day.year, day.month, day.day, tzinfo=UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "o": 100.0,
        "h": 110.0,
        "l": 90.0,
        "c": close,
        "v": volume,
        "n": 100,
        "vw": 100.0,
    }


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


@pytest.mark.asyncio
@respx.mock
async def test_filters_assets_by_exchange(client: httpx.AsyncClient) -> None:
    """ARCA and BATS are mostly ETFs; OTC volume does not support breakouts."""
    respx.get(ASSETS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "AAA", "name": "A", "exchange": "NASDAQ", "tradable": True},
                {"symbol": "BBB", "name": "B", "exchange": "NYSE", "tradable": True},
                {"symbol": "CCC", "name": "C", "exchange": "ARCA", "tradable": True},
                {"symbol": "DDD", "name": "D", "exchange": "OTC", "tradable": True},
                {"symbol": "EEE", "name": "E", "exchange": "NASDAQ", "tradable": False},
            ],
        )
    )

    assets = await AlpacaClient(client).fetch_assets()

    assert sorted(a.symbol for a in assets) == ["AAA", "BBB"]


@pytest.mark.asyncio
@respx.mock
async def test_requests_the_consolidated_feed(client: httpx.AsyncClient) -> None:
    """The single-venue feed reports 2-6% of real volume.

    Selecting it would skew every liquidity figure by up to fifty times while
    returning perfectly well-formed data.
    """
    route = respx.get(BARS_URL).mock(
        return_value=httpx.Response(200, json={"bars": {"AAA": [_bar(START)]}})
    )

    await AlpacaClient(client).fetch_daily_bars(["AAA"], START)

    assert route.calls.last.request.url.params["feed"] == FEED == "sip"


@pytest.mark.asyncio
@respx.mock
async def test_end_date_is_clamped_to_last_available_day(
    client: httpx.AsyncClient,
) -> None:
    """The free plan answers 403 for same-day SIP data."""
    route = respx.get(BARS_URL).mock(
        return_value=httpx.Response(200, json={"bars": {"AAA": [_bar(START)]}})
    )

    await AlpacaClient(client).fetch_daily_bars(
        ["AAA"], START, end=date.today() + timedelta(days=5)
    )

    sent = route.calls.last.request.url.params["end"]
    assert sent == latest_available_day().isoformat()


@pytest.mark.asyncio
@respx.mock
async def test_price_is_exact_decimal(client: httpx.AsyncClient) -> None:
    respx.get(BARS_URL).mock(
        return_value=httpx.Response(
            200, json={"bars": {"AAA": [_bar(START, close=193.07)]}}
        )
    )

    bars = await AlpacaClient(client).fetch_daily_bars(["AAA"], START)

    assert isinstance(bars["AAA"][0].close, Decimal)
    assert bars["AAA"][0].close == Decimal("193.07")


@pytest.mark.asyncio
@respx.mock
async def test_pagination_is_followed(client: httpx.AsyncClient) -> None:
    respx.get(BARS_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={"bars": {"AAA": [_bar(START)]}, "next_page_token": "abc"}
            ),
            httpx.Response(
                200,
                json={
                    "bars": {"AAA": [_bar(START + timedelta(days=1))]},
                    "next_page_token": None,
                },
            ),
        ]
    )

    bars = await AlpacaClient(client).fetch_daily_bars(["AAA"], START)

    assert len(bars["AAA"]) == 2
    assert bars["AAA"][0].day < bars["AAA"][1].day


@pytest.mark.asyncio
@respx.mock
async def test_refusal_is_not_retried(client: httpx.AsyncClient) -> None:
    """403 means the plan forbids it; retrying cannot change that."""
    route = respx.get(BARS_URL).mock(
        return_value=httpx.Response(
            403, json={"message": "subscription does not permit"}
        )
    )

    with pytest.raises(UpstreamDataError, match="alpaca refused"):
        await AlpacaClient(client).fetch_daily_bars(["AAA"], START)

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_malformed_bar_is_rejected(client: httpx.AsyncClient) -> None:
    respx.get(BARS_URL).mock(
        return_value=httpx.Response(200, json={"bars": {"AAA": [{"t": "2026-09-01Z"}]}})
    )

    with pytest.raises(UpstreamDataError, match="malformed bar"):
        await AlpacaClient(client).fetch_daily_bars(["AAA"], START)


@pytest.mark.asyncio
@respx.mock
async def test_zero_price_bar_is_rejected(client: httpx.AsyncClient) -> None:
    """Zero is a reporting gap; stored, it reads as a 100% move."""
    bad = _bar(START)
    bad["l"] = 0.0
    respx.get(BARS_URL).mock(
        return_value=httpx.Response(200, json={"bars": {"AAA": [bad]}})
    )

    with pytest.raises(UpstreamDataError, match="non-positive price"):
        await AlpacaClient(client).fetch_daily_bars(["AAA"], START)


@pytest.mark.asyncio
async def test_empty_symbol_list_is_a_noop(client: httpx.AsyncClient) -> None:
    assert await AlpacaClient(client).fetch_daily_bars([], START) == {}
