"""Schema-level guarantees.

These tests assert that the database itself rejects bad data. Validation in
Python can be bypassed by a direct SQL write or a future code path; a constraint
cannot.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from guardrail.db.models import AssetClass, Candle, Catalyst, Instrument

TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _instrument(session: Session, symbol: str = "BTCUSDT") -> Instrument:
    instrument = Instrument(
        symbol=symbol, venue="binance", asset_class=AssetClass.CRYPTO
    )
    session.add(instrument)
    session.flush()
    return instrument


def test_enum_is_stored_as_lowercase_value(session: Session) -> None:
    """The database must hold 'crypto', not the member name 'CRYPTO'.

    Hand-written SQL filtering on 'crypto' would silently return nothing if the
    member name were persisted instead.
    """
    instrument = _instrument(session)
    stored = session.execute(
        text("SELECT asset_class::text FROM instrument WHERE id = :id"),
        {"id": instrument.id},
    ).scalar_one()
    assert stored == "crypto"


def test_enum_rejects_uppercase_literal(session: Session) -> None:
    """A direct insert with the member name must fail, not be coerced."""
    with pytest.raises((DataError, IntegrityError)):
        session.execute(
            text(
                "INSERT INTO instrument (symbol, venue, asset_class) "
                "VALUES ('ETHUSDT', 'binance', 'CRYPTO')"
            )
        )


def test_duplicate_candle_is_rejected(session: Session) -> None:
    """The composite primary key must make a re-run idempotent at the DB level."""
    instrument = _instrument(session)
    values = {
        "instrument_id": instrument.id,
        "timeframe": "1h",
        "ts": TS,
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": Decimal("105"),
        "volume": Decimal("1000"),
    }
    session.add(Candle(**values))
    session.flush()

    session.add(Candle(**values))
    with pytest.raises(IntegrityError):
        session.flush()


def test_candle_with_high_below_low_is_rejected(session: Session) -> None:
    """A malformed bar from an upstream API must not reach storage."""
    instrument = _instrument(session)
    session.add(
        Candle(
            instrument_id=instrument.id,
            timeframe="1h",
            ts=TS,
            open=Decimal("100"),
            high=Decimal("80"),
            low=Decimal("90"),
            close=Decimal("85"),
            volume=Decimal("10"),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_catalyst_is_rejected(session: Session) -> None:
    """News APIs repeat items across polls; the dedupe key must drop repeats."""
    instrument = _instrument(session)
    values = {
        "instrument_id": instrument.id,
        "ts": TS,
        "source": "finnhub",
        "headline": "Example headline",
        "dedupe_key": "abc123",
    }
    session.add(Catalyst(**values))
    session.flush()

    session.add(Catalyst(**values))
    with pytest.raises(IntegrityError):
        session.flush()


def test_price_round_trips_as_exact_decimal(session: Session) -> None:
    """Money must survive storage without binary floating point error.

    0.1 + 0.2 is the canonical float failure. Numeric/Decimal must not reproduce
    it, because position sizing multiplies this error.
    """
    instrument = _instrument(session)
    price = Decimal("0.000000012345")
    session.add(
        Candle(
            instrument_id=instrument.id,
            timeframe="1h",
            ts=TS,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("0.1"),
        )
    )
    session.flush()
    session.expire_all()

    stored = session.get(Candle, (instrument.id, "1h", TS))
    assert stored is not None
    assert isinstance(stored.close, Decimal)
    assert stored.close == price
