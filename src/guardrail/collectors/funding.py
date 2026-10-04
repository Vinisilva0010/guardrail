"""Binance funding rate history.

Public endpoint, no API key. Funding settles every 8 hours, so three years is
roughly 3,285 records per symbol: four pages at the maximum page size.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from pydantic import BaseModel, ConfigDict
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from guardrail.collectors.errors import UpstreamDataError

log = structlog.get_logger(__name__)

BASE_URL: Final = "https://fapi.binance.com"
FUNDING_PATH: Final = "/fapi/v1/fundingRate"

# Verified against the live endpoint: 1000 is accepted, 1500 returns
# {"status": "ERROR", ... "illegal params."} with a 200 status code.
MAX_LIMIT: Final = 1000


class FundingRate(BaseModel):
    """One funding settlement."""

    model_config = ConfigDict(frozen=True)

    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal | None

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> "FundingRate":
        try:
            raw_mark = str(item.get("markPrice", "")).strip()
            return cls(
                funding_time=datetime.fromtimestamp(
                    int(item["fundingTime"]) / 1000, tz=UTC
                ),
                funding_rate=Decimal(str(item["fundingRate"])),
                # Historical records carry an empty markPrice; only recent ones
                # populate it. Empty means absent, not zero.
                mark_price=Decimal(raw_mark) if raw_mark else None,
            )
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            raise UpstreamDataError(f"malformed funding record: {exc}") from exc


class FundingClient:
    """Fetches funding rate history, paginating forward in time."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get_page(self, symbol: str, start_ms: int) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{BASE_URL}{FUNDING_PATH}",
            params={"symbol": symbol, "startTime": start_ms, "limit": MAX_LIMIT},
        )
        response.raise_for_status()
        payload = response.json()

        # The venue signals errors with a 200 status and an object body, so the
        # HTTP code alone cannot be trusted to mean success.
        if not isinstance(payload, list):
            raise UpstreamDataError(f"expected a list, got: {payload}")
        return payload

    async def fetch_funding(
        self, symbol: str, start: datetime, end: datetime | None = None
    ) -> list[FundingRate]:
        """Return every funding settlement in [start, end), oldest first.

        start must be a real timestamp: startTime=0 is ignored by the venue,
        which then returns the most recent page instead of the oldest.
        """
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        start_ms = int(start.timestamp() * 1000)
        if start_ms <= 0:
            raise ValueError("start must be after the epoch")

        now = datetime.now(UTC)
        # Never past the present: the venue can publish a settlement whose time
        # has not arrived when a call lands near the 8-hour boundary. Storing an
        # unsettled rate is the same defect as storing an unclosed candle.
        end_ms = int(min(end or now, now).timestamp() * 1000)
        cursor_ms = start_ms
        collected: list[FundingRate] = []
        seen: set[datetime] = set()

        while cursor_ms < end_ms:
            page = await self._get_page(symbol, cursor_ms)
            if not page:
                break

            for item in page:
                record = FundingRate.from_payload(item)
                if record.funding_time in seen:
                    continue
                if int(record.funding_time.timestamp() * 1000) >= end_ms:
                    continue
                seen.add(record.funding_time)
                collected.append(record)

            next_cursor = int(page[-1]["fundingTime"]) + 1
            if next_cursor <= cursor_ms:
                log.warning("funding.cursor_stalled", symbol=symbol, cursor=cursor_ms)
                break
            cursor_ms = next_cursor

            if len(page) < MAX_LIMIT:
                break
            await asyncio.sleep(0.2)

        collected.sort(key=lambda r: r.funding_time)
        log.info("funding.fetched", symbol=symbol, count=len(collected))
        return collected
