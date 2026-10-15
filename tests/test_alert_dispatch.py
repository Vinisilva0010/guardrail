"""Tests for the alert pass: what is collected, what is sent, what is remembered.

The Telegram API is replaced by a recording function, or by respx where the real
client is the point. Each of the four alerts fires in a test.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from guardrail.alerts import dispatch
from guardrail.alerts.dispatch import (
    DispatchResult,
    NoPositionTables,
    ReviewSource,
    collect,
)
from guardrail.alerts.rules import ReviewInfo
from guardrail.alerts.telegram import API, TelegramClient, TelegramError
from guardrail.db.models import (
    AlertSent,
    AssetClass,
    Candle,
    EventImpact,
    EventKind,
    EventScope,
    Instrument,
    ScheduledEvent,
    SourceHealth,
    Watchlist,
)

D = Decimal
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)  # Thursday
LAST_BAR = date(2026, 10, 14)


class Recorder:
    """A stand-in for Telegram that keeps what it was asked to send."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.texts: list[str] = []
        self.fail_on = fail_on

    async def __call__(self, text: str) -> None:
        if self.fail_on is not None and len(self.texts) + 1 == self.fail_on:
            raise TelegramError("telegram refused the message: HTTP 500")
        self.texts.append(text)


class OneOverdue:
    def overdue(self, session: Session, today: datetime) -> list[ReviewInfo] | None:
        return [ReviewInfo("7", "BTC long", date(2026, 10, 10))]


def _blank(session: Session) -> None:
    session.execute(
        text("TRUNCATE instrument, source_health, scheduled_event, alert_sent CASCADE")
    )


def _instrument(session: Session, symbol: str, venue: str) -> Instrument:
    instrument = Instrument(symbol=symbol, venue=venue, asset_class=AssetClass.EQUITY)
    session.add(instrument)
    session.flush()
    return instrument


def _bars(
    session: Session,
    instrument: Instrument,
    last_volume: int,
    last_day: date = LAST_BAR,
) -> None:
    for back in range(40):
        day = last_day - timedelta(days=back)
        price = D("104") if back == 0 else D("100")
        session.add(
            Candle(
                instrument_id=instrument.id,
                timeframe="1d",
                ts=datetime(day.year, day.month, day.day, tzinfo=UTC),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=D(last_volume) if back == 0 else D(1_000_000),
            )
        )
    session.flush()


def _event(
    key: str,
    *,
    starts_in: timedelta = timedelta(hours=10),
    scope: EventScope = EventScope.US,
    impact: EventImpact = EventImpact.HIGH,
    kind: EventKind = EventKind.INFLATION_PRINT,
    instrument_id: int | None = None,
) -> ScheduledEvent:
    return ScheduledEvent(
        source="test-source",
        kind=kind,
        scope=scope,
        instrument_id=instrument_id,
        title=f"event {key}",
        starts_at=NOW + starts_in,
        has_time=True,
        impact=impact,
        expected=D("0.2"),
        previous=D("0.3"),
        unit="%",
        payload={},
        identity_key=key * 32,
    )


def _broken_source(
    session: Session, name: str = "forexfactory.calendar", hours: int = 9
) -> None:
    session.add(
        SourceHealth(
            source=name,
            last_success_at=NOW - timedelta(hours=hours),
            consecutive_failures=4,
            last_error="HTTPStatusError: 429",
        )
    )


async def _pass(
    session: Session,
    send: Recorder,
    reviews: ReviewSource | None = None,
    dry: bool = False,
) -> DispatchResult:
    return await dispatch.dispatch(session, NOW, send, reviews, dry_run=dry)


def _sent_rows(session: Session) -> int:
    return session.execute(select(func.count()).select_from(AlertSent)).scalar_one()


# --- the four alerts -----------------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_alerts_fire(session: Session) -> None:
    _blank(session)
    aapl = _instrument(session, "AAPL", "alpaca")
    _bars(session, aapl, last_volume=4_000_000)
    session.add(Watchlist(instrument_id=aapl.id))
    session.add(_event("a"))
    _broken_source(session)
    session.flush()
    send = Recorder()

    result = await _pass(session, send, OneOverdue())

    kinds = {t.split(":")[0].split(" ")[0] for t in send.texts}
    assert kinds == {"ALTO", "FONTE", "REVISÃO", "VOLUME"}
    assert result.sent == 4 and result.failed is None and result.reviews_monitored


@pytest.mark.asyncio
async def test_an_event_alert_names_the_figures_and_the_reference_assets(
    session: Session,
) -> None:
    _blank(session)
    btc = _instrument(session, "BTCUSDT", "binance")
    session.add(_event("a", starts_in=timedelta(hours=10)))
    session.flush()
    send = Recorder()

    await _pass(session, send)

    assert btc.id  # the reference table seeds binance:BTCUSDT
    (text_,) = send.texts
    assert "ALTO IMPACTO em 24 h: event " in text_
    assert "Esperado 0,2% · Anterior 0,3%" in text_ and "Afeta BTC" in text_


# --- what counts as a candidate ------------------------------------------------


@pytest.mark.asyncio
async def test_only_market_wide_high_impact_events_in_our_markets_alert(
    session: Session,
) -> None:
    _blank(session)
    session.add_all(
        [
            _event("1"),  # counts
            _event("2", impact=EventImpact.MEDIUM),
            _event("3", scope=EventScope.GLOBAL),
            _event("4", kind=EventKind.HOLIDAY),
            _event("5", starts_in=timedelta(hours=30)),
            _event("6", starts_in=timedelta(hours=-2)),
        ]
    )
    session.flush()
    send = Recorder()

    await _pass(session, send)

    assert len(send.texts) == 1


@pytest.mark.asyncio
async def test_events_alert_for_watched_assets_only(
    session: Session,
) -> None:
    _blank(session)
    watched = _instrument(session, "AAPL", "alpaca")
    other = _instrument(session, "MSFT", "alpaca")
    session.add(Watchlist(instrument_id=watched.id))
    session.add_all(
        [
            _event(
                "1",
                kind=EventKind.EARNINGS,
                instrument_id=watched.id,
                scope=EventScope.US,
            ),
            _event(
                "2",
                kind=EventKind.EARNINGS,
                instrument_id=other.id,
                scope=EventScope.US,
            ),
        ]
    )
    session.flush()
    send = Recorder()

    await _pass(session, send)

    assert len(send.texts) == 1 and "Ativo: AAPL" in send.texts[0]


@pytest.mark.asyncio
async def test_volume_alert_needs_the_watchlist_a_current_bar_and_three_times(
    session: Session,
) -> None:
    _blank(session)
    watched_hot = _instrument(session, "HOT", "alpaca")
    watched_calm = _instrument(session, "CALM", "alpaca")
    unwatched = _instrument(session, "OTHER", "alpaca")
    stale = _instrument(session, "STALE", "alpaca")
    _bars(session, watched_hot, 4_000_000)
    _bars(session, watched_calm, 2_000_000)
    _bars(session, unwatched, 9_000_000)
    _bars(session, stale, 9_000_000, last_day=LAST_BAR - timedelta(days=5))
    session.add_all(
        [Watchlist(instrument_id=i.id) for i in (watched_hot, watched_calm, stale)]
    )
    session.flush()
    send = Recorder()

    await _pass(session, send)

    assert len(send.texts) == 1 and "HOT" in send.texts[0]


@pytest.mark.asyncio
async def test_source_alerts_skip_plumbing_fresh_and_barely_failing_sources(
    session: Session,
) -> None:
    _blank(session)
    _broken_source(session, "forexfactory.calendar")
    _broken_source(session, "job.macro_calendar")
    _broken_source(session, "scheduler.heartbeat")
    session.add(
        SourceHealth(
            source="bcb.sgs",
            last_success_at=NOW - timedelta(hours=1),
            consecutive_failures=0,
        )
    )
    session.add(
        SourceHealth(
            source="rss.coindesk",
            last_success_at=None,
            consecutive_failures=1,
            last_error="x",
        )
    )
    session.add(
        SourceHealth(
            source="rss.decrypt",
            last_success_at=None,
            consecutive_failures=3,
            last_error="y",
        )
    )
    session.flush()
    send = Recorder()

    await _pass(session, send)

    joined = "\n".join(send.texts)
    assert "forexfactory.calendar" in joined and "rss.decrypt" in joined
    assert "job.macro_calendar" not in joined and "scheduler.heartbeat" not in joined
    assert "bcb.sgs" not in joined and "rss.coindesk" not in joined


# --- remembering ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_pass_sends_nothing(session: Session) -> None:
    _blank(session)
    session.add(_event("a"))
    _broken_source(session)
    session.flush()

    first = await _pass(session, Recorder())
    second_send = Recorder()
    second = await _pass(session, second_send)

    assert first.sent == 2 and second.sent == 0 and second_send.texts == []
    assert _sent_rows(session) == 2


@pytest.mark.asyncio
async def test_the_day_alert_is_dropped_when_the_hour_alert_was_sent_first(
    session: Session,
) -> None:
    _blank(session)
    session.add(_event("a", starts_in=timedelta(minutes=30)))
    session.flush()
    send = Recorder()

    await _pass(session, send)
    later = Recorder()
    await _pass(session, later)

    assert len(send.texts) == 1 and "em 1 h" in send.texts[0]
    assert later.texts == []
    keys = set(session.execute(select(AlertSent.dedupe_key)).scalars())
    assert keys == {f"event:{'a' * 32}:1h", f"event:{'a' * 32}:24h"}


@pytest.mark.asyncio
async def test_a_failed_delivery_stops_the_pass_and_only_the_undelivered_are_retried(
    session: Session,
) -> None:
    _blank(session)
    session.add_all(
        [
            _event("a"),
            _event("b", starts_in=timedelta(hours=11)),
            _event("c", starts_in=timedelta(hours=12)),
        ]
    )
    session.flush()
    flaky = Recorder(fail_on=2)

    first = await _pass(session, flaky)

    assert first.sent == 1 and first.failed is not None and len(flaky.texts) == 1
    assert _sent_rows(session) == 1  # only the delivered one is remembered

    healthy = Recorder()
    second = await _pass(session, healthy)

    assert second.sent == 2 and len(healthy.texts) == 2
    assert _sent_rows(session) == 3


@pytest.mark.asyncio
async def test_a_dry_run_sends_and_remembers_nothing(session: Session) -> None:
    _blank(session)
    session.add(_event("a"))
    session.flush()
    send = Recorder()

    result = await _pass(session, send, dry=True)

    assert result.candidates == 1 and result.sent == 0 and send.texts == []
    assert _sent_rows(session) == 0
    assert len(result.texts) == 1


@pytest.mark.asyncio
async def test_unmonitored_reviews_are_reported_not_silently_empty(
    session: Session,
) -> None:
    _blank(session)

    without = await _pass(session, Recorder(), NoPositionTables())
    with_source = await _pass(session, Recorder(), OneOverdue())

    assert without.reviews_monitored is False
    assert with_source.reviews_monitored is True


# --- configuration and the real client -----------------------------------------


@pytest.mark.asyncio
async def test_without_a_token_the_pass_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch, "configured", lambda: False)

    result = await dispatch.run()

    assert result.configured is False and result.sent == 0


@pytest.mark.asyncio
@respx.mock
async def test_end_to_end_through_the_client_sends_one_request_per_alert(
    session: Session,
) -> None:
    _blank(session)
    session.add_all([_event("a"), _event("b", starts_in=timedelta(hours=12))])
    session.flush()
    token = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-000000"
    route = respx.post(f"{API}/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with httpx.AsyncClient() as http:
        client = TelegramClient(http, SecretStr(token), "42")
        result = await dispatch.dispatch(session, NOW, client.send)

    assert result.sent == 2 and route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_delivery_keeps_the_rest_for_the_next_pass(
    session: Session,
) -> None:
    _blank(session)
    session.add_all([_event("a"), _event("b", starts_in=timedelta(hours=12))])
    session.flush()
    token = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-000000"
    respx.post(f"{API}/bot{token}/sendMessage").mock(
        return_value=httpx.Response(
            429, json={"ok": False, "parameters": {"retry_after": 30}}
        )
    )

    async with httpx.AsyncClient() as http:
        client = TelegramClient(http, SecretStr(token), "42")
        result = await dispatch.dispatch(session, NOW, client.send)

    assert result.sent == 0 and result.failed is not None
    assert token not in result.failed
    assert _sent_rows(session) == 0


def test_collect_returns_the_alerts_without_touching_the_database(
    session: Session,
) -> None:
    _blank(session)
    session.add(_event("a"))
    session.flush()

    alerts, superseded, monitored = collect(session, NOW, NoPositionTables())

    assert len(alerts) == 1 and superseded == [] and monitored is False
    assert _sent_rows(session) == 0
