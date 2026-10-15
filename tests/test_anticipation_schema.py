"""Schema guarantees of the anticipation layer.

Raw SQL is used where the point is that the database refuses a value: going
through the ORM would let the Python enum reject it first and prove nothing
about the constraint.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from guardrail.db.models import (
    AssetClass,
    EventImpact,
    EventKind,
    EventScope,
    Expectation,
    Instrument,
    ScheduledEvent,
)
from tests.constants import TEST_SYMBOL, TEST_VENUE

KEY = "a" * 32
STARTS = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)

INSERT_EVENT = text(
    """
    INSERT INTO scheduled_event
        (source, kind, scope, instrument_id, title, starts_at, has_time,
         impact, payload, identity_key)
    VALUES
        ('forexfactory', :kind, 'us', :instrument_id, 'FOMC', :starts_at, true,
         :impact, '{}'::jsonb, :key)
    """
)


def _params(**override: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "rate_decision",
        "instrument_id": None,
        "starts_at": STARTS,
        "impact": "high",
        "key": KEY,
    }
    return {**base, **override}


def _instrument(session: Session) -> Instrument:
    instrument = Instrument(
        symbol=TEST_SYMBOL, venue=TEST_VENUE, asset_class=AssetClass.CRYPTO
    )
    session.add(instrument)
    session.flush()
    return instrument


def _event(**override: Any) -> ScheduledEvent:
    fields: dict[str, Any] = {
        "source": "forexfactory",
        "kind": EventKind.RATE_DECISION,
        "scope": EventScope.US,
        "title": "FOMC",
        "starts_at": STARTS,
        "has_time": True,
        "impact": EventImpact.HIGH,
        "payload": {"raw": True},
        "identity_key": KEY,
    }
    return ScheduledEvent(**{**fields, **override})


def test_event_round_trips_with_defaults(session: Session) -> None:
    event = _event(expected=Decimal("4.00"), previous=Decimal("4.25"))
    session.add(event)
    session.flush()
    session.refresh(event)

    assert event.date_is_estimated is False
    assert event.actual is None
    assert event.expected == Decimal("4.00000000")
    assert event.first_seen_at is not None
    assert event.payload == {"raw": True}


def test_enums_are_stored_as_lowercase_values(session: Session) -> None:
    session.add(_event())
    session.flush()

    row = session.execute(
        text(
            "SELECT kind::text, scope::text, impact::text FROM scheduled_event "
            "WHERE identity_key = :key"
        ),
        {"key": KEY},
    ).one()

    assert tuple(row) == ("rate_decision", "us", "high")


def test_identity_key_is_unique(session: Session) -> None:
    session.add(_event())
    session.flush()

    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_event(title="Same key, other title"))
        session.flush()


def test_same_event_on_another_date_is_still_one_row(session: Session) -> None:
    """The reason the date is not part of the key: a postponement must update."""
    session.add(_event())
    session.flush()

    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_event(starts_at=datetime(2026, 10, 29, 18, 0, tzinfo=UTC)))
        session.flush()


def test_unknown_impact_is_rejected(session: Session) -> None:
    with pytest.raises(DataError), session.begin_nested():
        session.execute(INSERT_EVENT, _params(impact="catastrophic"))


def test_unknown_kind_is_rejected(session: Session) -> None:
    with pytest.raises(DataError), session.begin_nested():
        session.execute(INSERT_EVENT, _params(kind="moon_landing"))


@pytest.mark.parametrize("key", ["", "short", "A" * 32, "g" * 32, "a" * 64])
def test_malformed_identity_key_is_rejected(session: Session, key: str) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(INSERT_EVENT, _params(key=key))


@pytest.mark.parametrize("kind", ["earnings", "token_unlock"])
def test_instrument_bound_kinds_require_an_instrument(
    session: Session, kind: str
) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(INSERT_EVENT, _params(kind=kind))


def test_earnings_with_an_instrument_is_accepted(session: Session) -> None:
    instrument = _instrument(session)

    session.execute(INSERT_EVENT, _params(kind="earnings", instrument_id=instrument.id))


def test_deleting_an_instrument_removes_its_events(session: Session) -> None:
    instrument = _instrument(session)
    session.add(_event(kind=EventKind.EARNINGS, instrument_id=instrument.id))
    session.flush()

    session.delete(instrument)
    session.flush()

    remaining = session.execute(
        select(func.count())
        .select_from(ScheduledEvent)
        .where(ScheduledEvent.instrument_id == instrument.id)
    ).scalar_one()
    assert remaining == 0


def test_expectation_rejects_negative_dispersion(session: Session) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Expectation(
                code="IPCA",
                horizon_year=2026,
                published_on=date(2026, 10, 9),
                stdev=Decimal("-0.1"),
            )
        )
        session.flush()


def test_expectation_rejects_absurd_horizon(session: Session) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Expectation(code="IPCA", horizon_year=26, published_on=date(2026, 10, 9))
        )
        session.flush()


STAT_INSERT = text(
    """
    INSERT INTO event_impact_stat
        (kind, instrument_id, horizon, n, p10, p90, pct_positive,
         window_start, window_end)
    VALUES
        ('rate_decision', :instrument_id, :horizon, :n, :p10, :p90, :pct,
         '2024-01-01', '2026-01-01')
    """
)


def _stat(instrument_id: int, **override: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instrument_id": instrument_id,
        "horizon": "d1",
        "n": 11,
        "p10": Decimal("-0.06"),
        "p90": Decimal("0.05"),
        "pct": Decimal("0.45"),
    }
    return {**base, **override}


def test_valid_stat_row_is_accepted(session: Session) -> None:
    session.execute(STAT_INSERT, _stat(_instrument(session).id))


@pytest.mark.parametrize(
    "override",
    [
        {"horizon": "d2"},
        {"n": -1},
        {"pct": Decimal("1.5")},
        {"pct": Decimal("-0.1")},
        {"p10": Decimal("0.2"), "p90": Decimal("-0.2")},
    ],
)
def test_invalid_stat_row_is_rejected(
    session: Session, override: dict[str, Any]
) -> None:
    instrument_id = _instrument(session).id

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(STAT_INSERT, _stat(instrument_id, **override))
