"""Request budget for sources with a ceiling.

An unattended loop can spend a monthly allowance in one night. Every call to a
capped source is reserved here first; a refused reservation means the caller
makes no HTTP request at all.

Thresholds (PLANO_ANTECIPACAO §3.5): above 80% of the budget only closing
collection passes, above 95% nothing does.

The reservation counts the attempt, not the outcome. The vendor meters requests
it received, so a call that failed or was retried still spent budget. It runs in
the caller's session and so commits with the data the call produced.
"""

import enum
from datetime import UTC, date, datetime
from typing import Final

import structlog
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.db.models import ApiQuota

log = structlog.get_logger(__name__)

# Percent of budget. Integer arithmetic keeps the comparison exact.
SOFT_LIMIT_PCT: Final = 80
HARD_LIMIT_PCT: Final = 95


class CallKind(enum.StrEnum):
    """Why a request is being made."""

    CLOSE = "close"
    OTHER = "other"


class Period(enum.StrEnum):
    """How often a source's budget resets."""

    MONTH = "month"
    DAY = "day"


# source -> (budget, reset period). Free-tier ceilings of each vendor.
BUDGETS: Final[dict[str, tuple[int, Period]]] = {
    "brapi": (15_000, Period.MONTH),
    "alphavantage": (25, Period.DAY),
}


class QuotaExceeded(Exception):
    """The reservation was refused; the caller must not make the request."""


def period_start(period: Period, today: date) -> date:
    """First day of the budget period that contains today."""
    return today.replace(day=1) if period is Period.MONTH else today


def reserve(
    session: Session,
    source: str,
    kind: CallKind = CallKind.OTHER,
    cost: int = 1,
    today: date | None = None,
) -> int:
    """Spend `cost` requests of the source's budget. Returns the new `used`.

    Raises QuotaExceeded, leaving the counter untouched, when the spend would
    cross the limit that applies to this kind of call.
    """
    if cost < 1:
        raise ValueError(f"cost must be at least 1, got {cost}")
    try:
        budget, period = BUDGETS[source]
    except KeyError:
        raise ValueError(f"no request budget defined for source {source!r}") from None

    start = period_start(period, today or datetime.now(UTC).date())

    # Create the row, and follow a changed budget, without touching `used`.
    session.execute(
        pg_insert(ApiQuota)
        .values(source=source, period=start, used=0, budget=budget)
        .on_conflict_do_update(
            index_elements=["source", "period"], set_={"budget": budget}
        )
    )

    limit_pct = HARD_LIMIT_PCT if kind is CallKind.CLOSE else SOFT_LIMIT_PCT
    # One statement checks and increments, so two processes cannot both pass
    # the check against the same remaining allowance.
    used = session.execute(
        text(
            "UPDATE api_quota SET used = used + :cost, updated_at = now() "
            "WHERE source = :source AND period = :period "
            "AND (used + :cost) * 100 <= budget * :limit_pct "
            "RETURNING used"
        ),
        {"cost": cost, "source": source, "period": start, "limit_pct": limit_pct},
    ).scalar_one_or_none()

    if used is not None:
        return int(used)

    # populate_existing: the UPDATE above bypassed the identity map.
    row = session.execute(
        select(ApiQuota)
        .where(ApiQuota.source == source, ApiQuota.period == start)
        .execution_options(populate_existing=True)
    ).scalar_one()
    log.error(
        "quota.refused",
        source=source,
        kind=kind.value,
        used=row.used,
        budget=row.budget,
        limit_pct=limit_pct,
    )
    raise QuotaExceeded(
        f"{source}: {row.used}/{row.budget} used, "
        f"{kind.value} calls stop at {limit_pct}% of the budget"
    )
