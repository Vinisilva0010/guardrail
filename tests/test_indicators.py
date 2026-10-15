"""Fixed-value tests for the chart indicators.

Every expected number here was worked out by hand and the arithmetic is written
in the docstring, so the test checks the implementation instead of recording
whatever it happened to return. Periods are deliberately small for that reason.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from guardrail.web import indicators

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(i: int, high: str, low: str, close: str, volume: str = "1") -> indicators.Bar:
    """A bar for indicator maths. `open` is set to the low; no indicator here
    reads it, and the database constraints that care are tested elsewhere."""
    return indicators.Bar(
        ts=BASE + timedelta(days=i),
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )


# --- moving average -------------------------------------------------------


def test_ema_matches_hand_arithmetic() -> None:
    """values 1..5, period 3.

    seed = mean(1, 2, 3) = 2            -> index 2
    weight = 2 / (3 + 1) = 0.5
    index 3 = (4 - 2) * 0.5 + 2 = 3
    index 4 = (5 - 3) * 0.5 + 3 = 4
    """
    values = [Decimal(n) for n in (1, 2, 3, 4, 5)]
    assert indicators.ema(values, 3) == [
        None,
        None,
        Decimal(2),
        Decimal(3),
        Decimal(4),
    ]


def test_ema_is_all_none_when_the_series_is_shorter_than_the_period() -> None:
    assert indicators.ema([Decimal(1), Decimal(2)], 3) == [None, None]


def test_ema_rejects_a_period_below_one() -> None:
    with pytest.raises(ValueError, match="period"):
        indicators.ema([Decimal(1)], 0)


# --- rsi ------------------------------------------------------------------


def test_rsi_matches_hand_arithmetic() -> None:
    """closes 10, 11, 12, 11, 10 with period 2. Changes: +1, +1, -1, -1.

    index 2: gains 2, losses 0 -> avg 1 and 0 -> no loss at all -> 100
    index 3: avg_gain = (1 * 1 + 0) / 2 = 0.5
             avg_loss = (0 * 1 + 1) / 2 = 0.5   -> rs 1    -> 100 - 100/2 = 50
    index 4: avg_gain = (0.5 * 1 + 0) / 2 = 0.25
             avg_loss = (0.5 * 1 + 1) / 2 = 0.75 -> rs 1/3 -> 100 - 75 = 25
    """
    closes = [Decimal(n) for n in (10, 11, 12, 11, 10)]
    assert indicators.rsi(closes, 2) == [
        None,
        None,
        Decimal(100),
        Decimal(50),
        Decimal(25),
    ]


def test_rsi_is_one_hundred_while_nothing_has_fallen() -> None:
    """A window with no losses has no ratio to take; 100 is the convention."""
    closes = [Decimal(n) for n in (1, 2, 3, 4)]
    assert indicators.rsi(closes, 2)[-1] == Decimal(100)


def test_rsi_needs_more_closes_than_the_period() -> None:
    assert indicators.rsi([Decimal(1), Decimal(2)], 2) == [None, None]


# --- true range and atr ---------------------------------------------------


def test_true_range_uses_the_gap_from_the_previous_close() -> None:
    """high 11, low 9, previous close 14: the gap down is 5, wider than the
    bar's own range of 2, so the true range is 5."""
    bar = _bar(1, high="11", low="9", close="10")
    assert indicators.true_range(bar, Decimal(14)) == Decimal(5)
    assert indicators.true_range(bar, None) == Decimal(2)


def test_atr_matches_hand_arithmetic() -> None:
    """Four bars, period 2.

    TR0 = 10 - 8 = 2                       (no previous close)
    TR1 = max(11-9, |11-9|, |9-9|)   = 2
    TR2 = max(12-11, |12-10|, |11-10|) = 2
    TR3 = max(13-12, |13-12|, |12-12|) = 1

    index 1 = mean(2, 2) = 2
    index 2 = (2 * 1 + 2) / 2 = 2
    index 3 = (2 * 1 + 1) / 2 = 1.5
    """
    bars = [
        _bar(0, high="10", low="8", close="9"),
        _bar(1, high="11", low="9", close="10"),
        _bar(2, high="12", low="11", close="12"),
        _bar(3, high="13", low="12", close="13"),
    ]
    assert indicators.atr(bars, 2) == [
        None,
        Decimal(2),
        Decimal(2),
        Decimal("1.5"),
    ]


# --- swings and levels ----------------------------------------------------


def test_swings_finds_the_turns_and_ignores_the_unconfirmed_edges() -> None:
    """Four bars, width 1, so only indices 1 and 2 can qualify.

    index 1 high 12 beats its neighbours 10 and 11 -> swing high
    index 2 low 8 is under its neighbours 11 and 9 -> swing low
    """
    bars = [
        _bar(0, high="10", low="9", close="10"),
        _bar(1, high="12", low="11", close="11"),
        _bar(2, high="11", low="8", close="9"),
        _bar(3, high="10", low="9", close="10"),
    ]
    found = indicators.swings(bars, width=1)
    assert [(s.bar_index, s.price, s.kind) for s in found] == [
        (1, Decimal(12), "high"),
        (2, Decimal(8), "low"),
    ]


def test_a_plateau_of_equal_highs_is_not_counted_twice() -> None:
    """Two bars at the same high are one turn, not two: counting both would
    double the touches of the level built from them."""
    bars = [
        _bar(0, high="10", low="9", close="10"),
        _bar(1, high="12", low="11", close="11"),
        _bar(2, high="12", low="11", close="11"),
        _bar(3, high="10", low="9", close="10"),
    ]
    assert [s.kind for s in indicators.swings(bars, width=1)] == []


def test_liquidity_levels_group_nearby_swings_and_drop_lonely_ones() -> None:
    """Highs at 100 and 100.2 are 0.2% apart, inside the 0.5% tolerance, so
    they are one level with two touches, centred on 100.1. The high at 105 is
    4.9% away and stands alone, so it is dropped. The two lows at 90 and 90.1
    make a support level centred on 90.05.
    """
    found = [
        indicators.Swing(1, BASE, Decimal("100"), "high"),
        indicators.Swing(5, BASE, Decimal("100.2"), "high"),
        indicators.Swing(9, BASE, Decimal("105"), "high"),
        indicators.Swing(3, BASE, Decimal("90"), "low"),
        indicators.Swing(7, BASE, Decimal("90.1"), "low"),
    ]
    levels = indicators.liquidity_levels(found)
    assert [(lv.price, lv.touches, lv.kind) for lv in levels] == [
        (Decimal("100.1"), 2, "resistance"),
        (Decimal("90.05"), 2, "support"),
    ]


def test_liquidity_levels_rejects_a_tolerance_of_zero() -> None:
    with pytest.raises(ValueError, match="tolerance"):
        indicators.liquidity_levels([], tolerance=Decimal(0))


# --- volume profile -------------------------------------------------------


def test_volume_profile_matches_hand_arithmetic() -> None:
    """Two bars, four bins.

    Range 8 to 12, so each bin is 1 wide: 8-9, 9-10, 10-11, 11-12.
    Bar A (8 to 8.5, volume 10) covers bin 0 only          -> bin 0 gets 10.
    Bar B (8 to 12, volume 90) covers all four bins evenly -> each gets 22.5.
    Buckets: 32.5, 22.5, 22.5, 22.5. Total 100, so the value area needs 70.
    Busiest bin is 0 (32.5). Growing upward: +22.5 = 55, +22.5 = 77.5, done.
    Value area is bins 0 to 2, which is price 8 to 11.
    The point of control is the centre of bin 0, which is 8.5.
    """
    bars = [
        _bar(0, high="8.5", low="8", close="8.2", volume="10"),
        _bar(1, high="12", low="8", close="11", volume="90"),
    ]
    profile = indicators.volume_profile(bars, bin_count=4)
    assert profile is not None
    assert profile.point_of_control == Decimal("8.5")
    assert profile.value_area_low == Decimal(8)
    assert profile.value_area_high == Decimal(11)
    assert [volume for _, volume in profile.bins] == [
        Decimal("32.5"),
        Decimal("22.5"),
        Decimal("22.5"),
        Decimal("22.5"),
    ]


def test_volume_profile_is_none_without_a_range_or_without_volume() -> None:
    """No profile is the honest answer; a profile of zeros is not."""
    flat = [_bar(0, high="10", low="10", close="10", volume="5")]
    empty = [_bar(0, high="12", low="8", close="10", volume="0")]
    assert indicators.volume_profile(flat) is None
    assert indicators.volume_profile(empty) is None
    assert indicators.volume_profile([]) is None


# --- projection -----------------------------------------------------------


def test_projection_matches_hand_arithmetic() -> None:
    """base 100, average true range 2, horizon 4 days.

    reach = 2 * sqrt(4) = 4
    near  = 100 -/+ 4  -> 96 and 104
    far   = 100 -/+ 8  -> 92 and 108
    """
    out = indicators.projection(Decimal(100), Decimal(2), 4)
    assert (out.near_low, out.near_high) == (Decimal(96), Decimal(104))
    assert (out.far_low, out.far_high) == (Decimal(92), Decimal(108))


def test_projection_rejects_nonsense_arguments() -> None:
    with pytest.raises(ValueError, match="horizon_days"):
        indicators.projection(Decimal(100), Decimal(2), 0)
    with pytest.raises(ValueError, match="atr_value"):
        indicators.projection(Decimal(100), Decimal(-1), 1)
