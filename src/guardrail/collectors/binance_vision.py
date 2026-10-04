"""Binance Vision historical metrics client.

Binance publishes daily CSV archives of futures metrics at a static, public,
unauthenticated host. Unlike the REST endpoint, which only retains 30 days, these
archives reach back years, which is what makes the open interest condition of the
deleveraging setup testable at all.

Every archive is verified against its published SHA256 before being parsed:
storing corrupted open interest would skew the backtest without raising anything.
"""

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from guardrail.collectors.errors import ChecksumMismatch, UpstreamDataError

log = structlog.get_logger(__name__)

BASE_URL: Final = "https://data.binance.vision"
METRICS_PATH: Final = "data/futures/um/daily/metrics"

# Exact header published since at least 2021-12; verified identical in 2026-08.
# Checked on every file: a silent column reorder upstream would otherwise map
# open interest onto a positioning ratio.
EXPECTED_HEADER: Final = (
    "create_time",
    "symbol",
    "sum_open_interest",
    "sum_open_interest_value",
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)


@dataclass(frozen=True, slots=True)
class MetricRow:
    """One 5-minute metrics sample."""

    ts: datetime

    # Optional because upstream publishes reporting gaps as blank or zero. See
    # _parse_row for why zero is treated as absence rather than as a value.
    open_interest: Decimal | None
    open_interest_value: Decimal | None

    # Optional because the REST endpoint for the recent window returns open
    # interest only. Rows parsed from a Vision dump always carry all three.
    toptrader_long_short_account_ratio: Decimal | None
    toptrader_long_short_position_ratio: Decimal | None
    taker_long_short_volume_ratio: Decimal | None


def _parse_decimal(raw: str, field: str) -> Decimal | None:
    """Parse a decimal, treating a blank field as absent rather than as an error.

    Binance publishes reporting gaps as empty columns: on 2023-11-11 22:00 the
    BTCUSDT row carries blank positioning ratios. A blank is missing data, and
    the database columns are nullable precisely for this.
    """
    value = raw.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise UpstreamDataError(f"bad decimal in {field}: {raw!r}") from exc


def _parse_row(row: list[str]) -> MetricRow:
    if len(row) != len(EXPECTED_HEADER):
        raise UpstreamDataError(
            f"expected {len(EXPECTED_HEADER)} fields, got {len(row)}"
        )
    try:
        naive = datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise UpstreamDataError(f"bad timestamp: {row[0]!r}") from exc

    # Binance publishes in UTC and the column carries no offset. Attaching UTC
    # explicitly keeps these rows aligned with the candles; letting Python infer
    # the local zone would shift them by the WSL host offset.
    # Zero open interest is a reporting gap, not a reading. A perpetual with any
    # trading activity never holds zero open interest, and storing the zero would
    # present itself to the deleveraging setup as a 100% drop: a false trigger
    # written permanently into history and counted as real by the backtest gate.
    open_interest = _parse_decimal(row[2], "sum_open_interest")
    open_interest_value = _parse_decimal(row[3], "sum_open_interest_value")
    if open_interest == 0:
        open_interest = None
        open_interest_value = None

    return MetricRow(
        ts=naive.replace(tzinfo=UTC),
        open_interest=open_interest,
        open_interest_value=open_interest_value,
        toptrader_long_short_account_ratio=_parse_decimal(row[4], "toptrader_account"),
        toptrader_long_short_position_ratio=_parse_decimal(
            row[5], "toptrader_position"
        ),
        taker_long_short_volume_ratio=_parse_decimal(row[7], "taker_volume"),
    )


def parse_metrics_csv(content: bytes) -> list[MetricRow]:
    """Parse a decompressed metrics CSV, validating its header first."""
    reader = csv.reader(io.StringIO(content.decode("utf-8")))
    try:
        header = next(reader)
    except StopIteration:
        raise UpstreamDataError("empty metrics file") from None

    actual = tuple(h.strip() for h in header)
    if actual != EXPECTED_HEADER:
        raise UpstreamDataError(f"unexpected header: {actual}")

    return [_parse_row(row) for row in reader if row]


class BinanceVisionClient:
    """Downloads and verifies daily metrics archives."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def _urls(self, symbol: str, day: date) -> tuple[str, str]:
        name = f"{symbol}-metrics-{day.isoformat()}.zip"
        archive = f"{BASE_URL}/{METRICS_PATH}/{symbol}/{name}"
        return archive, f"{archive}.CHECKSUM"

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, ChecksumMismatch)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def fetch_day(self, symbol: str, day: date) -> list[MetricRow]:
        """Return every metrics row for one day, or [] if not published.

        A missing day is not an error: publication has gaps, and the caller
        records the gap rather than aborting a multi-year backfill.
        """
        archive_url, checksum_url = self._urls(symbol, day)

        response = await self._client.get(archive_url)
        if response.status_code == 404:
            log.info("vision.day_missing", symbol=symbol, day=day.isoformat())
            return []
        response.raise_for_status()
        payload = response.content

        checksum_response = await self._client.get(checksum_url)
        if checksum_response.status_code == 200:
            expected = checksum_response.text.split()[0].strip().lower()
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise ChecksumMismatch(
                    f"{symbol} {day.isoformat()}: expected {expected}, got {actual}"
                )
        else:
            log.warning("vision.no_checksum", symbol=symbol, day=day.isoformat())

        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                if len(names) != 1:
                    raise UpstreamDataError(
                        f"expected 1 file in archive, got {len(names)}"
                    )
                content = archive.read(names[0])
        except zipfile.BadZipFile as exc:
            raise ChecksumMismatch(
                f"{symbol} {day.isoformat()}: corrupt archive"
            ) from exc

        return parse_metrics_csv(content)
