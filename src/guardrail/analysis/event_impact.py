"""Historical distribution of price moves around scheduled events.

This is the only kind of "projection" the system shows (PLANO_ANTECIPACAO §2):
not a forecast, an empirical distribution with its sample size in plain view.
Because it is shown, it has to be right, and so every shortcut that would flatter
it is closed:

* one sample per (kind, asset, event day). Core CPI and CPI come out at the same
  instant; counting both would double a single market reaction and inflate n;
* a window that holds a discontinuity from SPEC §7.1 (a recorded corporate
  action, an unexplained jump above 8x, a dormant code that came back) is
  dropped from the sample, not adjusted;
* a window with a hole in the bars is dropped too: a return measured across a
  week with no data is not a one-day return;
* below MIN_SAMPLE the figures are stored but the screen must not print them.

Returns are fractions (0.012 is 1.2%), computed from closes as
close(target) / close(base) - 1, where base is the last bar before the event day
and target is the k-th bar after it (k = 1, 3, 5 for d1, d3, w1).
"""

import argparse
import enum
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Final

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.db.models import (
    Candle,
    CorporateAction,
    EventImpact,
    EventImpactStat,
    EventKind,
    EventReferenceAsset,
    EventScope,
    Instrument,
    ScheduledEvent,
)
from guardrail.db.session import dispose_engine, session_scope
from guardrail.quality.completeness import IMPLAUSIBLE_PRICE_FACTOR, MAX_DORMANT_DAYS

log = structlog.get_logger(__name__)

# horizon -> k-th bar after the event day.
HORIZONS: Final = {"d1": 1, "d3": 3, "w1": 5}

# Below this many samples the screen prints "historico insuficiente (n=N)".
MIN_SAMPLE: Final = 8

# Longest gap between consecutive bars that is still a normal weekend or holiday.
MAX_BAR_GAP_DAYS: Final = 6

# Kinds that are not price events.
EXCLUDED_KINDS: Final = frozenset({EventKind.HOLIDAY, EventKind.OTHER})

# Market-wide events enter the sample only for the markets the operator trades,
# and only when the source rates them high impact. Mixing a German price index
# with a US one under the same kind would measure nothing.
MARKET_WIDE_SCOPES: Final = frozenset({EventScope.US, EventScope.BR})

# Bars fetched around the events of one asset.
LOOKBACK: Final = timedelta(days=10)
LOOKAHEAD: Final = timedelta(days=30)

QUANT: Final = Decimal("0.00000001")


class Skip(enum.StrEnum):
    """Why an event contributed no sample."""

    NO_BASE = "no_base_bar"
    NO_TARGET = "no_target_bar"
    CORPORATE_ACTION = "corporate_action"
    PRICE_JUMP = "price_jump"
    DORMANT = "dormant_code"
    HOLE = "hole_in_bars"


@dataclass(frozen=True, slots=True)
class Bar:
    """A daily close."""

    day: date
    close: Decimal


@dataclass(frozen=True, slots=True)
class Sample:
    """The move around one event, or the reason there is none."""

    ret: Decimal | None
    skip: Skip | None = None


@dataclass(frozen=True, slots=True)
class Stats:
    """Summary of a set of moves. All figures are None when n is 0."""

    n: int
    median_signed: Decimal | None
    median_abs: Decimal | None
    p10: Decimal | None
    p90: Decimal | None
    pct_positive: Decimal | None


def is_displayable(n: int) -> bool:
    """Whether a sample is large enough to be shown as numbers."""
    return n >= MIN_SAMPLE


def measure(
    bars: Sequence[Bar],
    event_day: date,
    steps: int,
    action_days: Collection[date] = (),
) -> Sample:
    """The move from the last close before `event_day` to the `steps`-th close after.

    `bars` must be sorted by day, oldest first.
    """
    base = None
    first_after = None
    for index, bar in enumerate(bars):
        if bar.day < event_day:
            base = index
        elif bar.day > event_day:
            first_after = index
            break
    if base is None:
        return Sample(None, Skip.NO_BASE)
    if first_after is None or first_after + steps - 1 >= len(bars):
        return Sample(None, Skip.NO_TARGET)
    target = first_after + steps - 1

    path = bars[base : target + 1]
    first_day, last_day = path[0].day, path[-1].day
    if any(first_day <= d <= last_day for d in action_days):
        return Sample(None, Skip.CORPORATE_ACTION)

    for before, after in pairwise(path):
        gap = (after.day - before.day).days
        if gap > MAX_DORMANT_DAYS:
            return Sample(None, Skip.DORMANT)
        if gap > MAX_BAR_GAP_DAYS:
            return Sample(None, Skip.HOLE)
        ratio = after.close / before.close
        if ratio > IMPLAUSIBLE_PRICE_FACTOR or ratio < 1 / IMPLAUSIBLE_PRICE_FACTOR:
            return Sample(None, Skip.PRICE_JUMP)

    return Sample(path[-1].close / path[0].close - 1)


def quantile(sorted_values: Sequence[Decimal], q: Decimal) -> Decimal:
    """Linear-interpolated quantile (the common "type 7" definition).

    For n sorted values the position is (n - 1) * q; between two neighbours the
    result is interpolated. One value is its own every quantile.
    """
    if not sorted_values:
        raise ValueError("quantile of an empty sample")
    position = (len(sorted_values) - 1) * q
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = position - low
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction


def _q(value: Decimal) -> Decimal:
    return value.quantize(QUANT, rounding=ROUND_HALF_EVEN)


def summarize(returns: Sequence[Decimal]) -> Stats:
    """Median, median absolute move, 10th and 90th percentile, share positive."""
    n = len(returns)
    if n == 0:
        return Stats(0, None, None, None, None, None)
    ordered = sorted(returns)
    absolute = sorted(abs(r) for r in returns)
    half = Decimal("0.5")
    return Stats(
        n=n,
        median_signed=_q(quantile(ordered, half)),
        median_abs=_q(quantile(absolute, half)),
        p10=_q(quantile(ordered, Decimal("0.1"))),
        p90=_q(quantile(ordered, Decimal("0.9"))),
        pct_positive=_q(Decimal(sum(1 for r in returns if r > 0)) / Decimal(n)),
    )


@dataclass(slots=True)
class Report:
    """What a recomputation did."""

    rows: int = 0
    samples: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    absent_reference: list[str] = field(default_factory=list)


def reference_instruments(session: Session) -> tuple[list[int], list[str]]:
    """Instrument ids of the reference assets, and the declared ones with no candles."""
    rows = session.execute(
        select(EventReferenceAsset.venue, EventReferenceAsset.symbol, Instrument.id)
        .outerjoin(
            Instrument,
            (Instrument.venue == EventReferenceAsset.venue)
            & (Instrument.symbol == EventReferenceAsset.symbol),
        )
        .order_by(EventReferenceAsset.id)
    ).all()
    present = [row.id for row in rows if row.id is not None]
    absent = [f"{row.venue}:{row.symbol}" for row in rows if row.id is None]
    return present, absent


def _load_bars(
    session: Session, instrument_id: int, start: date, end: date
) -> list[Bar]:
    rows = session.execute(
        select(Candle.ts, Candle.close)
        .where(
            Candle.instrument_id == instrument_id,
            Candle.timeframe == "1d",
            Candle.ts >= datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            Candle.ts <= datetime.combine(end, datetime.min.time(), tzinfo=UTC),
        )
        .order_by(Candle.ts)
    ).all()
    return [Bar(ts.astimezone(UTC).date(), close) for ts, close in rows]


def _load_action_days(session: Session, instrument_id: int) -> set[date]:
    return {
        d.astimezone(UTC).date()
        for d in session.execute(
            select(CorporateAction.effective_on).where(
                CorporateAction.instrument_id == instrument_id
            )
        ).scalars()
    }


def recompute(session: Session, now: datetime) -> Report:
    """Rebuild event_impact_stat from past events and stored candles.

    Replaces the whole table inside the caller's transaction: the table is
    derived, so a rerun with the same inputs leaves identical rows.
    """
    report = Report()
    reference, report.absent_reference = reference_instruments(session)
    if report.absent_reference:
        log.warning("event_impact.reference_absent", assets=report.absent_reference)

    events = session.execute(
        select(
            ScheduledEvent.kind, ScheduledEvent.instrument_id, ScheduledEvent.starts_at
        ).where(
            ScheduledEvent.starts_at < now,
            ScheduledEvent.kind.not_in(EXCLUDED_KINDS),
            (ScheduledEvent.instrument_id.is_not(None))
            | (
                (ScheduledEvent.impact == EventImpact.HIGH)
                & ScheduledEvent.scope.in_(MARKET_WIDE_SCOPES)
            ),
        )
    ).all()

    # (kind, instrument) -> distinct event days.
    days: dict[tuple[EventKind, int], set[date]] = defaultdict(set)
    for kind, instrument_id, starts_at in events:
        day = starts_at.astimezone(UTC).date()
        targets = [instrument_id] if instrument_id is not None else reference
        for target in targets:
            days[(kind, target)].add(day)

    by_instrument: dict[int, list[tuple[EventKind, set[date]]]] = defaultdict(list)
    for (kind, instrument_id), event_days in days.items():
        by_instrument[instrument_id].append((kind, event_days))

    rows: list[dict[str, object]] = []
    for instrument_id, groups in by_instrument.items():
        every_day = [d for _, ds in groups for d in ds]
        bars = _load_bars(
            session,
            instrument_id,
            min(every_day) - LOOKBACK,
            max(every_day) + LOOKAHEAD,
        )
        actions = _load_action_days(session, instrument_id)
        for kind, event_days in groups:
            for horizon, steps in HORIZONS.items():
                returns: list[Decimal] = []
                used: list[date] = []
                for day in sorted(event_days):
                    sample = measure(bars, day, steps, actions)
                    if sample.ret is None:
                        reason = sample.skip.value if sample.skip else "unknown"
                        report.skipped[reason] = report.skipped.get(reason, 0) + 1
                        continue
                    returns.append(sample.ret)
                    used.append(day)
                if not returns:
                    continue
                stats = summarize(returns)
                report.samples += stats.n
                rows.append(
                    {
                        "kind": kind,
                        "instrument_id": instrument_id,
                        "horizon": horizon,
                        "n": stats.n,
                        "median_signed": stats.median_signed,
                        "median_abs": stats.median_abs,
                        "p10": stats.p10,
                        "p90": stats.p90,
                        "pct_positive": stats.pct_positive,
                        "window_start": min(used),
                        "window_end": max(used),
                        "computed_at": now,
                    }
                )

    session.execute(delete(EventImpactStat))
    for start in range(0, len(rows), 1000):
        session.execute(pg_insert(EventImpactStat).values(rows[start : start + 1000]))
    report.rows = len(rows)
    return report


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(
        description="Recompute event impact statistics."
    ).parse_args()
    try:
        with session_scope() as session:
            report = recompute(session, datetime.now(UTC))
    finally:
        dispose_engine()
    print(f"\nrows written:      {report.rows}")
    print(f"samples used:      {report.samples}")
    print(f"samples skipped:   {report.skipped}")
    if report.absent_reference:
        print(f"reference with no candles: {', '.join(report.absent_reference)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
