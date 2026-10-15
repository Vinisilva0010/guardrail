"""Read side of the screen: one function per block.

Every function takes the caller's session and `now`, returns plain data, and
caps its own row count. Liquidity floors are applied to the turnover measured up
to the date of the data being shown, never to today's: a stock that was thin when
it moved is not made tradable by having become liquid since.

A block never returns "nothing" silently. Where data may be missing because a
source is down or does not exist, the function returns a Notice saying so.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, func, or_, select, text
from sqlalchemy.orm import Session

from guardrail.analysis.event_impact import MIN_SAMPLE, is_displayable
from guardrail.db.models import (
    Catalyst,
    DerivativeStat,
    EventImpact,
    EventImpactStat,
    EventKind,
    EventReferenceAsset,
    EventScope,
    Expectation,
    Instrument,
    MacroPoint,
    NewsTheme,
    NewsThemeAsset,
    NewsThemeItem,
    ScheduledEvent,
)
from guardrail.web import coverage, health
from guardrail.web.format import age, clock, number, percent, short_date

# --- tunables ----------------------------------------------------------------

# Minimum median daily turnover, measured up to the date of the data (SPEC §9.1,
# PLANO §5.4). US$ for US and crypto-less markets, R$ for Brazil.
MIN_TURNOVER: Final = {"us": Decimal("20000000"), "br": Decimal("5000000")}

# Relative volume from which a move counts as unusual enough to be listed.
ABNORMAL_MIN_RVOL: Final = Decimal("2")
# Relative volume that earns the card its warning band (PLANO §5.5).
RVOL_WARNING: Final = Decimal("3")
# Bars that must exist before the 20-day average is trusted.
MIN_PRIOR_BARS: Final = 15

SPARK_BARS: Final = 30
SNAPSHOT_DAYS: Final = 170
HEADLINE_WINDOW: Final = timedelta(hours=72)
EVENT_WINDOW_DAYS: Final = 7
TIMELINE_HOURS: Final = 72

# Positioning data older than this is shown as old and read by no rule.
POSITIONING_MAX_AGE: Final = timedelta(hours=6)

CURRENCY: Final = {"binance": "US$", "alpaca": "US$", "b3": "R$"}
VENUE_LABEL: Final = {"binance": "Binance", "alpaca": "EUA", "b3": "B3"}
MARKET_NAME: Final = {"crypto": "cripto", "us": "EUA", "br": "Brasil"}
MARKET_VENUES: Final = {"crypto": ("binance",), "us": ("alpaca",), "br": ("b3",)}
# Scopes of market-wide events that become the "next event" of an asset card.
# The same scopes feed the historical sample (analysis.event_impact), so the
# event shown and the history beside it describe the same kind of thing.
MARKET_SCOPES: Final = {
    "crypto": (EventScope.US,),
    "us": (EventScope.US,),
    "br": (EventScope.BR,),
}
# Scopes listed in a market page's agenda, which is wider: a foreign central
# bank moves crypto too, it is simply not part of the measured sample.
AGENDA_SCOPES: Final = {
    "crypto": (EventScope.US, EventScope.GLOBAL, EventScope.CRYPTO),
    "us": (EventScope.US,),
    "br": (EventScope.BR,),
}
SOURCE_NAMES: Final = {
    "alpaca.news": "Alpaca",
    "rss.coindesk": "CoinDesk",
    "rss.decrypt": "Decrypt",
    "rss.infomoney": "InfoMoney",
    "rss.moneytimes": "Money Times",
    "rss.seudinheiro": "Seu Dinheiro",
}
MACRO_KINDS: Final = (
    EventKind.RATE_DECISION,
    EventKind.INFLATION_PRINT,
    EventKind.JOBS_PRINT,
    EventKind.GDP_PRINT,
    EventKind.ACTIVITY_PRINT,
)
IMPACT_RANK: Final = {EventImpact.HIGH: 0, EventImpact.MEDIUM: 1, EventImpact.LOW: 2}


# --- data ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Notice:
    """A visible statement about missing or old data."""

    kind: str  # "warn" or "alert"
    text: str


@dataclass(frozen=True, slots=True)
class StatView:
    """A historical distribution as the screen may print it."""

    n: int
    reference: str
    median_signed: Decimal | None
    median_abs: Decimal | None
    p10: Decimal | None
    p90: Decimal | None
    pct_positive: Decimal | None

    @property
    def displayable(self) -> bool:
        return is_displayable(self.n)

    @property
    def insufficient_text(self) -> str:
        return f"histórico insuficiente (n={self.n})"


@dataclass(frozen=True, slots=True)
class EventView:
    """A scheduled event ready to render."""

    id: int
    title: str
    kind: EventKind
    scope: EventScope
    impact: EventImpact
    starts_at: datetime
    has_time: bool
    date_is_estimated: bool
    expected: Decimal | None
    previous: Decimal | None
    unit: str | None
    symbol: str | None
    source: str
    history: StatView | None = None
    affects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HeadlineView:
    """A news item for a card."""

    title: str
    outlet: str
    ts: datetime
    url: str | None


@dataclass(frozen=True, slots=True)
class Positioning:
    """Funding and open interest of a perpetual, with their ages."""

    funding: Decimal | None
    funding_ts: datetime | None
    oi_change: Decimal | None
    oi_ts: datetime | None
    stale: bool
    reading: str | None
    notice: Notice | None


@dataclass(slots=True)
class AssetCard:
    """Everything one asset card shows."""

    instrument_id: int
    symbol: str
    label: str
    venue: str
    market: str
    currency: str
    last_day: date
    last_close: Decimal
    change: Decimal | None
    closes: list[Decimal]
    rvol: Decimal | None
    turnover: Decimal | None
    stale: bool
    expected_day: date
    next_event: EventView | None = None
    event_notice: Notice | None = None
    positioning: Positioning | None = None
    history: StatView | None = None
    headline: HeadlineView | None = None
    headline_notice: Notice | None = None

    @property
    def volume_warning(self) -> bool:
        return self.rvol is not None and self.rvol >= RVOL_WARNING


@dataclass(frozen=True, slots=True)
class Snap:
    """Last bars of one instrument, before it becomes a card."""

    instrument_id: int
    symbol: str
    venue: str
    last_day: date
    last_close: Decimal
    prev_close: Decimal | None
    rvol: Decimal | None
    turnover: Decimal | None


@dataclass(frozen=True, slots=True)
class Block[T]:
    """Rows for a block, or the notice that explains why there are none."""

    rows: list[T]
    notice: Notice | None = None


@dataclass(frozen=True, slots=True)
class ThemeView:
    """A cluster of headlines and the assets that moved with it."""

    label: str
    n_items: int
    n_outlets: int
    assets: tuple[tuple[str, Decimal], ...]
    headlines: tuple[HeadlineView, ...]
    extra: int


@dataclass(frozen=True, slots=True)
class MacroLine:
    """One realised or expected macro figure."""

    label: str
    value: str
    detail: str


@dataclass(slots=True)
class Timeline:
    """High-impact events of the next 72 hours with their x position (0-100)."""

    events: list[tuple[EventView, float, int]] = field(default_factory=list)


# --- helpers ------------------------------------------------------------------


def display_symbol(symbol: str, venue: str) -> str:
    """'BTCUSDT' -> 'BTC'; other venues keep their ticker."""
    if venue == "binance" and symbol.endswith("USDT"):
        return symbol[: -len("USDT")]
    return symbol


def market_of(venue: str) -> str:
    """Market key of a venue."""
    return next(m for m, venues in MARKET_VENUES.items() if venue in venues)


def _closed_days(
    session: Session, scope: EventScope, start: date, end: date
) -> set[date]:
    rows = session.execute(
        select(ScheduledEvent.starts_at).where(
            ScheduledEvent.kind == EventKind.HOLIDAY,
            ScheduledEvent.scope == scope,
            ScheduledEvent.title.in_(["US market closed"])
            if scope is EventScope.US
            else ScheduledEvent.title.like("B3 closed:%"),
            ScheduledEvent.starts_at >= datetime.combine(start, time(), tzinfo=UTC),
            ScheduledEvent.starts_at <= datetime.combine(end, time(), tzinfo=UTC),
        )
    ).scalars()
    return {r.astimezone(UTC).date() for r in rows}


# B3 closing bars are collected on Tuesday and Friday evenings (PLANO §3.5), so
# between collections the newest bar is legitimately a few days old. Kept in step
# with the b3_close job by a test.
B3_COLLECTION_WEEKDAYS: Final = (1, 4)  # Tuesday, Friday
NEW_YORK: Final = ZoneInfo("America/New_York")


def expected_last_bar(session: Session, market: str, now: datetime) -> date:
    """The newest daily bar that should exist for a market right now.

    * crypto: daily bars are UTC, so yesterday's is the last closed one;
    * US: the free feed serves bars through New York's previous day, and the
      last trading day among those;
    * Brazil: the session of the latest Tuesday or Friday before today, the days
      the closing collection runs; older than that is genuinely stale.
    """
    today = now.astimezone(UTC).date()
    if market == "crypto":
        return today - timedelta(days=1)

    if market == "us":
        day = now.astimezone(NEW_YORK).date() - timedelta(days=1)
        scope = EventScope.US
    else:
        day = today - timedelta(days=1)
        while day.weekday() not in B3_COLLECTION_WEEKDAYS:
            day -= timedelta(days=1)
        scope = EventScope.BR

    closed = _closed_days(session, scope, day - timedelta(days=10), day)
    while day.weekday() >= 5 or day in closed:
        day -= timedelta(days=1)
    return day


def _stat_view(row: Any, reference: str) -> StatView:
    return StatView(
        n=row.n,
        reference=reference,
        median_signed=row.median_signed,
        median_abs=row.median_abs,
        p10=row.p10,
        p90=row.p90,
        pct_positive=row.pct_positive,
    )


def _insufficient(reference: str) -> StatView:
    return StatView(0, reference, None, None, None, None, None)


# --- header -------------------------------------------------------------------


def reference_labels(session: Session) -> dict[int, str]:
    """Display symbol of each reference asset that has candles."""
    rows = session.execute(
        select(Instrument.id, Instrument.symbol, Instrument.venue)
        .join(
            EventReferenceAsset,
            (EventReferenceAsset.venue == Instrument.venue)
            & (EventReferenceAsset.symbol == Instrument.symbol),
        )
        .order_by(EventReferenceAsset.id)
    ).all()
    return {r.id: display_symbol(r.symbol, r.venue) for r in rows}


# --- events -------------------------------------------------------------------


def _event_view(row: Any, symbol: str | None) -> EventView:
    return EventView(
        id=row.id,
        title=row.title,
        kind=row.kind,
        scope=row.scope,
        impact=row.impact,
        starts_at=row.starts_at,
        has_time=row.has_time,
        date_is_estimated=row.date_is_estimated,
        expected=row.expected,
        previous=row.previous,
        unit=row.unit,
        symbol=symbol,
        source=row.source,
    )


def upcoming_events(
    session: Session,
    now: datetime,
    days: int,
    *,
    scopes: tuple[EventScope, ...] | None = None,
    impacts: tuple[EventImpact, ...] | None = None,
    kinds: tuple[EventKind, ...] | None = None,
    include_holidays: bool = True,
    limit: int = 60,
) -> list[EventView]:
    """Events from now to now + days, high impact first, then by time."""
    stmt = (
        select(ScheduledEvent, Instrument.symbol, Instrument.venue)
        .outerjoin(Instrument, Instrument.id == ScheduledEvent.instrument_id)
        .where(
            not_yet_over(now),
            ScheduledEvent.starts_at <= now + timedelta(days=days),
        )
    )
    if scopes is not None:
        stmt = stmt.where(ScheduledEvent.scope.in_(scopes))
    if impacts is not None:
        stmt = stmt.where(ScheduledEvent.impact.in_(impacts))
    if kinds is not None:
        stmt = stmt.where(ScheduledEvent.kind.in_(kinds))
    if not include_holidays:
        stmt = stmt.where(ScheduledEvent.kind != EventKind.HOLIDAY)
    rows = session.execute(
        stmt.order_by(ScheduledEvent.starts_at).limit(limit * 4)
    ).all()
    views = [
        _event_view(ev, display_symbol(sym, venue) if sym else None)
        for ev, sym, venue in rows
    ]
    views.sort(key=lambda e: (IMPACT_RANK[e.impact], e.starts_at))
    return views[:limit]


def not_yet_over(now: datetime) -> ColumnElement[bool]:
    """Events still ahead. One with an hour must be after now; a date-only one
    (stored at midnight UTC) stays until its day ends."""
    return or_(
        ScheduledEvent.starts_at >= now,
        and_(
            ScheduledEvent.has_time.is_(False),
            ScheduledEvent.starts_at >= _start_of_day(now),
        ),
    )


def _start_of_day(now: datetime) -> datetime:
    """Midnight UTC today: date-only events are stored at 00:00 UTC and must not
    vanish from "upcoming" once the day has begun."""
    return datetime.combine(now.astimezone(UTC).date(), time(), tzinfo=UTC)


def history_for(
    session: Session, kind: EventKind, labels: dict[int, str]
) -> StatView | None:
    """The best-sampled d1 distribution for an event kind across reference assets.

    Always returns something for a kind the screen shows: with no stored row it
    is an n=0 view, so the card says "histórico insuficiente" and not nothing.
    """
    if not labels:
        return None
    rows = session.execute(
        select(EventImpactStat)
        .where(
            EventImpactStat.kind == kind,
            EventImpactStat.horizon == "d1",
            EventImpactStat.instrument_id.in_(list(labels)),
        )
        .order_by(EventImpactStat.n.desc())
    ).scalars()
    best = next(iter(rows), None)
    first = next(iter(labels.values()))
    if best is None:
        return _insufficient(first)
    return _stat_view(best, labels[best.instrument_id])


def decorate(session: Session, events: list[EventView]) -> list[EventView]:
    """Attach the historical distribution and the assets each event affects."""
    labels = reference_labels(session)
    affects = tuple(labels.values())
    cache: dict[EventKind, StatView | None] = {}
    out: list[EventView] = []
    for event in events:
        if event.symbol is None:
            if event.kind not in cache:
                cache[event.kind] = history_for(session, event.kind, labels)
            out.append(
                EventView(
                    **{
                        **_fields(event),
                        "history": cache[event.kind],
                        "affects": affects,
                    }
                )
            )
        else:
            out.append(event)
    return out


def _fields(event: EventView) -> dict[str, Any]:
    return {name: getattr(event, name) for name in event.__slots__}


def timeline(session: Session, now: datetime) -> Block[tuple[EventView, float, int]]:
    """High-impact events of the next 72 hours, placed along the axis.

    The third element is a row (0-2) so neighbouring labels do not overlap.
    """
    events = [
        e
        for e in upcoming_events(
            session,
            now,
            days=TIMELINE_HOURS // 24,
            impacts=(EventImpact.HIGH,),
            include_holidays=False,
            limit=40,
        )
        if e.starts_at >= now
    ]
    placed: list[tuple[EventView, float, int]] = []
    for index, event in enumerate(sorted(events, key=lambda e: e.starts_at)):
        hours = (event.starts_at - now).total_seconds() / 3600
        if hours > TIMELINE_HOURS:
            continue
        placed.append((event, round(hours / TIMELINE_HOURS * 100, 2), index % 3))
    return Block(placed, None if placed else macro_gap_notice(session, now))


def macro_gap_notice(session: Session, now: datetime) -> Notice:
    """Why an empty macro agenda cannot be read as an empty week."""
    state, detail = health.group_state(session, "forexfactory", now)
    if state is not health.State.OK:
        return Notice("alert", f"sem dado · calendário macro {state.label}: {detail}")
    return Notice(
        "warn", f"nenhum evento de alto impacto listado · {coverage.MACRO_HORIZON_NOTE}"
    )


# --- asset cards ---------------------------------------------------------------

_SNAPSHOT_SQL = text(
    """
    WITH recent AS (
        SELECT c.instrument_id, i.symbol, i.venue, c.ts, c.close, c.volume,
               row_number() OVER (
                   PARTITION BY c.instrument_id ORDER BY c.ts DESC
               ) AS rn
        FROM candle c JOIN instrument i ON i.id = c.instrument_id
        WHERE c.timeframe = '1d' AND i.venue = ANY(:venues) AND c.ts >= :since
    )
    SELECT instrument_id, symbol, venue,
           max(ts)     FILTER (WHERE rn = 1) AS last_ts,
           max(close)  FILTER (WHERE rn = 1) AS last_close,
           max(close)  FILTER (WHERE rn = 2) AS prev_close,
           max(volume) FILTER (WHERE rn = 1) AS last_volume,
           avg(volume) FILTER (WHERE rn BETWEEN 2 AND 21) AS avg_volume_20,
           count(*)    FILTER (WHERE rn BETWEEN 2 AND 21) AS prior_bars,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY close * volume)
               FILTER (WHERE rn <= 90) AS median_turnover_90
    FROM recent
    GROUP BY instrument_id, symbol, venue
    """
)


def market_snapshot(session: Session, market: str, now: datetime) -> list[Snap]:
    """Last bar, relative volume and turnover of every instrument in a market."""
    rows = session.execute(
        _SNAPSHOT_SQL,
        {
            "venues": list(MARKET_VENUES[market]),
            "since": now - timedelta(days=SNAPSHOT_DAYS),
        },
    ).all()
    out: list[Snap] = []
    for row in rows:
        rvol = None
        if (
            row.avg_volume_20 is not None
            and row.avg_volume_20 > 0
            and row.prior_bars >= MIN_PRIOR_BARS
        ):
            rvol = Decimal(row.last_volume) / Decimal(row.avg_volume_20)
        out.append(
            Snap(
                instrument_id=row.instrument_id,
                symbol=row.symbol,
                venue=row.venue,
                last_day=row.last_ts.astimezone(UTC).date(),
                last_close=row.last_close,
                prev_close=row.prev_close,
                rvol=rvol,
                turnover=(
                    Decimal(row.median_turnover_90)
                    if row.median_turnover_90 is not None
                    else None
                ),
            )
        )
    return out


def _closes(session: Session, ids: list[int]) -> dict[int, list[Decimal]]:
    if not ids:
        return {}
    rows = session.execute(
        text(
            """
            SELECT instrument_id, close FROM (
                SELECT instrument_id, ts, close,
                       row_number() OVER (
                           PARTITION BY instrument_id ORDER BY ts DESC
                       ) AS rn
                FROM candle
                WHERE instrument_id = ANY(:ids) AND timeframe = '1d'
            ) t WHERE rn <= :bars ORDER BY instrument_id, ts
            """
        ),
        {"ids": ids, "bars": SPARK_BARS},
    ).all()
    out: dict[int, list[Decimal]] = defaultdict(list)
    for instrument_id, close in rows:
        out[instrument_id].append(close)
    return out


def positioning(session: Session, instrument_id: int, now: datetime) -> Positioning:
    """Funding and 24h open-interest change, each with its own age."""
    funding_row = session.execute(
        select(DerivativeStat.funding_rate, DerivativeStat.ts)
        .where(
            DerivativeStat.instrument_id == instrument_id,
            DerivativeStat.funding_rate.is_not(None),
        )
        .order_by(DerivativeStat.ts.desc())
        .limit(1)
    ).first()
    oi_row = session.execute(
        select(DerivativeStat.open_interest_value, DerivativeStat.ts)
        .where(
            DerivativeStat.instrument_id == instrument_id,
            DerivativeStat.open_interest_value.is_not(None),
        )
        .order_by(DerivativeStat.ts.desc())
        .limit(1)
    ).first()

    funding = funding_row[0] if funding_row else None
    funding_ts = funding_row[1] if funding_row else None
    oi_ts = oi_row[1] if oi_row else None
    change: Decimal | None = None
    if oi_row is not None:
        earlier = session.execute(
            select(DerivativeStat.open_interest_value)
            .where(
                DerivativeStat.instrument_id == instrument_id,
                DerivativeStat.open_interest_value.is_not(None),
                DerivativeStat.ts <= oi_row[1] - timedelta(hours=24),
            )
            .order_by(DerivativeStat.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        current = oi_row[0]
        if earlier is not None and earlier > 0 and current is not None:
            change = current / earlier - 1

    newest = max((t for t in (funding_ts, oi_ts) if t is not None), default=None)
    if newest is None:
        return Positioning(
            None,
            None,
            None,
            None,
            True,
            None,
            Notice("alert", "sem dado · nenhuma linha de funding ou open interest"),
        )
    stale = now - newest > POSITIONING_MAX_AGE
    notice = None
    if stale:
        notice = Notice(
            "warn",
            "dado velho · posicionamento de "
            f"{short_date(newest.astimezone(UTC).date())} "
            f"{clock(newest)} ({age(newest, now)}); não entra em nenhuma leitura",
        )
    return Positioning(
        funding,
        funding_ts,
        change,
        oi_ts,
        stale,
        None if stale else reading(funding, change),
        notice,
    )


def reading(funding: Decimal | None, oi_change: Decimal | None) -> str | None:
    """One plain-Portuguese line from the sign of funding and open interest.

    Funding above zero means longs pay shorts. Describes who is paying and
    whether positions are growing; it does not say what price will do.
    """
    if funding is None or oi_change is None:
        return None
    flat = Decimal("0.02")
    if abs(oi_change) < flat:
        return "Posicionamento estável"
    growing = oi_change > 0
    if funding > 0:
        payer = "comprados pagando"
    elif funding < 0:
        payer = "vendidos pagando"
    else:
        payer = "funding neutro"
    return f"Posições {'aumentando' if growing else 'reduzindo'}, {payer}"


def latest_headline(
    session: Session, instrument_id: int, now: datetime
) -> tuple[HeadlineView | None, Notice | None]:
    """The newest headline in the last 72 hours, or why there is none."""
    row = session.execute(
        select(Catalyst)
        .where(
            Catalyst.instrument_id == instrument_id,
            Catalyst.ts >= now - HEADLINE_WINDOW,
            Catalyst.ts <= now,
        )
        .order_by(Catalyst.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return _headline(row), None
    for key in ("alpaca.news", "rss"):
        state, detail = health.group_state(session, key, now)
        if state is not health.State.OK:
            return None, Notice(
                "alert", f"sem dado · fonte de notícia {state.label}: {detail}"
            )
    return None, Notice("warn", "sem manchete nas últimas 72 h")


def _headline(row: Catalyst) -> HeadlineView:
    return HeadlineView(
        title=row.headline,
        outlet=SOURCE_NAMES.get(row.source, row.source),
        ts=row.ts,
        url=row.url,
    )


def build_cards(
    session: Session,
    snaps: list[Snap],
    market: str,
    now: datetime,
    market_events: list[EventView],
) -> list[AssetCard]:
    """Turn snapshots into full cards with event, positioning, history, headline."""
    if not snaps:
        return []
    expected = expected_last_bar(session, market, now)
    closes = _closes(session, [s.instrument_id for s in snaps])
    labels = reference_labels(session)

    bound = session.execute(
        select(ScheduledEvent, Instrument.symbol, Instrument.venue)
        .join(Instrument, Instrument.id == ScheduledEvent.instrument_id)
        .where(
            ScheduledEvent.instrument_id.in_([s.instrument_id for s in snaps]),
            not_yet_over(now),
            ScheduledEvent.starts_at <= now + timedelta(days=EVENT_WINDOW_DAYS),
        )
        .order_by(ScheduledEvent.starts_at)
    ).all()
    first_bound: dict[int, EventView] = {}
    for ev, sym, venue in bound:
        if ev.instrument_id is not None:
            first_bound.setdefault(
                ev.instrument_id, _event_view(ev, display_symbol(sym, venue))
            )

    cards: list[AssetCard] = []
    for snap in snaps:
        change = (
            snap.last_close / snap.prev_close - 1
            if snap.prev_close and snap.prev_close > 0
            else None
        )
        card = AssetCard(
            instrument_id=snap.instrument_id,
            symbol=snap.symbol,
            label=display_symbol(snap.symbol, snap.venue),
            venue=snap.venue,
            market=market,
            currency=CURRENCY[snap.venue],
            last_day=snap.last_day,
            last_close=snap.last_close,
            change=change,
            closes=closes.get(snap.instrument_id, []),
            rvol=snap.rvol,
            turnover=snap.turnover,
            stale=snap.last_day < expected,
            expected_day=expected,
        )
        event = first_bound.get(snap.instrument_id) or next(
            (e for e in market_events if e.scope in MARKET_SCOPES[market]), None
        )
        card.next_event = event
        if event is None:
            card.event_notice = Notice(
                "warn",
                "sem evento marcado · cobertura parcial: "
                + coverage.ASSET_EVENT_GAPS[market],
            )
        else:
            card.history = history_for_asset(
                session, event.kind, snap.instrument_id, card.label
            )
        if market == "crypto":
            card.positioning = positioning(session, snap.instrument_id, now)
        card.headline, card.headline_notice = latest_headline(
            session, snap.instrument_id, now
        )
        cards.append(card)
    del labels
    return cards


def history_for_asset(
    session: Session, kind: EventKind, instrument_id: int, label: str
) -> StatView:
    """The d1 distribution of an event kind for one asset (n=0 view if none)."""
    row = session.execute(
        select(EventImpactStat).where(
            EventImpactStat.kind == kind,
            EventImpactStat.instrument_id == instrument_id,
            EventImpactStat.horizon == "d1",
        )
    ).scalar_one_or_none()
    return _insufficient(label) if row is None else _stat_view(row, label)


def abnormal_cards(
    session: Session, market: str, now: datetime, limit: int
) -> Block[AssetCard]:
    """Assets of a market with unusual volume, by relative volume.

    Instruments whose last bar is older than expected are left out and the block
    says so: a stale move would be read as today's.
    """
    snaps = market_snapshot(session, market, now)
    if not snaps:
        return Block(
            [],
            Notice(
                "alert",
                f"sem dado · nenhuma vela diária de {MARKET_NAME[market]} no banco",
            ),
        )

    expected = expected_last_bar(session, market, now)
    fresh = [s for s in snaps if s.last_day >= expected]
    if not fresh:
        newest = max(s.last_day for s in snaps)
        return Block(
            [],
            Notice(
                "alert",
                f"sem dado atual · última vela de {MARKET_NAME[market]} é de "
                f"{short_date(newest)}, "
                f"esperada {short_date(expected)}; coletor de barras parado",
            ),
        )

    floor = MIN_TURNOVER.get(market)
    eligible = [
        s
        for s in fresh
        if s.rvol is not None
        and s.rvol >= ABNORMAL_MIN_RVOL
        and (floor is None or (s.turnover is not None and s.turnover >= floor))
    ]
    eligible.sort(key=lambda s: s.rvol or Decimal(0), reverse=True)
    chosen = eligible[:limit]
    if not chosen:
        return Block(
            [],
            Notice(
                "warn",
                "nenhum ativo com volume ≥ "
                f"{number(ABNORMAL_MIN_RVOL, 0)}x a média de 20 dias",
            ),
        )

    events = decorate(
        session,
        upcoming_events(
            session,
            now,
            EVENT_WINDOW_DAYS,
            impacts=(EventImpact.HIGH,),
            scopes=MARKET_SCOPES[market],
            include_holidays=False,
            limit=10,
        ),
    )
    return Block(build_cards(session, chosen, market, now, events))


def positioning_cards(session: Session, now: datetime) -> Block[AssetCard]:
    """BTC, ETH and SOL with their positioning, for the crypto page."""
    snaps = [s for s in market_snapshot(session, "crypto", now)]
    snaps.sort(key=lambda s: s.symbol)
    if not snaps:
        return Block([], Notice("alert", "sem dado · nenhuma vela de cripto no banco"))
    events = decorate(
        session,
        upcoming_events(
            session,
            now,
            EVENT_WINDOW_DAYS,
            impacts=(EventImpact.HIGH,),
            scopes=MARKET_SCOPES["crypto"],
            include_holidays=False,
            limit=10,
        ),
    )
    return Block(build_cards(session, snaps, "crypto", now, events))


def bound_event_cards(session: Session, market: str, now: datetime) -> Block[AssetCard]:
    """Assets of a market with an event of their own in the next 7 days."""
    ids = list(
        session.execute(
            select(ScheduledEvent.instrument_id)
            .join(Instrument, Instrument.id == ScheduledEvent.instrument_id)
            .where(
                Instrument.venue.in_(MARKET_VENUES[market]),
                not_yet_over(now),
                ScheduledEvent.starts_at <= now + timedelta(days=EVENT_WINDOW_DAYS),
            )
            .distinct()
            .limit(24)
        ).scalars()
    )
    if not ids:
        return Block(
            [],
            Notice(
                "warn",
                "nenhum ativo com evento próprio listado · cobertura parcial: "
                + coverage.ASSET_EVENT_GAPS[market],
            ),
        )
    snaps = [
        s for s in market_snapshot(session, market, now) if s.instrument_id in set(ids)
    ]
    return Block(build_cards(session, snaps, market, now, []))


# --- themes -------------------------------------------------------------------

THEME_MIN_ITEMS: Final = 4
THEME_MIN_OUTLETS: Final = 2


def themes(session: Session, now: datetime, hours: int, limit: int) -> Block[ThemeView]:
    """The most active themes of the last `hours`, only those worth showing."""
    since = now - timedelta(hours=hours)
    story = func.concat(Catalyst.source, "|", Catalyst.headline)
    rows = session.execute(
        select(
            NewsTheme.id,
            NewsTheme.label,
            func.count(func.distinct(story)).label("items"),
            func.count(func.distinct(Catalyst.source)).label("outlets"),
        )
        .join(NewsThemeItem, NewsThemeItem.theme_id == NewsTheme.id)
        .join(Catalyst, Catalyst.id == NewsThemeItem.catalyst_id)
        .where(NewsTheme.last_seen_at >= since)
        .group_by(NewsTheme.id, NewsTheme.label)
        .having(
            func.count(func.distinct(story)) >= THEME_MIN_ITEMS,
            func.count(func.distinct(Catalyst.source)) >= THEME_MIN_OUTLETS,
        )
        .order_by(func.count(func.distinct(story)).desc())
        .limit(limit)
    ).all()
    if not rows:
        return Block([], _theme_notice(session, now))

    views: list[ThemeView] = []
    for row in rows:
        assets = session.execute(
            select(Instrument.symbol, Instrument.venue, NewsThemeAsset.move_pct)
            .join(Instrument, Instrument.id == NewsThemeAsset.instrument_id)
            .where(NewsThemeAsset.theme_id == row.id)
            .order_by(func.abs(NewsThemeAsset.move_pct).desc())
            .limit(6)
        ).all()
        items = session.execute(
            select(Catalyst)
            .join(NewsThemeItem, NewsThemeItem.catalyst_id == Catalyst.id)
            .where(NewsThemeItem.theme_id == row.id)
            .order_by(Catalyst.ts.desc())
            .limit(2)
        ).scalars()
        views.append(
            ThemeView(
                label=row.label,
                n_items=row.items,
                n_outlets=row.outlets,
                assets=tuple((display_symbol(s, v), m) for s, v, m in assets),
                headlines=tuple(_headline(i) for i in items),
                extra=max(row.items - 2, 0),
            )
        )
    return Block(views)


def _theme_notice(session: Session, now: datetime) -> Notice:
    for key in ("alpaca.news", "rss"):
        state, detail = health.group_state(session, key, now)
        if state is not health.State.OK:
            return Notice(
                "alert", f"sem dado · fonte de notícia {state.label}: {detail}"
            )
    total = session.execute(select(func.count()).select_from(NewsTheme)).scalar_one()
    if total == 0:
        return Notice("warn", "sem temas · o agrupamento de notícias ainda não rodou")
    return Notice(
        "warn",
        f"nenhum tema com ≥ {THEME_MIN_ITEMS} notícias de "
        f"≥ {THEME_MIN_OUTLETS} veículos nas últimas horas",
    )


# --- macro and Brazil ----------------------------------------------------------


def macro_events(session: Session, now: datetime, days: int = 30) -> Block[EventView]:
    """Macro releases and decisions ahead, with expected, previous and history."""
    events = decorate(
        session,
        upcoming_events(
            session,
            now,
            days,
            kinds=MACRO_KINDS,
            impacts=(EventImpact.HIGH, EventImpact.MEDIUM),
            limit=40,
        ),
    )
    if events:
        return Block(events)
    return Block([], macro_gap_notice(session, now))


def brazil_macro(session: Session, now: datetime) -> Block[MacroLine]:
    """Selic, IPCA and the latest Focus medians, each with its date."""
    lines: list[MacroLine] = []
    for code, label in (("SELIC_TARGET", "Selic meta"), ("IPCA_MOM", "IPCA mensal")):
        row = session.execute(
            select(MacroPoint.ref_date, MacroPoint.value)
            .where(MacroPoint.code == code)
            .order_by(MacroPoint.ref_date.desc())
            .limit(1)
        ).first()
        if row is not None:
            lines.append(
                MacroLine(label, number(row[1], 2) + "%", f"em {short_date(row[0])}")
            )

    published = session.execute(select(func.max(Expectation.published_on))).scalar_one()
    if published is not None:
        this_year = now.year
        for code, label in (
            ("IPCA", "IPCA"),
            ("SELIC", "Selic"),
            ("PIB", "PIB"),
            ("CAMBIO", "Câmbio"),
        ):
            rows = session.execute(
                select(Expectation.horizon_year, Expectation.median)
                .where(
                    Expectation.code == code,
                    Expectation.published_on == published,
                    Expectation.horizon_year.in_([this_year, this_year + 1]),
                )
                .order_by(Expectation.horizon_year)
            ).all()
            if rows:
                value = " · ".join(f"{y}: {number(m, 2)}" for y, m in rows)
                lines.append(
                    MacroLine(
                        f"Focus {label} (mediana)",
                        value,
                        f"publicado {short_date(published)}",
                    )
                )
    if lines:
        return Block(lines)
    return Block([], Notice("alert", "sem dado · nenhuma série do BCB no banco"))


def percent_text(value: Decimal | None) -> str:
    """Thin wrapper so templates share one percent format."""
    return percent(value)


__all__ = ["MIN_SAMPLE"]
