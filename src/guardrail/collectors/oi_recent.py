"""Recent open interest from the Binance REST endpoint.

The Binance Vision daily dumps are complete (open interest and the positioning
ratios) but publish a day late. This endpoint covers only the last 30 days and
only open interest, and fills the gap between the newest dump and now, which is
what keeps the positioning block on the screen current.

Both sources write the same rows (instrument, 5-minute timestamp);
`store_metrics` fills each column from whichever source has it and never
overwrites a value already present.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from guardrail.collectors.backfill import DEFAULT_SYMBOLS, VENUE
from guardrail.collectors.binance_vision import MetricRow
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.store import (
    get_or_create_instrument,
    record_failure,
    record_success,
    store_metrics,
)
from guardrail.db.models import AssetClass
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

URL: Final = "https://fapi.binance.com/futures/data/openInterestHist"
PERIOD: Final = "5m"
# Rows per request: 5 hours of 5-minute samples, plenty for a run every 15 minutes
# and for catching up after a short outage. The endpoint allows up to 500.
DEFAULT_LIMIT: Final = 60
MAX_LIMIT: Final = 500
# A sample stamped after now is not a reading yet.
FUTURE_TOLERANCE: Final = timedelta(minutes=5)


def source_name(symbol: str) -> str:
    """Health key of one symbol."""
    return f"{VENUE}.oi_recent.{symbol}"


def _positive(value: Any) -> Decimal | None:
    """A reading, or None. Zero is how the venue publishes a reporting gap, and
    stored as a value it reads as open interest collapsing to nothing."""
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise UpstreamDataError(f"bad decimal: {value!r}") from None
    return number if number > 0 else None


def parse_rows(payload: Any, now: datetime) -> tuple[list[MetricRow], int]:
    """Metric rows from the response, and how many entries were unreadable."""
    if not isinstance(payload, list):
        # The venue reports errors as a 200 with an object body.
        raise UpstreamDataError(f"expected a list, got: {str(payload)[:120]}")
    rows: list[MetricRow] = []
    bad = 0
    for item in payload:
        try:
            ts = datetime.fromtimestamp(int(item["timestamp"]) / 1000, tz=UTC)
            oi = _positive(item["sumOpenInterest"])
            value = _positive(item["sumOpenInterestValue"])
        except (KeyError, TypeError, ValueError, UpstreamDataError):
            bad += 1
            continue
        if ts > now + FUTURE_TOLERANCE:
            bad += 1
            continue
        rows.append(MetricRow(ts, oi, value, None, None, None))
    if payload and bad > len(rows):
        raise UpstreamDataError(
            f"most open interest rows unreadable: {bad} bad, {len(rows)} good"
        )
    return rows, bad


class OiRecentClient:
    """Reads the recent open interest history."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def fetch(self, symbol: str, limit: int = DEFAULT_LIMIT) -> Any:
        """The raw response for one symbol."""
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        response = await self._client.get(
            URL, params={"symbol": symbol, "period": PERIOD, "limit": limit}
        )
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True, slots=True)
class OiResult:
    """Outcome for one symbol."""

    symbol: str
    rows: int
    written: int
    unreadable: int
    error: str | None = None


async def run(
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS, limit: int = DEFAULT_LIMIT
) -> list[OiResult]:
    """Entry point: refresh every symbol, record each one's health."""
    results: list[OiResult] = []
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = OiRecentClient(http)
        for symbol in symbols:
            try:
                payload = await client.fetch(symbol, limit)
                rows, bad = parse_rows(payload, datetime.now(UTC))
                with session_scope() as session:
                    instrument = get_or_create_instrument(
                        session, symbol, VENUE, AssetClass.CRYPTO
                    )
                    written = store_metrics(session, instrument.id, rows)
                    record_success(session, source_name(symbol))
                results.append(OiResult(symbol, len(rows), written, bad))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                log.warning("oi_recent.failed", symbol=symbol, error=message)
                with session_scope() as session:
                    record_failure(session, source_name(symbol), message)
                results.append(OiResult(symbol, 0, 0, 0, error=message))
    return results


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Refresh recent open interest.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    try:
        results = asyncio.run(run(limit=args.limit))
    finally:
        dispose_engine()
    for r in results:
        status = (
            f"ERROR {r.error}" if r.error else f"{r.rows} rows, {r.written} written"
        )
        print(f"{r.symbol}: {status}")
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
