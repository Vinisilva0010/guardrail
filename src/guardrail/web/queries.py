"""Queries behind the screen.

The screen answers questions, not "here is everything". Each block below is one
question a trader asks when looking at a market, and each returns few enough
rows to read at a glance.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.orm import Session

ROWS_PER_BLOCK: Final = 8

_SNAPSHOT = """
WITH b AS (
  SELECT c.instrument_id, i.symbol, i.venue, c.ts, c.close, c.volume,
         max(c.high) OVER w AS max20,
         min(c.low)  OVER w AS min20,
         avg(c.volume) OVER w AS vol20,
         avg(c.close * c.volume) OVER w AS turnover,
         lag(c.close) OVER o AS prev_close,
         lag(c.close, 5) OVER o AS close_5d,
         row_number() OVER (PARTITION BY c.instrument_id ORDER BY c.ts DESC) AS rn
  FROM candle c
  JOIN instrument i ON i.id = c.instrument_id
  WHERE i.venue = :venue AND c.timeframe = '1d'
    AND c.ts > now() - interval '120 days'
  WINDOW o AS (PARTITION BY c.instrument_id ORDER BY c.ts),
         w AS (PARTITION BY c.instrument_id ORDER BY c.ts
               ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
)
SELECT symbol, ts, close,
       volume / nullif(vol20, 0)          AS rvol,
       close  / nullif(prev_close, 0) - 1 AS chg_1d,
       close  / nullif(close_5d, 0) - 1   AS chg_5d,
       close  / nullif(max20, 0) - 1      AS vs_high,
       close  / nullif(min20, 0) - 1      AS vs_low,
       turnover,
       EXISTS (SELECT 1 FROM catalyst k
               WHERE k.instrument_id = b.instrument_id
                 AND k.ts > b.ts - interval '72 hours') AS has_news
FROM b
WHERE rn = 1 AND max20 IS NOT NULL AND vol20 > 0 AND prev_close > 0
"""

_CRYPTO = """
WITH b AS (
  SELECT c.instrument_id, i.symbol, c.ts, c.close,
         max(c.high) OVER w AS top7,
         min(c.low)  OVER w AS bottom7,
         lag(c.close, 6)  OVER o AS close_1d,
         lag(c.close, 42) OVER o AS close_7d,
         row_number() OVER (PARTITION BY c.instrument_id ORDER BY c.ts DESC) AS rn
  FROM candle c JOIN instrument i ON i.id = c.instrument_id
  WHERE i.asset_class = 'crypto' AND c.timeframe = '4h'
    AND c.ts > now() - interval '40 days'
  WINDOW o AS (PARTITION BY c.instrument_id ORDER BY c.ts),
         w AS (PARTITION BY c.instrument_id ORDER BY c.ts
               ROWS BETWEEN 42 PRECEDING AND 1 PRECEDING)
)
SELECT symbol, close,
       close / nullif(close_1d, 0) - 1 AS chg_1d,
       close / nullif(close_7d, 0) - 1 AS chg_5d,
       close / nullif(top7, 0) - 1     AS vs_high,
       close / nullif(bottom7, 0) - 1  AS vs_low,
       (SELECT d.funding_rate FROM derivative_stat d
         WHERE d.instrument_id = b.instrument_id AND d.funding_rate IS NOT NULL
         ORDER BY d.ts DESC LIMIT 1) AS funding,
       (SELECT d.open_interest FROM derivative_stat d
         WHERE d.instrument_id = b.instrument_id AND d.open_interest IS NOT NULL
         ORDER BY d.ts DESC LIMIT 1)
       / nullif((SELECT d.open_interest FROM derivative_stat d
         WHERE d.instrument_id = b.instrument_id AND d.open_interest IS NOT NULL
           AND d.ts <= now() - interval '24 hours'
         ORDER BY d.ts DESC LIMIT 1), 0) - 1 AS oi_24h
FROM b
WHERE rn = 1 AND top7 IS NOT NULL AND close_1d > 0
ORDER BY symbol
"""


@dataclass(frozen=True, slots=True)
class Row:
    """One instrument as the screen shows it."""

    symbol: str
    venue: str
    price: Decimal
    chg_1d: Decimal
    chg_5d: Decimal
    vs_high: Decimal
    vs_low: Decimal
    rvol: Decimal | None = None
    turnover: Decimal | None = None
    has_news: bool = False
    funding: Decimal | None = None
    oi_24h: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MarketPulse:
    """How a whole market is behaving today."""

    venue: str
    total: int
    rising: int
    falling: int
    median_change: Decimal
    unusual_volume: int


def _d(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _zero(value: Any) -> Decimal:
    return _d(value) or Decimal(0)


def snapshot(session: Session, venue: str, min_turnover: Decimal) -> list[Row]:
    """Every liquid instrument on a venue, with today's context."""
    out = []
    for r in session.execute(text(_SNAPSHOT), {"venue": venue}).all():
        turnover = _d(r.turnover)
        if turnover is None or turnover < min_turnover:
            continue
        out.append(
            Row(
                symbol=r.symbol,
                venue=venue,
                price=r.close,
                chg_1d=_zero(r.chg_1d),
                chg_5d=_zero(r.chg_5d),
                vs_high=_zero(r.vs_high),
                vs_low=_zero(r.vs_low),
                rvol=_d(r.rvol),
                turnover=turnover,
                has_news=bool(r.has_news),
            )
        )
    return out


def crypto(session: Session) -> list[Row]:
    """Current state of the crypto majors."""
    return [
        Row(
            symbol=r.symbol,
            venue="binance",
            price=r.close,
            chg_1d=_zero(r.chg_1d),
            chg_5d=_zero(r.chg_5d),
            vs_high=_zero(r.vs_high),
            vs_low=_zero(r.vs_low),
            funding=_d(r.funding),
            oi_24h=_d(r.oi_24h),
        )
        for r in session.execute(text(_CRYPTO)).all()
    ]


def pulse(venue: str, rows: list[Row]) -> MarketPulse:
    """Summarise a market in the few numbers worth reading first."""
    if not rows:
        return MarketPulse(venue, 0, 0, 0, Decimal(0), 0)
    changes = sorted(r.chg_1d for r in rows)
    return MarketPulse(
        venue=venue,
        total=len(rows),
        rising=sum(1 for r in rows if r.chg_1d > 0),
        falling=sum(1 for r in rows if r.chg_1d < 0),
        median_change=changes[len(changes) // 2],
        unusual_volume=sum(1 for r in rows if r.rvol and r.rvol >= 3),
    )


def breaking_out(rows: list[Row]) -> list[Row]:
    """New 20-day high, carried by volume.

    Price alone makes a new high every time a stock drifts up. Volume is what
    says other people acted on it.
    """
    hits = [r for r in rows if r.vs_high >= 0 and r.rvol and r.rvol >= Decimal(2)]
    hits.sort(key=lambda r: r.rvol or Decimal(0), reverse=True)
    return hits[:ROWS_PER_BLOCK]


def beaten_down(rows: list[Row]) -> list[Row]:
    """Furthest below its own 20-day high."""
    hits = [r for r in rows if r.vs_high <= Decimal("-0.15")]
    hits.sort(key=lambda r: r.vs_high)
    return hits[:ROWS_PER_BLOCK]


def turning_up(rows: list[Row]) -> list[Row]:
    """Fell hard, and today it is up with volume behind it.

    Three conditions together, which is the point: a drop alone is just a drop,
    a green day alone is noise, and volume alone says nothing about direction.
    """
    hits = [
        r
        for r in rows
        if r.vs_high <= Decimal("-0.12")
        and r.chg_1d >= Decimal("0.02")
        and r.rvol
        and r.rvol >= Decimal("1.5")
    ]
    hits.sort(key=lambda r: r.rvol or Decimal(0), reverse=True)
    return hits[:ROWS_PER_BLOCK]


def unusual_volume(rows: list[Row]) -> list[Row]:
    """Heavy volume without a new high and without news.

    Something is happening that the price has not finished showing.
    """
    hits = [
        r
        for r in rows
        if r.rvol and r.rvol >= Decimal(3) and r.vs_high < 0 and not r.has_news
    ]
    hits.sort(key=lambda r: r.rvol or Decimal(0), reverse=True)
    return hits[:ROWS_PER_BLOCK]
