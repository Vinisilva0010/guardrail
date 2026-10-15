"""Tests for the chart payload.

The series is a sawtooth on purpose: a monotonic ramp has no turns, so it
would produce no swings and no levels, and the level part of the payload would
be tested as "empty" forever. The symbol and venue are private to this file,
because a test that writes under a real symbol pollutes real data.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from guardrail.db.models import AssetClass, Candle, Instrument
from guardrail.web import chart_data

SYMBOL = "ZZCHART"
VENUE = "test-chart"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
COUNT = 60


def _instrument(session: Session) -> Instrument:
    instrument = Instrument(symbol=SYMBOL, venue=VENUE, asset_class=AssetClass.CRYPTO)
    session.add(instrument)
    session.flush()
    return instrument


def _candles(
    session: Session,
    instrument: Instrument,
    timeframe: str = "1d",
    count: int = COUNT,
) -> None:
    """A sawtooth with a period of ten bars.

    Price climbs from 100 to 118 and drops back, so the highs repeat at exactly
    120 and the lows at 98: several turns at the same price, which is what a
    liquidity level is made of.
    """
    for i in range(count):
        mid = Decimal(100 + (i % 10) * 2)
        session.add(
            Candle(
                instrument_id=instrument.id,
                timeframe=timeframe,
                ts=BASE + timedelta(days=i),
                open=mid,
                high=mid + 2,
                low=mid - 2,
                close=mid + 1,
                volume=Decimal(1000 + i),
            )
        )
    session.flush()


# --- loading --------------------------------------------------------------


def test_bars_come_back_oldest_first(session: Session) -> None:
    instrument = _instrument(session)
    _candles(session, instrument)
    _, bars = chart_data.load_bars(session, SYMBOL, "1d")
    assert len(bars) == COUNT
    assert bars[0].ts < bars[-1].ts
    assert bars[0].ts == BASE


def test_the_limit_keeps_the_most_recent_bars(session: Session) -> None:
    """A limit has to cut the old end, not the new one: a chart missing today
    is useless, a chart missing last year is just shorter."""
    instrument = _instrument(session)
    _candles(session, instrument)
    _, bars = chart_data.load_bars(session, SYMBOL, "1d", limit=5)
    assert len(bars) == 5
    assert bars[-1].ts == BASE + timedelta(days=COUNT - 1)


def test_only_the_requested_timeframe_is_returned(session: Session) -> None:
    instrument = _instrument(session)
    _candles(session, instrument, "1d", COUNT)
    _candles(session, instrument, "1h", 5)
    _, hourly = chart_data.load_bars(session, SYMBOL, "1h")
    assert len(hourly) == 5


def test_an_unknown_symbol_is_refused(session: Session) -> None:
    with pytest.raises(chart_data.UnknownSymbol):
        chart_data.load_bars(session, "ZZNOSUCH", "1d")


def test_a_symbol_without_candles_for_that_timeframe_is_refused(
    session: Session,
) -> None:
    """The instrument exists but the series does not. Saying so is the point:
    an empty chart with no message reads as a flat market."""
    instrument = _instrument(session)
    _candles(session, instrument, "1d", 3)
    with pytest.raises(chart_data.UnknownSymbol):
        chart_data.load_bars(session, SYMBOL, "4h")


def test_a_bad_timeframe_or_symbol_is_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="timeframe"):
        chart_data.load_bars(session, SYMBOL, "5m")
    with pytest.raises(ValueError, match="symbol"):
        chart_data.load_bars(session, "DROP TABLE", "1d")


# --- payload --------------------------------------------------------------


def test_payload_carries_candles_indicators_levels_and_reach(
    session: Session,
) -> None:
    """Warm-up is dropped rather than sent as nulls, so each series is exactly
    as long as the arithmetic allows: 60 bars give 11 points of a 50-period
    average, 40 of a 21-period one, and 46 of a 14-period RSI.
    """
    instrument = _instrument(session)
    _candles(session, instrument)
    now = BASE + timedelta(days=COUNT - 1, hours=5)
    payload = chart_data.build(session, SYMBOL.lower(), "1d", now)

    assert payload["meta"]["symbol"] == SYMBOL
    assert payload["meta"]["venue"] == VENUE
    assert payload["meta"]["bars"] == COUNT
    assert payload["meta"]["age_hours"] == 5.0
    assert len(payload["candles"]) == COUNT
    assert len(payload["ema_slow"]["points"]) == COUNT - chart_data.EMA_SLOW + 1
    assert len(payload["ema_fast"]["points"]) == COUNT - chart_data.EMA_FAST + 1
    assert len(payload["rsi"]["points"]) == COUNT - 14
    assert payload["rsi"]["period"] == 14


def test_payload_finds_the_repeated_turns_as_levels(session: Session) -> None:
    """The sawtooth turns at the same two prices over and over, so there is a
    resistance band and a support band, each touched more than once."""
    instrument = _instrument(session)
    _candles(session, instrument)
    payload = chart_data.build(session, SYMBOL, "1d", BASE)
    kinds = {level["kind"] for level in payload["levels"]}
    assert kinds == {"resistance", "support"}
    assert all(level["touches"] >= 2 for level in payload["levels"])
    assert len(payload["levels"]) <= chart_data.MAX_LEVELS


def test_payload_reach_covers_every_horizon(session: Session) -> None:
    """Each band is centred on the last close and widens with the horizon."""
    instrument = _instrument(session)
    _candles(session, instrument)
    payload = chart_data.build(session, SYMBOL, "1d", BASE)
    assert [band["horizon_days"] for band in payload["reach"]] == list(
        chart_data.HORIZONS
    )
    for band in payload["reach"]:
        assert band["far_low"] < band["near_low"] < band["base"]
        assert band["base"] < band["near_high"] < band["far_high"]


def test_payload_volume_profile_sits_inside_the_traded_range(
    session: Session,
) -> None:
    instrument = _instrument(session)
    _candles(session, instrument)
    profile = chart_data.build(session, SYMBOL, "1d", BASE)["profile"]
    assert profile is not None
    assert 98.0 <= profile["value_area_low"] <= profile["value_area_high"] <= 120.0
    assert 98.0 <= profile["point_of_control"] <= 120.0
