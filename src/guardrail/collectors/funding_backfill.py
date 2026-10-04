"""Funding rate backfill.

Resumable: the start point comes from the newest settlement already stored.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import select

from guardrail.collectors.funding import FundingClient
from guardrail.collectors.store import (
    get_or_create_instrument,
    record_failure,
    record_success,
    store_funding,
)
from guardrail.db.models import AssetClass, DerivativeStat
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

VENUE = "binance"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DEFAULT_DAYS = 365 * 3

# Settlements occur every 8 hours.
SETTLEMENT_INTERVAL = timedelta(hours=8)


@dataclass(frozen=True)
class FundingBackfillResult:
    """Outcome of one symbol's funding backfill."""

    symbol: str
    fetched: int
    written: int
    error: str | None = None


def _resume_point(instrument_id: int, default_start: datetime) -> datetime:
    """Resume just after the newest stored settlement, or from the default."""
    with session_scope() as session:
        newest = session.execute(
            select(DerivativeStat.ts)
            .where(
                DerivativeStat.instrument_id == instrument_id,
                DerivativeStat.funding_rate.is_not(None),
            )
            .order_by(DerivativeStat.ts.desc())
            .limit(1)
        ).scalar_one_or_none()

    if newest is None:
        return default_start
    return newest + SETTLEMENT_INTERVAL


async def backfill_funding_symbol(
    client: FundingClient, symbol: str, default_start: datetime
) -> FundingBackfillResult:
    """Fetch and store funding for one symbol."""
    source = f"{VENUE}.funding.{symbol}"
    with session_scope() as session:
        instrument = get_or_create_instrument(session, symbol, VENUE, AssetClass.CRYPTO)
        instrument_id = instrument.id

    start = _resume_point(instrument_id, default_start)

    try:
        rows = await client.fetch_funding(symbol, start)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, source, f"{type(exc).__name__}: {exc}")
        log.error("funding_backfill.failed", symbol=symbol, error=str(exc))
        return FundingBackfillResult(symbol, 0, 0, error=str(exc))

    with session_scope() as session:
        written = store_funding(session, instrument_id, rows)
        record_success(session, source)

    return FundingBackfillResult(symbol, len(rows), written)


async def run_funding_backfill(
    symbols: tuple[str, ...], days: int
) -> list[FundingBackfillResult]:
    """Backfill funding for every symbol."""
    default_start = datetime.now(UTC) - timedelta(days=days)
    results = []
    async with httpx.AsyncClient(timeout=30.0) as http:
        client = FundingClient(http)
        for symbol in symbols:
            results.append(await backfill_funding_symbol(client, symbol, default_start))
    return results


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Backfill funding rate history.")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    try:
        results = asyncio.run(run_funding_backfill(tuple(args.symbols), args.days))
    finally:
        dispose_engine()

    print()
    print(f"{'SYMBOL':<10} {'FETCHED':>9} {'WRITTEN':>9}  STATUS")
    for r in results:
        status = "ok" if r.error is None else f"FAILED: {r.error[:40]}"
        print(f"{r.symbol:<10} {r.fetched:>9} {r.written:>9}  {status}")
    print()
    print(f"total written: {sum(r.written for r in results)}")

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
