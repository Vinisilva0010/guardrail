"""Tests for the four alert rules. Pure: fixed time, no database, no network.

15 Oct 2026 is a Thursday. NOW is 12:00 UTC.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from guardrail.alerts.rules import (
    MAX_SOURCE_ALERTS,
    EventInfo,
    ReviewInfo,
    SourceInfo,
    VolumeInfo,
    event_alerts,
    review_alerts,
    source_alerts,
    volume_alerts,
)

D = Decimal
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
K = "a" * 32


def _event(
    starts_in: timedelta | None = None,
    *,
    key: str = K,
    has_time: bool = True,
    starts_at: datetime | None = None,
    title: str = "Core CPI m/m",
) -> EventInfo:
    return EventInfo(
        identity_key=key,
        title=title,
        kind="inflation_print",
        starts_at=starts_at or (NOW + (starts_in or timedelta(0))),
        has_time=has_time,
        expected=D("0.2"),
        previous=D("0.3"),
        unit="%",
        symbol=None,
        affects=("BTC", "ETH", "QQQ"),
    )


# --- 1. events -----------------------------------------------------------------


def test_event_inside_24_hours_alerts_once_with_the_figures() -> None:
    alerts, superseded = event_alerts([_event(timedelta(hours=23))], NOW, set())

    (alert,) = alerts
    assert alert.key == f"event:{K}:24h" and alert.kind == "event"
    assert "ALTO IMPACTO em 24 h: Core CPI m/m" in alert.text
    assert "Esperado 0,2% · Anterior 0,3%" in alert.text
    assert "Afeta BTC · ETH · QQQ" in alert.text
    assert superseded == []


def test_event_beyond_24_hours_is_not_yet_due() -> None:
    assert event_alerts([_event(timedelta(hours=24, minutes=1))], NOW, set()) == (
        [],
        [],
    )


def test_the_thresholds_are_inclusive() -> None:
    day, _ = event_alerts([_event(timedelta(hours=24))], NOW, set())
    hour, _ = event_alerts([_event(timedelta(hours=1))], NOW, set())

    assert [a.key for a in day] == [f"event:{K}:24h"]
    assert [a.key for a in hour] == [f"event:{K}:1h"]


def test_one_hour_alert_follows_the_day_alert() -> None:
    sent = {f"event:{K}:24h"}

    alerts, superseded = event_alerts([_event(timedelta(minutes=50))], NOW, sent)

    assert [a.key for a in alerts] == [f"event:{K}:1h"]
    assert "em 1 h" in alerts[0].text
    assert superseded == []


def test_an_event_first_seen_inside_the_hour_gets_one_message_not_two() -> None:
    alerts, superseded = event_alerts([_event(timedelta(minutes=30))], NOW, set())

    assert [a.key for a in alerts] == [f"event:{K}:1h"]
    assert superseded == [f"event:{K}:24h"]


def test_an_alert_already_sent_is_not_sent_again() -> None:
    sent = {f"event:{K}:24h"}

    assert event_alerts([_event(timedelta(hours=10))], NOW, sent) == ([], [])


def test_an_event_that_already_happened_sends_nothing() -> None:
    assert event_alerts([_event(timedelta(minutes=-1))], NOW, set()) == ([], [])


def test_a_date_only_event_gets_one_message_the_day_before_or_the_day_itself() -> None:
    tomorrow = _event(has_time=False, starts_at=datetime(2026, 10, 16, tzinfo=UTC))
    today = _event(
        key="b" * 32, has_time=False, starts_at=datetime(2026, 10, 15, tzinfo=UTC)
    )
    later = _event(
        key="c" * 32, has_time=False, starts_at=datetime(2026, 10, 17, tzinfo=UTC)
    )

    alerts, _ = event_alerts([tomorrow, today, later], NOW, set())

    assert [a.key for a in alerts] == [f"event:{'b' * 32}:day", f"event:{K}:day"]
    assert (
        "ALTO IMPACTO hoje" in alerts[0].text
        and "ALTO IMPACTO amanhã" in alerts[1].text
    )
    assert event_alerts([tomorrow], NOW, {f"event:{K}:day"}) == ([], [])


def test_a_date_only_event_is_not_shifted_into_the_previous_day() -> None:
    event = _event(has_time=False, starts_at=datetime(2026, 10, 28, tzinfo=UTC))

    text = event_alerts([event], datetime(2026, 10, 27, 12, tzinfo=UTC), set())[0][
        0
    ].text

    assert "quarta 28/10" in text


def test_an_asset_event_names_the_asset_instead_of_the_reference_list() -> None:
    event = EventInfo(
        K,
        "AAPL results",
        "earnings",
        NOW + timedelta(hours=5),
        True,
        None,
        None,
        None,
        "AAPL",
        (),
    )

    (alert,), _ = event_alerts([event], NOW, set())

    assert "Ativo: AAPL" in alert.text and "Afeta" not in alert.text


def test_a_hostile_title_is_carried_as_plain_text() -> None:
    """No parse_mode is used, so markup characters have no meaning."""
    title = "<b>x</b> *y* [z](http://evil.example) `code`"

    (alert,), _ = event_alerts([_event(timedelta(hours=2), title=title)], NOW, set())

    assert title in alert.text


# --- 2. volume -----------------------------------------------------------------


def _vol(
    rvol: str, instrument_id: int = 1, day: date = date(2026, 10, 14)
) -> VolumeInfo:
    return VolumeInfo(instrument_id, "AAPL", day, D(rvol), D("0.042"))


def test_volume_at_or_above_three_times_alerts() -> None:
    alerts = volume_alerts([_vol("3.0"), _vol("2.99", 2)], set())

    (alert,) = alerts
    assert alert.key == "volume:1:2026-10-14"
    assert "VOLUME 3,0x a média de 20 dias: AAPL (+4,2%)" in alert.text
    assert "Fechamento de 14/10" in alert.text


def test_volume_alert_is_once_per_asset_and_day() -> None:
    sent = {"volume:1:2026-10-14"}

    assert volume_alerts([_vol("5")], sent) == []
    assert len(volume_alerts([_vol("5", day=date(2026, 10, 15))], sent)) == 1


def test_strongest_volume_goes_first() -> None:
    alerts = volume_alerts([_vol("3.5", 1), _vol("9.1", 2), _vol("4.2", 3)], set())

    assert [a.key.split(":")[1] for a in alerts] == ["2", "3", "1"]


# --- 3. review dates -----------------------------------------------------------


def test_a_review_date_in_the_past_alerts() -> None:
    position = ReviewInfo("7", "BTC long", date(2026, 10, 10))

    (alert,) = review_alerts([position], date(2026, 10, 15), set())

    assert alert.key == "review:7:2026-10-10"
    assert "REVISÃO VENCIDA: BTC long" in alert.text
    assert "5 dia(s) atrás" in alert.text


def test_a_review_due_today_or_later_is_not_overdue() -> None:
    today, later = (
        ReviewInfo("1", "a", date(2026, 10, 15)),
        ReviewInfo("2", "b", date(2026, 10, 20)),
    )

    assert review_alerts([today, later], date(2026, 10, 15), set()) == []


def test_an_overdue_review_alerts_once_per_review_date() -> None:
    position = ReviewInfo("7", "BTC long", date(2026, 10, 10))
    sent = {"review:7:2026-10-10"}

    assert review_alerts([position], date(2026, 10, 15), sent) == []
    moved = ReviewInfo("7", "BTC long", date(2026, 10, 12))
    assert len(review_alerts([moved], date(2026, 10, 15), sent)) == 1


# --- 4. broken sources ---------------------------------------------------------


def _src(
    hours_ago: float | None, source: str = "forexfactory.calendar", failures: int = 4
) -> SourceInfo:
    last = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
    return SourceInfo(
        source, "Calendário macro", last, failures, "HTTPStatusError: 429"
    )


def test_a_source_down_for_more_than_six_hours_alerts() -> None:
    (alert,) = source_alerts([_src(7)], NOW, set())

    assert alert.key.startswith("source:forexfactory.calendar:")
    assert "FONTE PARADA: Calendário macro (forexfactory.calendar)" in alert.text
    assert (
        "4 falha(s) seguida(s)" in alert.text and "HTTPStatusError: 429" in alert.text
    )


def test_six_hours_is_the_line() -> None:
    assert source_alerts([_src(5.99)], NOW, set()) == []
    assert len(source_alerts([_src(6.0)], NOW, set())) == 1


def test_a_source_that_never_worked_alerts() -> None:
    (alert,) = source_alerts([_src(None)], NOW, set())

    assert alert.key == "source:forexfactory.calendar:never"
    assert "nunca coletou" in alert.text


def test_one_message_per_outage() -> None:
    info = _src(7)
    (first,) = source_alerts([info], NOW, set())

    # The same outage seen an hour later has the same last good run: same key.
    assert source_alerts([info], NOW + timedelta(hours=1), {first.key}) == []


def test_a_source_that_recovers_and_breaks_again_is_a_new_outage() -> None:
    info = _src(7)
    (first,) = source_alerts([info], NOW, set())

    recovered = NOW + timedelta(hours=2)
    broke_again = SourceInfo(info.source, info.label, recovered, 3, "boom")
    (second,) = source_alerts(
        [broke_again], recovered + timedelta(hours=7), {first.key}
    )

    assert second.key != first.key


def test_a_burst_of_broken_sources_is_capped_per_pass() -> None:
    many = [_src(9, source=f"src.{i:02d}") for i in range(MAX_SOURCE_ALERTS + 5)]

    alerts = source_alerts(many, NOW, set())

    assert len(alerts) == MAX_SOURCE_ALERTS
    # The rest are not lost: once these are sent, the next pass returns the others.
    sent = {a.key for a in alerts}
    assert len(source_alerts(many, NOW, sent)) == 5
