"""Corporate action collection from Alpaca.

Events are read from the venue, never inferred from price. A 120-to-1 reverse
split is indistinguishable from an 18x breakout in bar data, and a failed trial
is indistinguishable from a spin-off: any threshold that separates one pair
misclassifies the other. These events are published, so they are fetched.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import AlpacaClient, latest_available_day
from guardrail.collectors.equity_backfill import load_universe
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import CorporateAction, CorporateActionType
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

SOURCE: Final = "alpaca.corporate_actions"
DEFAULT_DAYS: Final = 365 * 3

# Symbols per request.
BATCH_SIZE: Final = 100

# Response keys that change the price series. cash_dividends are excluded: the
# bars are split-adjusted but not dividend-adjusted, and a dividend moves price
# by a fraction of a percent, far below anything the setup reacts to.
# name_changes are excluded too: the instrument continues, only its label moves.
RELEVANT: Final = {
    "reverse_splits": CorporateActionType.REVERSE_SPLIT,
    "forward_splits": CorporateActionType.FORWARD_SPLIT,
    "unit_splits": CorporateActionType.UNIT_SPLIT,
    "spin_offs": CorporateActionType.SPIN_OFF,
    "stock_mergers": CorporateActionType.STOCK_MERGER,
}


@dataclass(frozen=True)
class ParsedAction:
    """One corporate action, normalised across response shapes."""

    symbol: str
    effective_on: date
    action_type: CorporateActionType
    ratio: Decimal | None
    detail: str


def _as_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _effective_date(item: dict[str, Any]) -> date | None:
    """Pick the date the price series actually changes.

    The field differs by event type, so each candidate is tried in the order the
    venue uses it.
    """
    for key in ("ex_date", "effective_date", "process_date", "payable_date"):
        raw = item.get(key)
        if raw:
            try:
                return date.fromisoformat(str(raw))
            except ValueError:
                continue
    return None


def _ratio(action_type: CorporateActionType, item: dict[str, Any]) -> Decimal | None:
    """New shares per old share, where the event defines a single factor.

    Splits carry old_rate and new_rate; the factor is new/old. Mergers carry the
    acquirer rate. Spin-offs have no single factor: the holder keeps the original
    position and receives a new one, so the price drop has no conversion ratio.
    """
    if action_type is CorporateActionType.SPIN_OFF:
        return None
    if action_type is CorporateActionType.STOCK_MERGER:
        return _as_decimal(item.get("acquirer_rate"))

    old = _as_decimal(item.get("old_rate"))
    new = _as_decimal(item.get("new_rate"))
    if old is None or new is None or old == 0:
        return None
    return new / old


def parse_actions(payload: dict[str, Any]) -> list[ParsedAction]:
    """Convert a corporate-actions response into storable rows."""
    actions = payload.get("corporate_actions")
    if not isinstance(actions, dict):
        raise UpstreamDataError(f"unexpected payload shape: {type(actions)}")

    out: list[ParsedAction] = []
    for key, action_type in RELEVANT.items():
        for item in actions.get(key) or []:
            symbol = (
                item.get("symbol")
                or item.get("source_symbol")
                or item.get("acquiree_symbol")
                or item.get("old_symbol")
            )
            effective = _effective_date(item)
            if not symbol or effective is None:
                continue
            out.append(
                ParsedAction(
                    symbol=str(symbol),
                    effective_on=effective,
                    action_type=action_type,
                    ratio=_ratio(action_type, item),
                    detail=key,
                )
            )
    return out


def store_actions(
    session: Session, actions: list[ParsedAction], universe: dict[str, int]
) -> int:
    """Insert actions for symbols in the universe, skipping duplicates."""
    rows = [
        {
            "instrument_id": universe[a.symbol],
            "effective_on": datetime.combine(a.effective_on, datetime.min.time(), UTC),
            "action_type": a.action_type,
            "ratio": a.ratio,
            "detail": a.detail,
        }
        for a in actions
        if a.symbol in universe
    ]
    if not rows:
        return 0

    statement = (
        pg_insert(CorporateAction)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["instrument_id", "effective_on", "action_type"]
        )
        .returning(CorporateAction.id)
    )
    return len(session.execute(statement).fetchall())


async def fetch_actions(
    client: AlpacaClient, symbols: list[str], start: date, end: date
) -> list[ParsedAction]:
    """Fetch corporate actions for a list of symbols."""
    out: list[ParsedAction] = []
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        payload = await client.fetch_corporate_actions(batch, start, end)
        out.extend(parse_actions(payload))
        await asyncio.sleep(0.1)
    return out


async def run_corporate_actions(days: int) -> tuple[int, int]:
    """Collect corporate actions for the universe. Returns (found, stored)."""
    start = date.today() - timedelta(days=days)
    end = latest_available_day()

    with session_scope() as session:
        universe = load_universe(session)

    if not universe:
        log.warning("corporate_actions.empty_universe")
        return 0, 0

    try:
        async with httpx.AsyncClient(timeout=120.0) as http:
            client = AlpacaClient(http)
            actions = await fetch_actions(client, sorted(universe), start, end)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, SOURCE, f"{type(exc).__name__}: {exc}")
        raise

    with session_scope() as session:
        stored = store_actions(session, actions, universe)
        record_success(session, SOURCE)

    log.info("corporate_actions.collected", found=len(actions), stored=stored)
    return len(actions), stored


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Collect corporate actions.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    try:
        found, stored = asyncio.run(run_corporate_actions(args.days))
    finally:
        dispose_engine()

    print()
    print(f"actions found:  {found}")
    print(f"actions stored: {stored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
