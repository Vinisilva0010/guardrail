"""Daily bar backfill for the equity universe.

Resumable per symbol: the start point comes from what is already stored, so an
interrupted run resumes instead of refetching.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import AlpacaClient, DailyBar, latest_available_day
from guardrail.collectors.binance import Kline
from guardrail.collectors.store import record_failure, record_success, store_klines
from guardrail.db.models import Candle, Instrument, UniverseMembership
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

TIMEFRAME: Final = "1d"
DEFAULT_DAYS: Final = 365 * 3
SOURCE: Final = "alpaca.bars"

# Symbols resolved per database round trip when looking up resume points.
RESUME_BATCH: Final = 500


@dataclass(frozen=True)
class EquityBackfillResult:
    """Outcome of one backfill run."""

    symbols: int
    fetched: int
    inserted: int
    skipped: int
    error: str | None = None


def _bar_to_kline(bar: DailyBar) -> Kline:
    """Adapt a daily bar to the shared storage model.

    store_klines already guarantees idempotent writes and is covered by tests;
    reusing it is safer than writing a second insert path for the same table.
    """
    opened = datetime(bar.day.year, bar.day.month, bar.day.day, tzinfo=UTC)
    return Kline(
        open_time=opened,
        close_time=opened + timedelta(days=1) - timedelta(milliseconds=1),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def load_universe(session: Session) -> dict[str, int]:
    """Return symbol to instrument id for the currently open memberships."""
    rows = session.execute(
        select(Instrument.symbol, Instrument.id)
        .join(UniverseMembership, UniverseMembership.instrument_id == Instrument.id)
        .where(UniverseMembership.exited_on.is_(None))
    ).all()
    return {symbol: instrument_id for symbol, instrument_id in rows}


def resume_points(
    session: Session, instrument_ids: list[int], default_start: date
) -> dict[int, date]:
    """Return where each instrument should resume from.

    One grouped query instead of one per symbol: with over two thousand symbols
    the per-symbol version would issue thousands of round trips.
    """
    stored = {
        iid: (oldest, newest)
        for iid, oldest, newest in session.execute(
            select(
                Candle.instrument_id,
                func.min(Candle.ts),
                func.max(Candle.ts),
            )
            .where(
                Candle.instrument_id.in_(instrument_ids),
                Candle.timeframe == TIMEFRAME,
            )
            .group_by(Candle.instrument_id)
        ).all()
    }

    out: dict[int, date] = {}
    for iid in instrument_ids:
        if iid not in stored:
            out[iid] = default_start
            continue
        oldest, newest = stored[iid]
        # Resuming from the newest bar alone is not enough: when the requested
        # window reaches further back than what is stored, the earlier part would
        # be skipped forever. That happens after any short run, which writes
        # recent bars and makes every symbol look up to date.
        if oldest.date() > default_start + timedelta(days=1):
            out[iid] = default_start
        else:
            out[iid] = newest.date() + timedelta(days=1)
    return out


async def run_equity_backfill(days: int) -> EquityBackfillResult:
    """Fetch and store daily bars for every symbol in the universe."""
    default_start = date.today() - timedelta(days=days)
    end = latest_available_day()

    with session_scope() as session:
        universe = load_universe(session)
        by_id = resume_points(session, list(universe.values()), default_start)

    if not universe:
        log.warning("equity_backfill.empty_universe")
        return EquityBackfillResult(0, 0, 0, 0)

    pending = {
        symbol: by_id[iid] for symbol, iid in universe.items() if by_id[iid] <= end
    }
    skipped = len(universe) - len(pending)
    log.info(
        "equity_backfill.planned",
        universe=len(universe),
        pending=len(pending),
        skipped=skipped,
    )
    if not pending:
        return EquityBackfillResult(len(universe), 0, 0, skipped)

    # Group symbols by their resume date so each request covers one window.
    by_start: dict[date, list[str]] = {}
    for symbol, start in pending.items():
        by_start.setdefault(start, []).append(symbol)

    fetched = inserted = 0
    try:
        async with httpx.AsyncClient(timeout=180.0) as http:
            client = AlpacaClient(http)
            for start, symbols in sorted(by_start.items()):
                bars = await client.fetch_daily_bars(symbols, start, end)
                with session_scope() as session:
                    for symbol, symbol_bars in bars.items():
                        instrument_id = universe.get(symbol)
                        if instrument_id is None:
                            continue
                        fetched += len(symbol_bars)
                        inserted += store_klines(
                            session,
                            instrument_id,
                            TIMEFRAME,
                            [_bar_to_kline(b) for b in symbol_bars],
                        )
                log.info(
                    "equity_backfill.window_done",
                    start=start.isoformat(),
                    symbols=len(symbols),
                    inserted=inserted,
                )
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, SOURCE, f"{type(exc).__name__}: {exc}")
        log.error("equity_backfill.failed", error=str(exc))
        return EquityBackfillResult(
            len(universe), fetched, inserted, skipped, error=str(exc)
        )

    with session_scope() as session:
        record_success(session, SOURCE)
    return EquityBackfillResult(len(universe), fetched, inserted, skipped)


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Backfill equity daily bars.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    try:
        result = asyncio.run(run_equity_backfill(args.days))
    finally:
        dispose_engine()

    print()
    print(f"symbols in universe: {result.symbols}")
    print(f"already up to date:  {result.skipped}")
    print(f"bars fetched:        {result.fetched}")
    print(f"bars inserted:       {result.inserted}")
    if result.error:
        print(f"FAILED: {result.error}")
    return 1 if result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
