"""The four alerts, as pure functions (PLANO_ANTECIPACAO Phase 6).

1. a high-impact event 24 hours and 1 hour before it happens;
2. relative volume above 3x on a watchlist asset;
3. a position whose review date has passed;
4. a source broken for more than 6 hours.

Nothing else is ever sent. Each alert carries a key that says what it is about,
so the same condition seen again on the next pass does not send a second message.
Text is Portuguese, for the operator; it is plain text, never markup.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

from guardrail.web import format as fmt

# Hours before an event at which the alert goes out.
EVENT_THRESHOLDS: Final = ((timedelta(hours=24), "24h"), (timedelta(hours=1), "1h"))
VOLUME_THRESHOLD: Final = Decimal("3")
SOURCE_DOWN_AFTER: Final = timedelta(hours=6)
# At most this many source alerts per pass; the rest follow on the next one.
MAX_SOURCE_ALERTS: Final = 10


@dataclass(frozen=True, slots=True)
class Alert:
    """One message to send."""

    key: str
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class EventInfo:
    """An upcoming event, as the alert needs it."""

    identity_key: str
    title: str
    kind: str
    starts_at: datetime
    has_time: bool
    expected: Decimal | None
    previous: Decimal | None
    unit: str | None
    symbol: str | None
    affects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    """A watchlist asset's latest bar."""

    instrument_id: int
    label: str
    last_day: date
    rvol: Decimal
    change: Decimal | None


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """A source that looks broken."""

    source: str
    label: str
    last_success_at: datetime | None
    failures: int
    error: str | None


@dataclass(frozen=True, slots=True)
class ReviewInfo:
    """A position past its review date."""

    position_key: str
    label: str
    review_by: date


# --- 1. events ---------------------------------------------------------------------


def _event_text(event: EventInfo, headline: str) -> str:
    lines = [
        f"ALTO IMPACTO {headline}: {event.title}",
        fmt.event_when(event.starts_at, event.has_time),
    ]
    figures = []
    if event.expected is not None:
        figures.append(f"Esperado {fmt.figure(event.expected, event.unit)}")
    if event.previous is not None:
        figures.append(f"Anterior {fmt.figure(event.previous, event.unit)}")
    if figures:
        lines.append(" · ".join(figures))
    if event.symbol:
        lines.append(f"Ativo: {event.symbol}")
    elif event.affects:
        lines.append("Afeta " + " · ".join(event.affects))
    return "\n".join(lines)


def event_alerts(
    events: Collection[EventInfo], now: datetime, already: Collection[str]
) -> tuple[list[Alert], list[str]]:
    """Alerts due now, and keys to mark as sent without sending.

    An event first seen 30 minutes out has crossed both thresholds; it gets the
    1 hour message only, and the 24 hour key is marked so it does not follow.
    A date-only event gets one message, the day before or the day itself.
    """
    alerts: list[Alert] = []
    superseded: list[str] = []
    for event in sorted(events, key=lambda e: e.starts_at):
        if event.has_time:
            remaining = event.starts_at - now
            if remaining <= timedelta(0):
                continue
            crossed = [(t, label) for t, label in EVENT_THRESHOLDS if remaining <= t]
            if not crossed:
                continue
            _, label = crossed[-1]  # the tightest threshold crossed
            keys = {lab: f"event:{event.identity_key}:{lab}" for _, lab in crossed}
            for lab, key in keys.items():
                if lab != label and key not in already:
                    superseded.append(key)
            key = keys[label]
            if key in already:
                continue
            headline = "em 24 h" if label == "24h" else "em 1 h"
            alerts.append(Alert(key, "event", _event_text(event, headline)))
        else:
            days = (
                event.starts_at.astimezone(UTC).date() - now.astimezone(UTC).date()
            ).days
            if days < 0 or days > 1:
                continue
            key = f"event:{event.identity_key}:day"
            if key in already:
                continue
            headline = "hoje" if days == 0 else "amanhã"
            alerts.append(Alert(key, "event", _event_text(event, headline)))
    return alerts, superseded


# --- 2. volume ---------------------------------------------------------------------


def volume_alerts(
    assets: Collection[VolumeInfo], already: Collection[str]
) -> list[Alert]:
    """One alert per watchlist asset and trading day with volume above 3x."""
    out: list[Alert] = []
    for asset in sorted(assets, key=lambda a: a.rvol, reverse=True):
        if asset.rvol < VOLUME_THRESHOLD:
            continue
        key = f"volume:{asset.instrument_id}:{asset.last_day.isoformat()}"
        if key in already:
            continue
        move = f" ({fmt.percent(asset.change, 1)})" if asset.change is not None else ""
        out.append(
            Alert(
                key,
                "volume",
                f"VOLUME {fmt.number(asset.rvol, 1)}x a média de 20 dias: "
                f"{asset.label}{move}\nFechamento de {fmt.short_date(asset.last_day)}",
            )
        )
    return out


# --- 3. review dates -----------------------------------------------------------------


def review_alerts(
    positions: Collection[ReviewInfo], today: date, already: Collection[str]
) -> list[Alert]:
    """One alert per position whose review date has passed."""
    out: list[Alert] = []
    for position in sorted(positions, key=lambda p: p.review_by):
        if position.review_by >= today:
            continue
        key = f"review:{position.position_key}:{position.review_by.isoformat()}"
        if key in already:
            continue
        overdue = (today - position.review_by).days
        out.append(
            Alert(
                key,
                "review",
                f"REVISÃO VENCIDA: {position.label}\n"
                f"Data de revisão {fmt.short_date(position.review_by)}, "
                f"{overdue} dia(s) atrás. Escreva a revisão: manter, reduzir ou sair.",
            )
        )
    return out


# --- 4. broken sources -----------------------------------------------------------


def source_alerts(
    sources: Collection[SourceInfo], now: datetime, already: Collection[str]
) -> list[Alert]:
    """One alert per source broken for more than 6 hours, once per outage.

    The key holds the last good run, so a source that recovers and breaks again
    is a new outage and sends a new message.
    """
    out: list[Alert] = []
    for info in sorted(sources, key=lambda s: s.source):
        if info.last_success_at is not None:
            down_for = now - info.last_success_at
            if down_for < SOURCE_DOWN_AFTER:
                continue
            last = info.last_success_at
            since = f"última coleta {fmt.clock(last)} ({fmt.age(last, now)})"
            marker = info.last_success_at.isoformat()
        else:
            since = "nunca coletou"
            marker = "never"
        key = f"source:{info.source}:{marker}"
        if key in already:
            continue
        error = f"\n{info.error[:140]}" if info.error else ""
        out.append(
            Alert(
                key,
                "source",
                f"FONTE PARADA: {info.label} ({info.source})\n"
                f"{since}, {info.failures} falha(s) seguida(s).{error}",
            )
        )
        if len(out) >= MAX_SOURCE_ALERTS:
            break
    return out
