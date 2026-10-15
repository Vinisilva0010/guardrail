"""Source health for the page header.

A dead collector must never look like a quiet market, so every page shows one
dot per source group, read from source_health. A source nobody collects yet
(missing key, paid, no endpoint) is listed too, as "no collector", so its absence
is visible instead of being mistaken for "nothing happening".
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.config import get_settings
from guardrail.db.models import SourceHealth
from guardrail.web.format import age, clock


class State(enum.StrEnum):
    """Traffic light of a source."""

    OK = "ok"
    LATE = "late"
    FAILED = "failed"
    ABSENT = "absent"

    @property
    def label(self) -> str:
        """The state in Portuguese, for sentences."""
        return {
            "ok": "em dia",
            "late": "atrasada",
            "failed": "com falha",
            "absent": "sem coletor",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Group:
    """A family of sources shown as one dot."""

    key: str
    label: str
    prefixes: tuple[str, ...]
    max_age: timedelta


# Maximum age before a source counts as late. Twice its planned cadence, so one
# missed run is amber and several are red.
GROUPS: Final = (
    Group("binance.klines", "Velas cripto", ("binance.klines.",), timedelta(hours=2)),
    Group("binance.funding", "Funding", ("binance.funding.",), timedelta(hours=18)),
    Group(
        "binance.vision", "Posicionamento", ("binance.vision.",), timedelta(hours=36)
    ),
    Group("alpaca.bars", "Barras EUA", ("alpaca.bars",), timedelta(hours=36)),
    Group("alpaca.news", "Notícia EUA", ("alpaca.news",), timedelta(hours=2)),
    Group(
        "alpaca.actions",
        "Eventos societários",
        ("alpaca.corporate_actions",),
        timedelta(hours=36),
    ),
    Group("alpaca.calendar", "Feriados EUA", ("alpaca.calendar",), timedelta(hours=36)),
    Group("brapi", "B3", ("brapi.",), timedelta(days=4)),
    Group("rss", "Notícia RSS", ("rss.",), timedelta(hours=2)),
    Group("forexfactory", "Calendário macro", ("forexfactory.",), timedelta(hours=12)),
    Group("bcb.sgs", "BCB séries", ("bcb.sgs",), timedelta(hours=36)),
    Group("bcb.focus", "BCB Focus", ("bcb.focus",), timedelta(hours=36)),
    Group(
        "calendar.rules",
        "Vencimentos e feriados B3",
        ("calendar.rules",),
        timedelta(hours=36),
    ),
    Group(
        "binance.oi",
        "Open interest recente",
        ("binance.oi_recent.",),
        timedelta(hours=1),
    ),
    Group("analysis.themes", "Temas", ("analysis.themes",), timedelta(hours=2)),
    Group(
        "analysis.impact",
        "Estatística de eventos",
        ("analysis.event_impact",),
        timedelta(hours=36),
    ),
    Group("quality", "Auditoria de dados", ("quality.",), timedelta(hours=36)),
    Group("scheduler", "Agendador", ("scheduler.",), timedelta(minutes=5)),
    Group("alerts", "Alertas Telegram", ("alerts.",), timedelta(minutes=30)),
)

# Sources the plan wants that have no collector, with the reason. Shown in the
# header and used by the blocks that would have been fed by them.
NO_COLLECTOR: Final = {
    "Resultados de empresas (Alpha Vantage)": "chave ALPHAVANTAGE_API_KEY ausente",
    "Séries macro EUA (FRED)": "chave FRED_API_KEY ausente",
    "Desbloqueio de token (DefiLlama)": "fonte paga (HTTP 402)",
    "Datas do Copom (BCB)": "sem API acessível",
}

# Consecutive failures from which a source is red regardless of its age.
RED_FAILURES: Final = 3
# Age as a multiple of max_age from which a source is red.
RED_AGE_FACTOR: Final = 3


@dataclass(frozen=True, slots=True)
class Dot:
    """One header indicator."""

    label: str
    state: State
    detail: str

    @property
    def title(self) -> str:
        return f"{self.label}: {self.detail}"


def _worst(states: list[State]) -> State:
    order = (State.FAILED, State.LATE, State.OK)
    for candidate in order:
        if candidate in states:
            return candidate
    return State.OK


def classify(row: SourceHealth, max_age: timedelta, now: datetime) -> tuple[State, str]:
    """State and a one-line reason for a single source row."""
    if row.last_success_at is None:
        return State.FAILED, f"nunca coletou · {row.last_error or 'sem tentativa'}"
    old = now - row.last_success_at
    detail = (
        f"última coleta {clock(row.last_success_at)} ({age(row.last_success_at, now)})"
    )
    if row.consecutive_failures >= RED_FAILURES or old > max_age * RED_AGE_FACTOR:
        if row.last_error:
            detail += f" · {row.consecutive_failures} falhas · {row.last_error[:80]}"
        return State.FAILED, detail
    if row.consecutive_failures >= 1:
        detail += (
            f" · {row.consecutive_failures} falha(s) · {(row.last_error or '')[:80]}"
        )
        return State.LATE, detail
    if old > max_age:
        return State.LATE, detail
    return State.OK, detail


TELEGRAM_MISSING: Final = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ausentes"


def missing_collectors(telegram_configured: bool) -> dict[str, str]:
    """Everything the plan wants that nothing collects or sends, with the reason."""
    missing = dict(NO_COLLECTOR)
    if not telegram_configured:
        missing["Alertas Telegram"] = TELEGRAM_MISSING
    return missing


def telegram_is_configured() -> bool:
    """Whether the alert bot can send. Read from the settings, never from the DB."""
    settings = get_settings()
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def dots(session: Session, now: datetime) -> list[Dot]:
    """One dot per source group, then one per source with no collector."""
    rows = list(session.execute(select(SourceHealth)).scalars())
    configured = telegram_is_configured()
    out: list[Dot] = []
    for group in GROUPS:
        if group.key == "alerts" and not configured:
            continue  # listed below as "no collector", with the reason
        members = [r for r in rows if r.source.startswith(group.prefixes)]
        if not members:
            out.append(Dot(group.label, State.ABSENT, "nenhuma coleta registrada"))
            continue
        classified = [(classify(r, group.max_age, now), r) for r in members]
        state = _worst([s for (s, _), _ in classified])
        worst_detail = next(
            (d for (s, d), r in classified if s is state), classified[0][0][1]
        )
        out.append(Dot(group.label, state, worst_detail))
    out.extend(
        Dot(label, State.ABSENT, f"sem coletor: {reason}")
        for label, reason in missing_collectors(configured).items()
    )
    return out


def group_state(session: Session, key: str, now: datetime) -> tuple[State, str]:
    """State of one named group, for blocks that depend on it."""
    group = next(g for g in GROUPS if g.key == key)
    rows = [
        r
        for r in session.execute(select(SourceHealth)).scalars()
        if r.source.startswith(group.prefixes)
    ]
    if not rows:
        return State.ABSENT, "nenhuma coleta registrada"
    classified = [classify(r, group.max_age, now) for r in rows]
    state = _worst([s for s, _ in classified])
    return state, next(d for s, d in classified if s is state)
