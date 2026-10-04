"""Historical open interest backfill from Binance Vision.

Resumable and idempotent: days already complete in the database are skipped, so
re-running after an interruption costs only the remaining days.
"""

import argparse
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import structlog
from sqlalchemy import func, select

from guardrail.collectors.binance_vision import BinanceVisionClient, MetricRow
from guardrail.collectors.store import (
    get_or_create_instrument,
    record_failure,
    record_success,
    store_metrics,
)
from guardrail.db.models import AssetClass, DerivativeStat
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

VENUE = "binance"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DEFAULT_DAYS = 365 * 3

# Rows a complete day holds: one sample every 5 minutes.
ROWS_PER_DAY = 288

# Concurrent downloads. Sequential would take too long over ~5300 files;
# unbounded risks being throttled by the host.
MAX_CONCURRENCY = 8


@dataclass(frozen=True)
class MetricsBackfillResult:
    """Outcome of one symbol's metrics backfill."""

    symbol: str
    days_requested: int
    days_skipped: int
    days_missing: int
    rows_written: int
    error: str | None = None


def _day_range(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _complete_days(instrument_id: int, start: date, end: date) -> set[date]:
    """Return days already holding a full set of samples.

    One grouped query per symbol rather than one existence check per day: the
    latter would issue over a thousand round trips for a three-year window.
    """
    with session_scope() as session:
        rows = session.execute(
            select(
                # AT TIME ZONE 'UTC' makes the grouping independent of the
                # session zone, so this query is correct even when run by hand
                # from a client that did not pin UTC.
                func.date_trunc(
                    "day", DerivativeStat.ts.op("AT TIME ZONE")("UTC")
                ).label("day"),
                func.count().label("samples"),
            )
            .where(
                DerivativeStat.instrument_id == instrument_id,
                DerivativeStat.ts >= datetime.combine(start, datetime.min.time(), UTC),
                DerivativeStat.ts
                < datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC),
                DerivativeStat.open_interest.is_not(None),
            )
            .group_by("day")
            .having(func.count() >= ROWS_PER_DAY)
        ).all()
    return {row.day.date() for row in rows}


async def backfill_metrics_symbol(
    client: BinanceVisionClient,
    symbol: str,
    start: date,
    end: date,
) -> MetricsBackfillResult:
    """Download and store every missing day for one symbol."""
    source = f"binance.vision.metrics.{symbol}"
    with session_scope() as session:
        instrument = get_or_create_instrument(session, symbol, VENUE, AssetClass.CRYPTO)
        instrument_id = instrument.id

    already = _complete_days(instrument_id, start, end)
    pending = [day for day in _day_range(start, end) if day not in already]
    total = sum(1 for _ in _day_range(start, end))

    log.info(
        "metrics_backfill.planned",
        symbol=symbol,
        total_days=total,
        skipped=len(already),
        pending=len(pending),
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    written = 0
    missing = 0
    # Which days the host never published. Kept so the completeness query can
    # distinguish "upstream gap" from "we failed to fetch it".
    missing_days: list[date] = []

    async def fetch(day: date) -> tuple[date, list[MetricRow]]:
        async with semaphore:
            return day, await client.fetch_day(symbol, day)

    try:
        for batch_start in range(0, len(pending), MAX_CONCURRENCY * 4):
            batch = pending[batch_start : batch_start + MAX_CONCURRENCY * 4]
            results = await asyncio.gather(*(fetch(day) for day in batch))

            # Commit per batch, not at the end: an interrupted run keeps what it
            # already downloaded.
            with session_scope() as session:
                for day, rows in results:
                    if not rows:
                        missing += 1
                        missing_days.append(day)
                        continue
                    written += store_metrics(session, instrument_id, rows)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, source, f"{type(exc).__name__}: {exc}")
        log.error("metrics_backfill.failed", symbol=symbol, error=str(exc))
        return MetricsBackfillResult(
            symbol, total, len(already), missing, written, error=str(exc)
        )

    with session_scope() as session:
        record_success(session, source)

    if missing_days:
        log.warning(
            "metrics_backfill.upstream_gaps",
            symbol=symbol,
            count=len(missing_days),
            first=missing_days[0].isoformat(),
            last=missing_days[-1].isoformat(),
        )

    return MetricsBackfillResult(symbol, total, len(already), missing, written)


async def run_metrics_backfill(
    symbols: tuple[str, ...], days: int
) -> list[MetricsBackfillResult]:
    """Backfill metrics for every symbol."""
    end = datetime.now(UTC).date() - timedelta(days=1)
    start = end - timedelta(days=days)

    results = []
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = BinanceVisionClient(http)
        for symbol in symbols:
            results.append(await backfill_metrics_symbol(client, symbol, start, end))
    return results


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill historical open interest from Binance Vision."
    )
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    try:
        results = asyncio.run(run_metrics_backfill(tuple(args.symbols), args.days))
    finally:
        dispose_engine()

    print()
    header = (
        f"{'SYMBOL':<10} {'DAYS':>6} {'SKIPPED':>8} {'MISSING':>8} {'ROWS':>9}  STATUS"
    )
    print(header)
    for r in results:
        status = "ok" if r.error is None else f"FAILED: {r.error[:30]}"
        print(
            f"{r.symbol:<10} {r.days_requested:>6} {r.days_skipped:>8} "
            f"{r.days_missing:>8} {r.rows_written:>9}  {status}"
        )
    print()
    print(f"total rows written: {sum(r.rows_written for r in results)}")

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
