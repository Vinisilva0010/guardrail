"""Chart indicators and structural levels.

Pure functions over a candle series: no database, no network, no clock. Each
one takes the series and returns numbers, so each one can be checked against
values worked out by hand in a test.

Prices stay Decimal, as everywhere else in this project. These are display
values rather than position sizing, but a float price series mixed into a
Decimal one is how rounding drift gets in unnoticed.

What these functions are NOT: a forecast. `projection` reports the distance
price has recently been covering, and the level helpers report where price has
already turned. Neither says which way the next move goes.
"""

import math
from datetime import datetime
from decimal import Decimal
from typing import Final, NamedTuple

PRICE_QUANT: Final = Decimal("0.00000001")
UNIT_QUANT: Final = Decimal("0.0001")

RSI_PERIOD: Final = 14
ATR_PERIOD: Final = 14
SWING_WIDTH: Final = 3
# Two swings within this distance of each other count as the same level. Half a
# percent is wide enough to join wicks that were aiming at the same price and
# narrow enough not to merge separate levels on a liquid major.
LEVEL_TOLERANCE: Final = Decimal("0.005")
MIN_TOUCHES: Final = 2
PROFILE_BINS: Final = 24
# Share of traded volume that defines the value area, the band where most of
# the business was done. 70% is the conventional figure.
VALUE_AREA_SHARE: Final = Decimal("0.70")


class Bar(NamedTuple):
    """One candle, in the shape the indicators need."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class Swing(NamedTuple):
    """A bar that is a local extreme: price turned there."""

    bar_index: int
    ts: datetime
    price: Decimal
    kind: str  # "high" or "low"


class Level(NamedTuple):
    """A price band several swings agree on.

    `touches` is how many times price turned inside the band. More touches mean
    more resting orders: this is the pool a sweep goes after.
    """

    price: Decimal
    low: Decimal
    high: Decimal
    touches: int
    kind: str  # "resistance" or "support"
    last_ts: datetime


class VolumeProfile(NamedTuple):
    """Where the volume traded, by price.

    An approximation, and it has to be labelled as one: real volume profile
    needs trade-by-trade data. This spreads each bar's volume evenly across the
    price bins its range covers, which is the standard approach when only OHLCV
    is available.
    """

    point_of_control: Decimal
    value_area_low: Decimal
    value_area_high: Decimal
    bins: list[tuple[Decimal, Decimal]]


class Projection(NamedTuple):
    """How far price has been travelling, carried forward.

    `near` is one average true range scaled to the horizon; `far` is two. ATR
    measures range, not standard deviation, so these are NOT confidence
    intervals and must never be labelled as a probability on screen. They answer
    one question: if the next days look like the recent ones, how much ground
    can price cover.
    """

    horizon_days: int
    base: Decimal
    near_low: Decimal
    near_high: Decimal
    far_low: Decimal
    far_high: Decimal


def _q(value: Decimal, quant: Decimal = PRICE_QUANT) -> Decimal:
    return value.quantize(quant)


def ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    """Exponential moving average, aligned to the input.

    The first `period - 1` entries are None because no average exists yet. The
    seed is the simple mean of the first `period` values, so the series does not
    depend on how far back the data happens to start.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    weight = Decimal(2) / Decimal(period + 1)
    average = sum(values[:period], Decimal(0)) / Decimal(period)
    out[period - 1] = _q(average)
    for i in range(period, len(values)):
        average = (values[i] - average) * weight + average
        out[i] = _q(average)
    return out


def rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> list[Decimal | None]:
    """Relative strength index, with Wilder's smoothing.

    The first value sits at index `period`: it needs that many changes. A window
    with no losses gives 100 by definition, which is the convention and avoids a
    division by zero.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    out: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = Decimal(0)
    losses = Decimal(0)
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else Decimal(0)
        loss = -change if change < 0 else Decimal(0)
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return _q(Decimal(100) - Decimal(100) / (Decimal(1) + rs), UNIT_QUANT)


def true_range(bar: Bar, previous_close: Decimal | None) -> Decimal:
    """The bar's range, including any gap from the previous close."""
    if previous_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def atr(bars: list[Bar], period: int = ATR_PERIOD) -> list[Decimal | None]:
    """Average true range, with Wilder's smoothing."""
    if period < 1:
        raise ValueError("period must be at least 1")
    out: list[Decimal | None] = [None] * len(bars)
    if len(bars) < period:
        return out
    ranges = [
        true_range(bar, bars[i - 1].close if i else None) for i, bar in enumerate(bars)
    ]
    average = sum(ranges[:period], Decimal(0)) / Decimal(period)
    out[period - 1] = _q(average)
    for i in range(period, len(bars)):
        average = (average * Decimal(period - 1) + ranges[i]) / Decimal(period)
        out[i] = _q(average)
    return out


def swings(bars: list[Bar], width: int = SWING_WIDTH) -> list[Swing]:
    """Bars where price turned.

    A swing high is strictly higher than every high `width` bars to each side,
    and a swing low strictly lower than every low. Strict on both sides on
    purpose: a plateau of equal highs would otherwise report several swings for
    one turn, inflating the touch count of the level built from them.

    The last `width` bars can never qualify, because the turn is not confirmed
    until price has moved away from it.
    """
    if width < 1:
        raise ValueError("width must be at least 1")
    found: list[Swing] = []
    for i in range(width, len(bars) - width):
        window = range(i - width, i + width + 1)
        high = bars[i].high
        low = bars[i].low
        if all(bars[j].high < high for j in window if j != i):
            found.append(Swing(i, bars[i].ts, high, "high"))
        if all(bars[j].low > low for j in window if j != i):
            found.append(Swing(i, bars[i].ts, low, "low"))
    return found


def liquidity_levels(
    found: list[Swing],
    tolerance: Decimal = LEVEL_TOLERANCE,
    min_touches: int = MIN_TOUCHES,
) -> list[Level]:
    """Price bands that several swings of the same kind agree on.

    Swings are grouped per kind, in price order, joining the next one while it
    sits within `tolerance` of the group's running centre. Only groups with at
    least `min_touches` are returned: a single turn is not a level.
    """
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    levels: list[Level] = []
    for kind, label in (("high", "resistance"), ("low", "support")):
        same = sorted((s for s in found if s.kind == kind), key=lambda s: s.price)
        group: list[Swing] = []
        for swing in same:
            if group:
                centre = sum((s.price for s in group), Decimal(0)) / Decimal(len(group))
                if abs(swing.price - centre) > centre * tolerance:
                    levels.extend(_level_from(group, label, min_touches))
                    group = []
            group.append(swing)
        levels.extend(_level_from(group, label, min_touches))
    levels.sort(key=lambda level: level.price, reverse=True)
    return levels


def _level_from(group: list[Swing], label: str, min_touches: int) -> list[Level]:
    if len(group) < min_touches:
        return []
    prices = [s.price for s in group]
    centre = sum(prices, Decimal(0)) / Decimal(len(prices))
    return [
        Level(
            price=_q(centre),
            low=min(prices),
            high=max(prices),
            touches=len(group),
            kind=label,
            last_ts=max(s.ts for s in group),
        )
    ]


def volume_profile(
    bars: list[Bar], bin_count: int = PROFILE_BINS
) -> VolumeProfile | None:
    """Traded volume by price, with the point of control and the value area.

    Returns None when the series has no range or no volume to distribute, which
    is the honest answer: there is no profile, not a profile of zeros.
    """
    if bin_count < 1:
        raise ValueError("bin_count must be at least 1")
    if not bars:
        return None
    floor = min(bar.low for bar in bars)
    ceiling = max(bar.high for bar in bars)
    total = sum((bar.volume for bar in bars), Decimal(0))
    if ceiling <= floor or total <= 0:
        return None
    step = (ceiling - floor) / Decimal(bin_count)
    buckets = [Decimal(0)] * bin_count
    for bar in bars:
        first = _bin_of(bar.low, floor, step, bin_count)
        last = _bin_of(bar.high, floor, step, bin_count)
        spread = Decimal(last - first + 1)
        share = bar.volume / spread
        for index in range(first, last + 1):
            buckets[index] += share
    centres = [
        _q(floor + step * (Decimal(i) + Decimal("0.5"))) for i in range(bin_count)
    ]
    peak = max(range(bin_count), key=lambda i: buckets[i])
    low_index, high_index = _value_area(buckets, peak, total)
    return VolumeProfile(
        point_of_control=centres[peak],
        value_area_low=_q(floor + step * Decimal(low_index)),
        value_area_high=_q(floor + step * Decimal(high_index + 1)),
        bins=[(centres[i], _q(buckets[i])) for i in range(bin_count)],
    )


def _bin_of(price: Decimal, floor: Decimal, step: Decimal, bin_count: int) -> int:
    index = int((price - floor) / step)
    return max(0, min(bin_count - 1, index))


def _value_area(buckets: list[Decimal], peak: int, total: Decimal) -> tuple[int, int]:
    """Grow outward from the busiest bin until the share is covered."""
    target = total * VALUE_AREA_SHARE
    low = high = peak
    covered = buckets[peak]
    while covered < target and (low > 0 or high < len(buckets) - 1):
        below = buckets[low - 1] if low > 0 else Decimal(-1)
        above = buckets[high + 1] if high < len(buckets) - 1 else Decimal(-1)
        if above >= below:
            high += 1
            covered += above
        else:
            low -= 1
            covered += below
    return low, high


def projection(base: Decimal, atr_value: Decimal, horizon_days: int) -> Projection:
    """The ground price can cover in `horizon_days`, at its recent pace.

    The range is scaled by the square root of the horizon, the usual way to
    carry a one-period range forward. Read the docstring of `Projection` before
    putting any of this on screen: it is a distance, not a probability.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    if atr_value < 0:
        raise ValueError("atr_value cannot be negative")
    reach = atr_value * Decimal(str(math.sqrt(horizon_days)))
    return Projection(
        horizon_days=horizon_days,
        base=_q(base),
        near_low=_q(base - reach),
        near_high=_q(base + reach),
        far_low=_q(base - reach * 2),
        far_high=_q(base + reach * 2),
    )
