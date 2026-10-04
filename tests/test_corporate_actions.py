"""Tests for corporate action collection.

These events exist to keep a 120-to-1 reverse split from reaching the engine as
an 18x breakout. The hard part is not detecting the split; it is not catching the
real moves that look identical in bar data. A biotech dropping 82% on a failed
trial and a spin-off produce the same price gap, which is why this reads the
venue's published events instead of thresholding price and volume.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.collectors.corporate_actions import (
    parse_actions,
    store_actions,
)
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.store import get_or_create_instrument
from guardrail.db.models import AssetClass, CorporateAction, CorporateActionType
from tests.constants import TEST_SYMBOL, TEST_VENUE


def _payload(**groups: list[dict[str, Any]]) -> dict[str, Any]:
    return {"corporate_actions": groups}


def test_reverse_split_ratio_is_new_over_old() -> None:
    """A 12-to-1 reverse split converts one old share into 1/12 of a new one."""
    actions = parse_actions(
        _payload(
            reverse_splits=[
                {
                    "symbol": "AAA",
                    "ex_date": "2025-05-06",
                    "old_rate": 12,
                    "new_rate": 1,
                }
            ]
        )
    )

    assert len(actions) == 1
    assert actions[0].action_type is CorporateActionType.REVERSE_SPLIT
    assert actions[0].ratio == Decimal(1) / Decimal(12)


def test_unit_split_ratio_is_captured() -> None:
    """The real WOLF event: a factor of 0.008352, roughly 120-to-1."""
    actions = parse_actions(
        _payload(
            unit_splits=[
                {
                    "old_symbol": "WOLF",
                    "effective_date": "2025-09-29",
                    "old_rate": 1,
                    "new_rate": 0.008352,
                }
            ]
        )
    )

    assert actions[0].ratio == Decimal("0.008352")


def test_forward_split_ratio_is_above_one() -> None:
    actions = parse_actions(
        _payload(
            forward_splits=[
                {
                    "symbol": "AAA",
                    "ex_date": "2025-12-08",
                    "old_rate": 1,
                    "new_rate": 2,
                }
            ]
        )
    )

    assert actions[0].ratio == Decimal(2)


def test_spin_off_has_no_single_ratio() -> None:
    """The holder keeps the original position and receives a new one.

    There is no conversion factor, so inventing one would be wrong.
    """
    actions = parse_actions(
        _payload(
            spin_offs=[
                {
                    "source_symbol": "CTVA",
                    "ex_date": "2026-10-01",
                    "source_rate": 1,
                    "new_rate": 1,
                    "new_symbol": "VYLR",
                }
            ]
        )
    )

    assert actions[0].action_type is CorporateActionType.SPIN_OFF
    assert actions[0].ratio is None


def test_dividends_and_name_changes_are_ignored() -> None:
    """Neither breaks the price series in a way the setup reacts to."""
    actions = parse_actions(
        _payload(
            cash_dividends=[{"symbol": "AAA", "ex_date": "2025-01-01", "rate": 0.16}],
            name_changes=[
                {"old_symbol": "AAA", "new_symbol": "BBB", "process_date": "2025-01-01"}
            ],
        )
    )

    assert actions == []


def test_event_without_a_usable_date_is_skipped() -> None:
    actions = parse_actions(
        _payload(
            reverse_splits=[
                {"symbol": "AAA", "old_rate": 2, "new_rate": 1},
            ]
        )
    )

    assert actions == []


def test_unexpected_payload_shape_is_rejected() -> None:
    with pytest.raises(UpstreamDataError, match="unexpected payload shape"):
        parse_actions({"corporate_actions": []})


def test_store_skips_symbols_outside_the_universe(session: Session) -> None:
    """The venue returns events for acquirees that are not tradable here."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )
    session.flush()
    actions = parse_actions(
        _payload(
            reverse_splits=[
                {
                    "symbol": TEST_SYMBOL,
                    "ex_date": "2025-05-06",
                    "old_rate": 2,
                    "new_rate": 1,
                },
                {
                    "symbol": "NOTINUNIVERSE",
                    "ex_date": "2025-05-06",
                    "old_rate": 2,
                    "new_rate": 1,
                },
            ]
        )
    )

    stored = store_actions(session, actions, {TEST_SYMBOL: instrument.id})

    assert stored == 1


def test_rerun_does_not_duplicate(session: Session) -> None:
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )
    session.flush()
    actions = parse_actions(
        _payload(
            reverse_splits=[
                {
                    "symbol": TEST_SYMBOL,
                    "ex_date": "2025-05-06",
                    "old_rate": 2,
                    "new_rate": 1,
                },
            ]
        )
    )
    universe = {TEST_SYMBOL: instrument.id}

    store_actions(session, actions, universe)
    second = store_actions(session, actions, universe)
    session.flush()

    assert second == 0
    rows = (
        session.execute(
            select(CorporateAction).where(
                CorporateAction.instrument_id == instrument.id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].effective_on.date() == date(2025, 5, 6)
