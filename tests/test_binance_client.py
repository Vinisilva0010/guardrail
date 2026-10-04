"""Tests for the Binance klines client.

No network calls: respx intercepts HTTP so that malformed payloads, rate limits
and pagination can be exercised deterministically.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from guardrail.collectors.binance import (
    BASE_URL,
    KLINES_PATH,
    BinanceClient,
    Kline,
)
from guardrail.collectors.errors import UpstreamDataError

URL = f"{BASE_URL}{KLINES_PATH}"
START = datetime(2026, 1, 1, tzinfo=UTC)


def _row(open_ms: int, close_ms: int, close: str = "100.5") -> list[Any]:
    return [
        open_ms,
        "100.00000000",
        "110.00000000",
        "90.00000000",
        close,
        "1234.56789000",
        close_ms,
        "0",
        0,
        "0",
        "0",
        "0",
    ]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


@pytest.mark.asyncio
@respx.mock
async def test_returns_closed_klines(client: httpx.AsyncClient) -> None:
    open_time = START
    close_time = START + timedelta(days=1) - timedelta(milliseconds=1)
    respx.get(URL).mock(
        return_value=httpx.Response(200, json=[_row(_ms(open_time), _ms(close_time))])
    )

    result = await BinanceClient(client).fetch_klines("BTCUSDT", "1d", START)

    assert len(result) == 1
    assert result[0].open_time == open_time


@pytest.mark.asyncio
@respx.mock
async def test_drops_bar_that_has_not_closed_yet(client: httpx.AsyncClient) -> None:
    """An open bar still changes; storing it makes backtests irreproducible."""
    now = datetime.now(UTC)
    closed_open = now - timedelta(days=2)
    closed_close = now - timedelta(days=1)
    open_bar_open = now - timedelta(hours=1)
    open_bar_close = now + timedelta(hours=23)

    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _row(_ms(closed_open), _ms(closed_close)),
                _row(_ms(open_bar_open), _ms(open_bar_close)),
            ],
        )
    )

    result = await BinanceClient(client).fetch_klines(
        "BTCUSDT", "1d", now - timedelta(days=5)
    )

    assert len(result) == 1
    assert result[0].close_time < now


@pytest.mark.asyncio
@respx.mock
async def test_price_is_exact_decimal(client: httpx.AsyncClient) -> None:
    """A value float cannot hold must survive parsing unchanged."""
    close_time = START + timedelta(days=1) - timedelta(milliseconds=1)
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json=[_row(_ms(START), _ms(close_time), close="0.000000012345")],
        )
    )

    result = await BinanceClient(client).fetch_klines("BTCUSDT", "1d", START)

    assert result[0].close == Decimal("0.000000012345")
    # Proof that the float path would have lost the value: the same literal
    # routed through float no longer equals the exact Decimal.
    via_float = Decimal(float("0.000000012345"))
    assert via_float != Decimal("0.000000012345")
    assert result[0].close != via_float


@pytest.mark.asyncio
@respx.mock
async def test_malformed_row_is_not_retried(client: httpx.AsyncClient) -> None:
    """Re-requesting bad data returns bad data; fail fast instead."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=[[1, 2]]))

    with pytest.raises(UpstreamDataError):
        await BinanceClient(client).fetch_klines("BTCUSDT", "1d", START)

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_is_retried(client: httpx.AsyncClient) -> None:
    """429 is transient and must be retried, unlike malformed data."""
    close_time = START + timedelta(days=1) - timedelta(milliseconds=1)
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=[_row(_ms(START), _ms(close_time))]),
        ]
    )

    result = await BinanceClient(client).fetch_klines("BTCUSDT", "1d", START)

    assert route.call_count == 2
    assert len(result) == 1


@pytest.mark.asyncio
@respx.mock
async def test_non_list_payload_is_rejected(client: httpx.AsyncClient) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, json={"code": -1121, "msg": "Invalid symbol"})
    )

    with pytest.raises(UpstreamDataError):
        await BinanceClient(client).fetch_klines("BTCUSDT", "1d", START)


@pytest.mark.asyncio
async def test_unsupported_interval_is_rejected(client: httpx.AsyncClient) -> None:
    with pytest.raises(ValueError, match="unsupported interval"):
        await BinanceClient(client).fetch_klines("BTCUSDT", "5m", START)


def test_kline_rejects_high_below_low() -> None:
    row = _row(_ms(START), _ms(START + timedelta(days=1)))
    row[2] = "80.0"  # high
    row[3] = "90.0"  # low

    with pytest.raises(UpstreamDataError):
        Kline.from_row(row)


@pytest.mark.asyncio
@respx.mock
async def test_pagination_follows_cursor_without_duplicates(
    client: httpx.AsyncClient,
) -> None:
    """A range longer than one page must be walked without gaps or repeats.

    This path only triggers past 1000 bars, which is why it is easy to break
    unnoticed during normal development and why it is tested explicitly.
    """
    from guardrail.collectors.binance import MAX_LIMIT

    hour = timedelta(hours=1)
    first_page = [
        _row(_ms(START + hour * i), _ms(START + hour * (i + 1)) - 1)
        for i in range(MAX_LIMIT)
    ]
    second_page = [
        _row(_ms(START + hour * i), _ms(START + hour * (i + 1)) - 1)
        for i in range(MAX_LIMIT, MAX_LIMIT + 5)
    ]
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json=first_page),
            httpx.Response(200, json=second_page),
        ]
    )

    end = START + hour * (MAX_LIMIT + 5)
    result = await BinanceClient(client).fetch_klines("BTCUSDT", "1h", START, end)

    assert route.call_count == 2
    assert len(result) == MAX_LIMIT + 5
    open_times = [k.open_time for k in result]
    assert len(set(open_times)) == len(open_times)
    assert open_times == sorted(open_times)


@pytest.mark.asyncio
@respx.mock
async def test_pagination_stops_if_cursor_does_not_advance(
    client: httpx.AsyncClient,
) -> None:
    """A venue repeating the same page must not loop forever."""
    from guardrail.collectors.binance import MAX_LIMIT

    hour = timedelta(hours=1)
    page = [
        _row(_ms(START + hour * i), _ms(START + hour * (i + 1)) - 1)
        for i in range(MAX_LIMIT)
    ]
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=page))

    end = START + hour * (MAX_LIMIT * 3)
    result = await BinanceClient(client).fetch_klines("BTCUSDT", "1h", START, end)

    assert route.call_count < 5, "pagination did not terminate"
    assert len(result) == MAX_LIMIT
