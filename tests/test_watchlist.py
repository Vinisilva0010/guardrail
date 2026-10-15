"""Tests for the watchlist commands."""

import pytest
from sqlalchemy.orm import Session

from guardrail.db.models import AssetClass, Instrument
from guardrail.watchlist import add, listing, remove, resolve
from tests.constants import TEST_SYMBOL, TEST_SYMBOL_ALT, TEST_VENUE


def _instrument(session: Session, symbol: str, venue: str = TEST_VENUE) -> Instrument:
    instrument = Instrument(symbol=symbol, venue=venue, asset_class=AssetClass.EQUITY)
    session.add(instrument)
    session.flush()
    return instrument


def test_symbol_resolves_case_insensitively(session: Session) -> None:
    instrument = _instrument(session, TEST_SYMBOL)

    assert resolve(session, TEST_SYMBOL.lower()).id == instrument.id


def test_unknown_symbol_is_an_error_not_a_guess(session: Session) -> None:
    with pytest.raises(ValueError, match="no instrument named"):
        resolve(session, "NOSUCHTHING")


def test_a_symbol_on_two_venues_must_name_the_venue(session: Session) -> None:
    _instrument(session, TEST_SYMBOL, "venue-a")
    b = _instrument(session, TEST_SYMBOL, "venue-b")

    with pytest.raises(ValueError, match="several venues"):
        resolve(session, TEST_SYMBOL)
    assert resolve(session, f"venue-b:{TEST_SYMBOL}").id == b.id


def test_add_is_idempotent_and_reports_only_new_entries(session: Session) -> None:
    _instrument(session, TEST_SYMBOL)

    first = add(session, [TEST_SYMBOL])
    second = add(session, [TEST_SYMBOL])

    assert first == [f"{TEST_VENUE}:{TEST_SYMBOL}"] and second == []


def test_remove_and_list(session: Session) -> None:
    _instrument(session, TEST_SYMBOL)
    _instrument(session, TEST_SYMBOL_ALT)
    add(session, [TEST_SYMBOL, TEST_SYMBOL_ALT])
    before = [line for line in listing(session) if line.startswith(f"{TEST_VENUE}:")]

    removed = remove(session, [TEST_SYMBOL])
    again = remove(session, [TEST_SYMBOL])
    after = [line for line in listing(session) if line.startswith(f"{TEST_VENUE}:")]

    assert before == sorted(before) and len(before) == 2
    assert removed == [f"{TEST_VENUE}:{TEST_SYMBOL}"] and again == []
    assert after == [f"{TEST_VENUE}:{TEST_SYMBOL_ALT}"]
