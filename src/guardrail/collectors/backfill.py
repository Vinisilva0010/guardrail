"""Historical candle backfill.

Resumable by construction: the start point is derived from what is already
stored, so an interrupted run is recovered by running the same command again.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.collectors.binance import SUPPORTED_INTERVALS, BinanceClient
from guardrail.collectors.store import (
    get_or_create_instrument,
    record_failure,
    record_success,
    store_klines,
)
from guardrail.db.models import AssetClass, Candle
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

VENUE = "binance"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DEFAULT_TIMEFRAMES = ("1h", "4h", "1d")
DEFAULT_DAYS = 365 * 3

TIMEFRAME_DELTA = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True)
class BackfillResult:
    """Outcome of a single symbol/timeframe backfill."""

    symbol: str
    timeframe: str
    fetched: int
    inserted: int
    error: str | None = None


def _resume_point(
    session: Session, instrument_id: int, timeframe: str, default_start: datetime
) -> datetime:
    """Return where to start fetching for this symbol and timeframe.

    Resuming from the newest stored bar alone is not enough: if the requested
    window reaches further back than what is stored, the earlier part would be
    skipped forever. That happens whenever a short test run has already written
    recent bars.

    So when there is a gap behind the oldest stored bar, start from the
    requested beginning and let ON CONFLICT DO NOTHING discard the overlap.
    """
    # MIN and MAX return NULL when no rows match, which is the first-run case.
    # The annotation states that; without it the checker assumes a value is
    # always present and marks the empty-table branch unreachable.
    # Two scalar queries instead of one two-column row: scalar_one_or_none
    # already carries the "may be None" type, so this does not depend on the
    # internal shape of Row, which differs across SQLAlchemy versions. The extra
    # round trip is negligible at nine calls per full backfill.
    stored = select(Candle.ts).where(
        Candle.instrument_id == instrument_id, Candle.timeframe == timeframe
    )
    oldest = session.execute(
        stored.order_by(Candle.ts.asc()).limit(1)
    ).scalar_one_or_none()
    newest = session.execute(
        stored.order_by(Candle.ts.desc()).limit(1)
    ).scalar_one_or_none()

    if newest is None or oldest is None:
        return default_start

    # Tolerance of one bar: the requested start moves forward every run while
    # the oldest stored bar stays put, so a bare "oldest > default_start" would
    # refetch the whole history on every execution.
    if oldest - default_start > TIMEFRAME_DELTA[timeframe]:
        log.info(
            "backfill.gap_behind_oldest",
            instrument_id=instrument_id,
            timeframe=timeframe,
            oldest=oldest.isoformat(),
            requested_start=default_start.isoformat(),
        )
        return default_start

    return newest + TIMEFRAME_DELTA[timeframe]


async def backfill_symbol(
    client: BinanceClient,
    symbol: str,
    timeframe: str,
    default_start: datetime,
) -> BackfillResult:
    """Fetch and store one symbol/timeframe, resuming from stored data."""
    source = f"{VENUE}.klines.{symbol}.{timeframe}"
    with session_scope() as session:
        instrument = get_or_create_instrument(session, symbol, VENUE, AssetClass.CRYPTO)
        instrument_id = instrument.id
        start = _resume_point(session, instrument_id, timeframe, default_start)

    try:
        klines = await client.fetch_klines(symbol, timeframe, start)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, source, f"{type(exc).__name__}: {exc}")
        log.error("backfill.failed", symbol=symbol, timeframe=timeframe, error=str(exc))
        return BackfillResult(symbol, timeframe, 0, 0, error=str(exc))

    with session_scope() as session:
        inserted = store_klines(session, instrument_id, timeframe, klines)
        record_success(session, source)

    return BackfillResult(symbol, timeframe, len(klines), inserted)


async def run_backfill(
    symbols: tuple[str, ...],
    timeframes: tuple[str, ...],
    days: int,
) -> list[BackfillResult]:
    """Backfill every symbol/timeframe pair, sequentially per symbol."""
    default_start = datetime.now(UTC) - timedelta(days=days)
    results: list[BackfillResult] = []

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = BinanceClient(http)
        for symbol in symbols:
            for timeframe in timeframes:
                results.append(
                    await backfill_symbol(client, symbol, timeframe, default_start)
                )
    return results


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Backfill historical candles.")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    unsupported = set(args.timeframes) - SUPPORTED_INTERVALS
    if unsupported:
        parser.error(f"unsupported timeframes: {sorted(unsupported)}")

    try:
        results = asyncio.run(
            run_backfill(tuple(args.symbols), tuple(args.timeframes), args.days)
        )
    finally:
        dispose_engine()

    failures = [r for r in results if r.error]
    print()
    print(f"{'SYMBOL':<12} {'TF':<4} {'FETCHED':>9} {'INSERTED':>9}  STATUS")
    for r in results:
        status = "ok" if r.error is None else f"FAILED: {r.error[:40]}"
        print(
            f"{r.symbol:<12} {r.timeframe:<4} {r.fetched:>9} {r.inserted:>9}  {status}"
        )
    print()
    print(f"total inserted: {sum(r.inserted for r in results)}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
