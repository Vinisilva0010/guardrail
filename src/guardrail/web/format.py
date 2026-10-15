"""Display formatting: Brazilian Portuguese numbers, dates and ages.

Everything here returns plain text. Escaping is the template engine's job and
no function in this module returns markup.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

SAO_PAULO: Final = ZoneInfo("America/Sao_Paulo")
MINUS: Final = chr(0x2212)  # a true minus sign, not a hyphen

WEEKDAYS: Final = (
    "segunda",
    "terça",
    "quarta",
    "quinta",
    "sexta",
    "sábado",
    "domingo",
)


def _group(digits: str) -> str:
    out: list[str] = []
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return ".".join(out)


def number(value: Decimal | None, places: int = 2, sign: bool = False) -> str:
    """12345.6 -> '12.345,60'. A true minus sign, never a hyphen."""
    if value is None:
        return "—"
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum)
    negative = rounded < 0
    whole, _, frac = f"{abs(rounded):f}".partition(".")
    text = _group(whole) + (f",{frac}" if places else "")
    if negative:
        return MINUS + text
    return ("+" + text) if sign and rounded > 0 else text


def percent(fraction: Decimal | None, places: int = 1, sign: bool = True) -> str:
    """0.0423 -> '+4,2%'."""
    if fraction is None:
        return "—"
    return number(fraction * 100, places, sign) + "%"


def money(value: Decimal | None, currency: str, places: int = 2) -> str:
    """Price with currency prefix: 'US$ 122,40'."""
    if value is None:
        return "—"
    return f"{currency} {number(value, places)}"


def compact_money(value: Decimal | None, currency: str) -> str:
    """Large turnover: 'US$ 45 M', 'R$ 1,2 bi'."""
    if value is None:
        return "—"
    if value >= 1_000_000_000:
        return f"{currency} {number(value / 1_000_000_000, 1)} bi"
    return f"{currency} {number(value / 1_000_000, 0)} M"


def arrow(value: Decimal | None) -> str:
    """Direction as a symbol, so color is never the only carrier."""
    if value is None or value == 0:
        return "■"
    return "▲" if value > 0 else "▼"


def direction(value: Decimal | None) -> str:
    """CSS class for the sign of a change."""
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def local(moment: datetime) -> datetime:
    """A stored instant in the operator's time zone."""
    return moment.astimezone(SAO_PAULO)


def day_label(day: date) -> str:
    """'quarta 28/10'."""
    return f"{WEEKDAYS[day.weekday()]} {day:%d/%m}"


def event_when(starts_at: datetime, has_time: bool) -> str:
    """'quarta 28/10 · 15:00', or the date alone when the source gave no hour.

    A date-only event is stored as midnight UTC and must not be shifted into the
    previous day by the time zone.
    """
    if not has_time:
        return day_label(starts_at.astimezone(UTC).date())
    when = local(starts_at)
    return f"{day_label(when.date())} · {when:%H:%M}"


def countdown(starts_at: datetime, has_time: bool, now: datetime) -> str:
    """'em 3 dias', 'em 2 h 15 min', 'hoje', 'passou'."""
    if not has_time:
        days = (starts_at.astimezone(UTC).date() - now.astimezone(UTC).date()).days
        if days < 0:
            return "passou"
        if days == 0:
            return "hoje"
        return "amanhã" if days == 1 else f"em {days} dias"
    delta = starts_at - now
    if delta < timedelta(0):
        return "passou"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"em {max(minutes, 1)} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"em {hours} h {rest:02d} min"
    days = hours // 24
    return "amanhã" if days == 1 else f"em {days} dias"


def age(moment: datetime | None, now: datetime) -> str:
    """'há 2 h', 'há 3 dias'; 'nunca' when there is no moment."""
    if moment is None:
        return "nunca"
    delta = now - moment
    if delta < timedelta(0):
        return "agora"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "agora"
    if minutes < 60:
        return f"há {minutes} min"
    if minutes < 24 * 60:
        return f"há {minutes // 60} h"
    return f"há {minutes // (24 * 60)} dias"


def clock(moment: datetime | None) -> str:
    """'14:32' in the operator's zone, '—' when unknown."""
    return "—" if moment is None else f"{local(moment):%H:%M}"


def short_date(day: date) -> str:
    """'13/10'."""
    return f"{day:%d/%m}"


KIND_LABELS: Final = {
    "rate_decision": "Decisão de juros",
    "inflation_print": "Inflação",
    "jobs_print": "Emprego",
    "gdp_print": "PIB",
    "activity_print": "Atividade",
    "earnings": "Resultado",
    "token_unlock": "Desbloqueio",
    "options_expiry": "Vencimento de opções",
    "futures_expiry": "Vencimento de futuros",
    "lockup_expiry": "Fim de lock-up",
    "dividend_ex": "Data ex-dividendo",
    "auction": "Leilão",
    "central_bank_speech": "Fala de banco central",
    "holiday": "Feriado",
    "other": "Evento",
}


def kind_label(kind: str) -> str:
    """Portuguese name of an event kind."""
    return KIND_LABELS.get(kind, "Evento")


def figure(value: Decimal | None, unit: str | None) -> str:
    """A published figure as the source wrote it: '0,2%', '197K', '-3,2M'."""
    if value is None:
        return "—"
    plain = format(value.normalize(), "f")
    text = plain.replace(".", ",").replace("-", MINUS)
    return text + (unit or "")


def safe_href(url: str | None) -> str | None:
    """A link only when it is plain http or https. Anything else is not a link."""
    if not url:
        return None
    lowered = url.strip().lower()
    return url.strip() if lowered.startswith(("http://", "https://")) else None
