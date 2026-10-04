"""Binance public market data client.

Only public endpoints are used: no API key is required and none is ever sent.
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)

BASE_URL: Final = "https://api.binance.com"
KLINES_PATH: Final = "/api/v3/klines"

# Binance caps a single klines response at 1000 rows.
MAX_LIMIT: Final = 1000

SUPPORTED_INTERVALS: Final = frozenset({"1h", "4h", "1d"})


class UpstreamDataError(ValueError):
    """The response was well-formed HTTP but the payload is unusable.

    Raised instead of a retryable error: re-requesting bad data returns bad data.
    """


class Kline(BaseModel):
    """A single closed OHLCV bar.

    Prices arrive from Binance as decimal strings. They are parsed straight into
    Decimal; routing them through float first would bake in rounding error that
    cannot be undone.
    """

    model_config = ConfigDict(frozen=True)

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("negative price or volume")
        return value

    @model_validator(mode="after")
    def _high_covers_low(self) -> "Kline":
        if self.high < self.low:
            raise ValueError("high below low")
        if self.close_time <= self.open_time:
            raise ValueError("close_time not after open_time")
        return self

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "Kline":
        """Build from Binance's positional array format."""
        if len(row) < 7:
            raise UpstreamDataError(f"kline row too short: {len(row)} fields")
        try:
            return cls(
                open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
                close_time=datetime.fromtimestamp(int(row[6]) / 1000, tz=UTC),
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise UpstreamDataError(f"malformed kline row: {exc}") from exc


def _is_retryable(exc: BaseException) -> bool:
    """Retry transport failures and rate limiting, never malformed data."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class BinanceClient:
    """Fetches closed klines, paginating over long ranges."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get_page(
        self, symbol: str, interval: str, start_ms: int, limit: int
    ) -> list[list[Any]]:
        response = await self._client.get(
            f"{BASE_URL}{KLINES_PATH}",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "limit": limit,
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if not _is_retryable(exc):
                raise UpstreamDataError(
                    f"binance rejected request: {exc.response.status_code}"
                ) from exc
            raise
        payload = response.json()
        if not isinstance(payload, list):
            raise UpstreamDataError(f"expected a list, got {type(payload).__name__}")
        return payload

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Kline]:
        """Return every closed bar in [start, end), oldest first.

        Bars whose close_time has not passed yet are dropped: an open bar still
        changes, and storing it would make backtests irreproducible.
        """
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(f"unsupported interval: {interval}")
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")

        now = datetime.now(UTC)
        end = end or now
        cursor_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        collected: list[Kline] = []
        seen_open_times: set[datetime] = set()

        while cursor_ms < end_ms:
            rows = await self._get_page(symbol, interval, cursor_ms, MAX_LIMIT)
            if not rows:
                break

            for row in rows:
                kline = Kline.from_row(row)
                if kline.close_time > now:
                    continue  # still open
                if kline.open_time in seen_open_times:
                    continue
                if int(kline.open_time.timestamp() * 1000) >= end_ms:
                    continue
                seen_open_times.add(kline.open_time)
                collected.append(kline)

            next_cursor_ms = int(rows[-1][0]) + 1
            if next_cursor_ms <= cursor_ms:
                # The page carried nothing past the cursor. This is the end of
                # available data, or a venue repeating a page; either way there
                # is no new data to fetch and looping again would never end.
                log.warning(
                    "binance.klines.cursor_stalled",
                    symbol=symbol,
                    interval=interval,
                    cursor_ms=cursor_ms,
                )
                break
            cursor_ms = next_cursor_ms

            if len(rows) < MAX_LIMIT:
                break

            # Courtesy pacing. Binance allows far more, but a backfill has no
            # reason to run at the edge of the limit.
            await asyncio.sleep(0.2)

        collected.sort(key=lambda k: k.open_time)
        log.info(
            "binance.klines.fetched",
            symbol=symbol,
            interval=interval,
            count=len(collected),
        )
        return collected
