"""Tradable universe construction.

The universe is derived from a rule and recalculated periodically. No symbol is
written by hand anywhere in this project: a fixed list freezes on the day it was
written and misses whatever appears afterwards, which is what the system exists
to find. See SPEC section 9.1.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Final

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import AlpacaClient, DailyBar
from guardrail.collectors.store import get_or_create_instrument
from guardrail.db.models import AssetClass, Instrument, UniverseMembership
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

VENUE: Final = "alpaca"

# Median daily dollar volume required to join the universe. Chosen from the
# measured distribution, not estimated: the universe median is about US$1.25M a
# day. A US$1M floor admits roughly 4,500 symbols, many in a band where an order
# of a few thousand dollars moves the price — the breakout shows on the chart but
# is not executable. A US$50M floor leaves 1,600 and cuts small and mid caps,
# which is where 20-30% moves happen. See SPEC section 9.1.
DEFAULT_THRESHOLD_USD: Final = Decimal("20000000")

# Window for the liquidity measurement.
LOOKBACK_DAYS: Final = 90

# Minimum bars required to trust the median: a symbol that only traded a handful
# of sessions in the window has no stable liquidity to measure.
MIN_BARS: Final = 40


@dataclass(frozen=True)
class UniverseChange:
    """What one rebuild changed."""

    measured: int
    passed: int
    added: int
    removed: int
    unchanged: int


def _median_dollar_volume(bars: list[DailyBar]) -> Decimal | None:
    """Median of close x volume across the window.

    Median, not mean: a single news day can multiply a symbol's volume twenty
    times, and a mean would admit an illiquid symbol on the strength of one
    outlier session.
    """
    if len(bars) < MIN_BARS:
        return None
    return Decimal(median(sorted(b.close * b.volume for b in bars)))


async def measure_liquidity(
    client: AlpacaClient, symbols: list[str], lookback_days: int = LOOKBACK_DAYS
) -> dict[str, Decimal]:
    """Return median daily dollar volume per symbol."""
    start = date.today() - timedelta(days=lookback_days)
    bars = await client.fetch_daily_bars(symbols, start)

    out: dict[str, Decimal] = {}
    for symbol, symbol_bars in bars.items():
        value = _median_dollar_volume(symbol_bars)
        if value is not None:
            out[symbol] = value
    log.info("universe.liquidity_measured", requested=len(symbols), measured=len(out))
    return out


def apply_universe(
    session: Session, liquidity: dict[str, Decimal], threshold: Decimal
) -> UniverseChange:
    """Reconcile the computed universe against the stored one.

    Symbols already in the universe and still passing are left untouched, so a
    repeated run does not create duplicate membership rows.

    Takes a session rather than opening one. Anything absent from `liquidity` is
    treated as having left the universe, so this must be able to run inside a
    caller's transaction — a test that opens its own connection here would close
    every real membership and the rollback would not undo it.
    """
    now = datetime.now(UTC)
    passing = {s: v for s, v in liquidity.items() if v >= threshold}

    added = 0
    # Declared outside the session block: it is read after the block closes, and
    # on a run where nobody leaves the universe the assignment inside would never
    # happen, leaving the later len() referencing an undefined name.
    leaving: list[int] = []

    # symbol -> id of the membership row that is still open.
    current: dict[str, int] = {
        symbol: membership_id
        for symbol, membership_id in session.execute(
            select(Instrument.symbol, UniverseMembership.id)
            .join(
                UniverseMembership,
                UniverseMembership.instrument_id == Instrument.id,
            )
            .where(UniverseMembership.exited_on.is_(None))
        ).all()
    }

    for symbol, value in passing.items():
        if symbol in current:
            continue
        instrument = get_or_create_instrument(session, symbol, VENUE, AssetClass.EQUITY)
        session.add(
            UniverseMembership(
                instrument_id=instrument.id,
                entered_on=now,
                median_dollar_volume=value,
                threshold_usd=threshold,
            )
        )
        added += 1

    leaving = [mid for symbol, mid in current.items() if symbol not in passing]
    if leaving:
        session.execute(
            update(UniverseMembership)
            .where(UniverseMembership.id.in_(leaving))
            .values(exited_on=now)
        )

    change = UniverseChange(
        measured=len(liquidity),
        passed=len(passing),
        added=added,
        removed=len(leaving),
        unchanged=len(passing) - added,
    )
    log.info("universe.rebuilt", **change.__dict__)
    return change


async def rebuild_universe(threshold: Decimal) -> UniverseChange:
    """Fetch assets, measure liquidity and reconcile membership."""
    async with httpx.AsyncClient(timeout=120.0) as http:
        client = AlpacaClient(http)
        assets = await client.fetch_assets()
        liquidity = await measure_liquidity(client, [a.symbol for a in assets])
    with session_scope() as session:
        return apply_universe(session, liquidity, threshold)


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Rebuild the tradable universe.")
    parser.add_argument(
        "--threshold",
        type=Decimal,
        default=DEFAULT_THRESHOLD_USD,
        help="minimum median daily dollar volume",
    )
    args = parser.parse_args()

    try:
        change = asyncio.run(rebuild_universe(args.threshold))
    finally:
        dispose_engine()

    print()
    print(f"threshold:   US$ {args.threshold:,.0f}")
    print(f"measured:    {change.measured}")
    print(f"passed:      {change.passed}")
    print(f"added:       {change.added}")
    print(f"removed:     {change.removed}")
    print(f"unchanged:   {change.unchanged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
