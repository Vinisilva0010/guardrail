"""Tests for the Binance Vision metrics client.

The checksum path matters most here: a corrupted archive that reached the parser
would store wrong open interest without raising, and the backtest gate would then
validate a setup against bad data.
"""

import hashlib
import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from guardrail.collectors.binance_vision import (
    EXPECTED_HEADER,
    BinanceVisionClient,
    parse_metrics_csv,
)
from guardrail.collectors.errors import ChecksumMismatch, UpstreamDataError

DAY = date(2026, 8, 1)
SYMBOL = "BTCUSDT"
ARCHIVE_URL = (
    f"https://data.binance.vision/data/futures/um/daily/metrics/{SYMBOL}/"
    f"{SYMBOL}-metrics-{DAY.isoformat()}.zip"
)
CHECKSUM_URL = f"{ARCHIVE_URL}.CHECKSUM"

CSV_BODY = (
    ",".join(EXPECTED_HEADER)
    + "\n"
    + "2026-08-01 00:00:00,BTCUSDT,109489.826,6890085260.354,"
    + "2.36265734,1.61198300,2.20090658,1.79177500\n"
    + "2026-08-01 00:05:00,BTCUSDT,109479.913,6889214072.139,"
    + "2.36213405,1.61241100,2.20111254,0.85816500\n"
)


def _zip(body: str = CSV_BODY) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{SYMBOL}-metrics-{DAY.isoformat()}.csv", body)
    return buffer.getvalue()


def _checksum(payload: bytes) -> str:
    return f"{hashlib.sha256(payload).hexdigest()}  file.zip"


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


@pytest.mark.asyncio
@respx.mock
async def test_fetches_and_parses_day(client: httpx.AsyncClient) -> None:
    payload = _zip()
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, content=payload))
    respx.get(CHECKSUM_URL).mock(
        return_value=httpx.Response(200, text=_checksum(payload))
    )

    rows = await BinanceVisionClient(client).fetch_day(SYMBOL, DAY)

    assert len(rows) == 2
    assert rows[0].ts == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    assert rows[0].open_interest == Decimal("109489.826")


@pytest.mark.asyncio
@respx.mock
async def test_missing_day_returns_empty(client: httpx.AsyncClient) -> None:
    """Publication has gaps; a 404 must not abort a multi-year backfill."""
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(404))

    assert await BinanceVisionClient(client).fetch_day(SYMBOL, DAY) == []


@pytest.mark.asyncio
@respx.mock
async def test_checksum_mismatch_is_retried_then_raised(
    client: httpx.AsyncClient,
) -> None:
    """Corrupted transfer must never reach the parser."""
    payload = _zip()
    archive = respx.get(ARCHIVE_URL).mock(
        return_value=httpx.Response(200, content=payload)
    )
    respx.get(CHECKSUM_URL).mock(
        return_value=httpx.Response(200, text="deadbeef  f.zip")
    )

    with pytest.raises(ChecksumMismatch):
        await BinanceVisionClient(client).fetch_day(SYMBOL, DAY)

    assert archive.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_corrupt_archive_is_treated_as_checksum_failure(
    client: httpx.AsyncClient,
) -> None:
    respx.get(ARCHIVE_URL).mock(return_value=httpx.Response(200, content=b"not a zip"))
    respx.get(CHECKSUM_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(ChecksumMismatch):
        await BinanceVisionClient(client).fetch_day(SYMBOL, DAY)


def test_unexpected_header_is_rejected() -> None:
    """A silent column reorder upstream would map open interest onto a ratio."""
    reordered = list(EXPECTED_HEADER)
    reordered[2], reordered[3] = reordered[3], reordered[2]
    body = ",".join(reordered) + "\n"

    with pytest.raises(UpstreamDataError, match="unexpected header"):
        parse_metrics_csv(body.encode())


def test_bad_decimal_is_rejected() -> None:
    body = ",".join(EXPECTED_HEADER) + "\n2026-08-01 00:00:00,BTCUSDT,abc,1,1,1,1,1\n"

    with pytest.raises(UpstreamDataError, match="bad decimal"):
        parse_metrics_csv(body.encode())


def test_bad_timestamp_is_rejected() -> None:
    body = ",".join(EXPECTED_HEADER) + "\nnot-a-date,BTCUSDT,1,1,1,1,1,1\n"

    with pytest.raises(UpstreamDataError, match="bad timestamp"):
        parse_metrics_csv(body.encode())


def test_empty_file_is_rejected() -> None:
    with pytest.raises(UpstreamDataError, match="empty metrics file"):
        parse_metrics_csv(b"")


def test_blank_fields_become_null() -> None:
    """Upstream publishes reporting gaps as empty columns, not as an error."""
    body = (
        ",".join(EXPECTED_HEADER)
        + "\n2023-11-11 22:00:00,BTCUSDT,1000,50000,,,,0.838\n"
    )

    rows = parse_metrics_csv(body.encode())

    assert len(rows) == 1
    assert rows[0].open_interest == Decimal("1000")
    assert rows[0].toptrader_long_short_account_ratio is None
    assert rows[0].toptrader_long_short_position_ratio is None
    assert rows[0].taker_long_short_volume_ratio == Decimal("0.838")


def test_zero_open_interest_becomes_null() -> None:
    """The real 2023-11-11 22:00 BTCUSDT row.

    A perpetual with trading activity never holds zero open interest. Stored as
    a value, this would read to the deleveraging setup as a 100% drop: a false
    trigger written permanently into history and counted as real by the gate.
    """
    body = (
        ",".join(EXPECTED_HEADER)
        + "\n2023-11-11 22:00:00,BTCUSDT,0E-8,0E-8,,,,0.83835742\n"
    )

    rows = parse_metrics_csv(body.encode())

    assert rows[0].open_interest is None
    assert rows[0].open_interest_value is None


def test_nonzero_open_interest_is_kept() -> None:
    """The guard must not swallow legitimate small values."""
    body = (
        ",".join(EXPECTED_HEADER)
        + "\n2026-08-01 00:00:00,BTCUSDT,0.00000001,1,1,1,1,1\n"
    )

    rows = parse_metrics_csv(body.encode())

    assert rows[0].open_interest == Decimal("0.00000001")
