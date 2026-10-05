"""Tests for news collection.

The symbol cap carries the weight here. Measured over 30 days of the live feed,
62% of items name a single company and a handful name 38 at once. Without the
cap, every large cap would carry a catalyst every day and the setup\'s third
condition would stop filtering anything.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.news import (
    MAX_SYMBOLS_PER_ITEM,
    parse_news,
    store_news,
)
from guardrail.collectors.store import get_or_create_instrument
from guardrail.db.models import AssetClass, Catalyst
from tests.constants import TEST_SYMBOL, TEST_SYMBOL_ALT, TEST_VENUE


def _item(
    symbols: list[str], headline: str = "Headline", **extra: Any
) -> dict[str, Any]:
    return {
        "id": extra.pop("id", 1),
        "symbols": symbols,
        "headline": headline,
        "created_at": extra.pop("created_at", "2026-03-15T14:30:00Z"),
        "url": "https://example.com/story",
        **extra,
    }


def _payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"news": list(items)}


def test_single_symbol_item_is_kept() -> None:
    items = parse_news(_payload(_item(["AAA"])))

    assert len(items) == 1
    assert items[0].symbols == ("AAA",)
    assert items[0].published_at == datetime(2026, 3, 15, 14, 30, tzinfo=UTC)


def test_item_at_the_cap_is_kept() -> None:
    """Real company news often names a second party: a buyer and a target."""
    items = parse_news(_payload(_item(["AAA", "BBB", "CCC"])))

    assert len(items) == 1


def test_market_roundup_is_dropped() -> None:
    """A story tagged with dozens of symbols is not news about any of them."""
    many = [f"S{i}" for i in range(MAX_SYMBOLS_PER_ITEM + 1)]

    assert parse_news(_payload(_item(many))) == []


def test_item_without_symbols_is_dropped() -> None:
    assert parse_news(_payload(_item([]))) == []


def test_item_without_headline_is_dropped() -> None:
    assert parse_news(_payload(_item(["AAA"], headline="   "))) == []


def test_item_with_bad_timestamp_is_dropped() -> None:
    assert parse_news(_payload(_item(["AAA"], created_at="not-a-date"))) == []


def test_unexpected_payload_is_rejected() -> None:
    with pytest.raises(UpstreamDataError, match="expected a list of news"):
        parse_news({"news": {"oops": 1}})


def test_stores_one_row_per_symbol_in_universe(session: Session) -> None:
    a = get_or_create_instrument(session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY)
    b = get_or_create_instrument(
        session, TEST_SYMBOL_ALT, TEST_VENUE, AssetClass.EQUITY
    )
    session.flush()
    items = parse_news(_payload(_item([TEST_SYMBOL, TEST_SYMBOL_ALT, "OUTSIDE"])))

    stored = store_news(session, items, {TEST_SYMBOL: a.id, TEST_SYMBOL_ALT: b.id})

    assert stored == 2


def test_rerun_stores_nothing(session: Session) -> None:
    """A collector that runs on a schedule always re-reads recent stories."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )
    session.flush()
    items = parse_news(_payload(_item([TEST_SYMBOL])))
    universe = {TEST_SYMBOL: instrument.id}

    store_news(session, items, universe)
    second = store_news(session, items, universe)
    session.flush()

    assert second == 0
    total = session.execute(
        select(func.count())
        .select_from(Catalyst)
        .where(Catalyst.instrument_id == instrument.id)
    ).scalar_one()
    assert total == 1


def test_different_stories_about_one_symbol_both_store(session: Session) -> None:
    """Deduplication must key on the story, not just the symbol."""
    instrument = get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.EQUITY
    )
    session.flush()
    items = parse_news(
        _payload(
            _item([TEST_SYMBOL], headline="First story", id=1),
            _item([TEST_SYMBOL], headline="Second story", id=2),
        )
    )

    assert store_news(session, items, {TEST_SYMBOL: instrument.id}) == 2
