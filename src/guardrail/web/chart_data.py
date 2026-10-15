"""Chart payload for one asset: candles, indicators and levels.

Read-only, and built entirely from candles already stored: nothing here calls
an exchange, so the chart draws the same whether or not the collectors are
running. It always reports how old the last bar is, because a chart with no
date on it is the easiest way to read a stale price as the current one.

Decimal is used for every calculation and converted to float only on the way
out, because the chart library takes numbers. The conversion happens in one
place, `_f`, so it is obvious where precision stops mattering.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.db.models import Candle, Instrument
from guardrail.web import indicators

TIMEFRAMES: Final = ("1h", "4h", "1d")
DEFAULT_LIMIT: Final = 300
MAX_LIMIT: Final = 1000
# Symbols come from the URL. The query is parameterised either way, but a
# pattern keeps nonsense out of the lookup and out of the page title.
SYMBOL_PATTERN: Final = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

EMA_FAST: Final = 21
EMA_SLOW: Final = 50
# Horizons the reach bands are drawn for: tomorrow, next week, next month.
HORIZONS: Final = (1, 5, 20)
MAX_LEVELS: Final = 12


class UnknownSymbol(LookupError):
    """No instrument with that symbol, or no candles for that timeframe."""


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def load_bars(
    session: Session,
    symbol: str,
    timeframe: str,
    limit: int = DEFAULT_LIMIT,
) -> tuple[Instrument, list[indicators.Bar]]:
    """The most recent `limit` bars, oldest first.

    Fetched newest-first and reversed, so a symbol with ten years of history
    costs the same as one with ten bars.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
    if not SYMBOL_PATTERN.match(symbol):
        raise ValueError("symbol has characters that no instrument uses")
    limit = max(1, min(MAX_LIMIT, limit))

    instrument = session.scalars(
        select(Instrument)
        .where(Instrument.symbol == symbol.upper())
        .order_by(Instrument.is_active.desc(), Instrument.id)
        .limit(1)
    ).first()
    if instrument is None:
        raise UnknownSymbol(symbol)

    rows = session.scalars(
        select(Candle)
        .where(
            Candle.instrument_id == instrument.id,
            Candle.timeframe == timeframe,
        )
        .order_by(Candle.ts.desc())
        .limit(limit)
    ).all()
    if not rows:
        raise UnknownSymbol(f"{symbol} has no {timeframe} candles")

    bars = [
        indicators.Bar(
            ts=row.ts,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )
        for row in reversed(rows)
    ]
    return instrument, bars


def build(
    session: Session,
    symbol: str,
    timeframe: str,
    now: datetime,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Everything the chart draws, in one payload."""
    instrument, bars = load_bars(session, symbol, timeframe, limit)
    closes = [bar.close for bar in bars]

    ema_fast = indicators.ema(closes, EMA_FAST)
    ema_slow = indicators.ema(closes, EMA_SLOW)
    rsi = indicators.rsi(closes)
    atr = indicators.atr(bars)
    levels = indicators.liquidity_levels(indicators.swings(bars))
    profile = indicators.volume_profile(bars)

    last = bars[-1]
    last_atr = next((value for value in reversed(atr) if value is not None), None)
    reach = (
        [_reach(indicators.projection(last.close, last_atr, days)) for days in HORIZONS]
        if last_atr is not None
        else []
    )

    return {
        "meta": {
            "symbol": instrument.symbol,
            "venue": instrument.venue,
            "timeframe": timeframe,
            "bars": len(bars),
            "last_ts": last.ts.astimezone(UTC).isoformat(),
            "last_close": _f(last.close),
            "age_hours": round(
                (now - last.ts.astimezone(UTC)).total_seconds() / 3600, 1
            ),
        },
        "candles": [
            {
                "time": int(bar.ts.timestamp()),
                "open": _f(bar.open),
                "high": _f(bar.high),
                "low": _f(bar.low),
                "close": _f(bar.close),
                "volume": _f(bar.volume),
            }
            for bar in bars
        ],
        "ema_fast": _line(bars, ema_fast, EMA_FAST),
        "ema_slow": _line(bars, ema_slow, EMA_SLOW),
        "rsi": _line(bars, rsi, indicators.RSI_PERIOD),
        "levels": [
            {
                "price": _f(level.price),
                "low": _f(level.low),
                "high": _f(level.high),
                "touches": level.touches,
                "kind": level.kind,
                "last_ts": level.last_ts.astimezone(UTC).isoformat(),
            }
            for level in levels[:MAX_LEVELS]
        ],
        "profile": None
        if profile is None
        else {
            "point_of_control": _f(profile.point_of_control),
            "value_area_low": _f(profile.value_area_low),
            "value_area_high": _f(profile.value_area_high),
            "bins": [
                {"price": _f(price), "volume": _f(volume)}
                for price, volume in profile.bins
            ],
        },
        "reach": reach,
        "atr": _f(last_atr),
    }


def _line(
    bars: list[indicators.Bar], values: list[Decimal | None], period: int
) -> dict[str, Any]:
    """One indicator series, with the leading Nones dropped.

    The chart library wants points, not holes, so the warm-up period is left
    out rather than sent as nulls. `period` travels with it so the page can
    label the line honestly.
    """
    return {
        "period": period,
        "points": [
            {"time": int(bar.ts.timestamp()), "value": _f(value)}
            for bar, value in zip(bars, values, strict=True)
            if value is not None
        ],
    }


def _reach(projection: indicators.Projection) -> dict[str, Any]:
    return {
        "horizon_days": projection.horizon_days,
        "base": _f(projection.base),
        "near_low": _f(projection.near_low),
        "near_high": _f(projection.near_high),
        "far_low": _f(projection.far_low),
        "far_high": _f(projection.far_high),
    }
