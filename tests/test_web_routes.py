"""Tests for the screen: every route, with a full and with an empty database.

The empty database is the state in which the screen lies most easily, so it is a
first-class case: no route may break and no block may render empty without
saying why.

Hostile content (headline, symbol, event title) is stored through the real
tables and must come out as text.
"""

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from guardrail.db.models import (
    AssetClass,
    Candle,
    Catalyst,
    DerivativeStat,
    EventImpact,
    EventImpactStat,
    EventKind,
    EventScope,
    Instrument,
    NewsTheme,
    NewsThemeAsset,
    NewsThemeItem,
    ScheduledEvent,
    SourceHealth,
)
from guardrail.web import app as web
from guardrail.web import health
from guardrail.web.app import app, get_session

D = Decimal
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)  # Thursday
LAST_BAR = date(2026, 10, 14)
HOSTILE = "<script>alert(1)</script> {{ 7*7 }} '; DROP TABLE catalyst;--"
STATIC = Path(web.__file__).parent / "static"
TEMPLATES = Path(web.__file__).parent / "templates"

ROUTES = ["/", "/cripto", "/eua", "/brasil", "/macro", "/mundo", "/agenda", "/radar"]


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(web, "utcnow", lambda: NOW)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _blank(session: Session) -> None:
    """Empty every table the screen reads. TRUNCATE is transactional in
    PostgreSQL, so the fixture's rollback restores the real rows."""
    session.execute(
        text(
            "TRUNCATE instrument, source_health, expectation, macro_point, "
            "macro_series, news_theme, scheduled_event CASCADE"
        )
    )


def _instrument(
    session: Session, symbol: str, venue: str, cls: AssetClass
) -> Instrument:
    instrument = Instrument(symbol=symbol, venue=venue, asset_class=cls)
    session.add(instrument)
    session.flush()
    return instrument


def _bars(
    session: Session,
    instrument: Instrument,
    close: Decimal,
    volume: Decimal,
    last_volume: Decimal,
    last_day: date = LAST_BAR,
    days: int = 40,
) -> None:
    for back in range(days):
        day = last_day - timedelta(days=back)
        last = back == 0
        price = close * D("1.04") if last else close
        session.add(
            Candle(
                instrument_id=instrument.id,
                timeframe="1d",
                ts=datetime(day.year, day.month, day.day, tzinfo=UTC),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=last_volume if last else volume,
            )
        )
    session.flush()


def _event(
    key: str,
    title: str,
    kind: EventKind,
    starts_at: datetime,
    *,
    impact: EventImpact = EventImpact.HIGH,
    scope: EventScope = EventScope.US,
    instrument_id: int | None = None,
    has_time: bool = True,
    expected: Decimal | None = None,
    previous: Decimal | None = None,
) -> ScheduledEvent:
    return ScheduledEvent(
        source="test-source",
        kind=kind,
        scope=scope,
        instrument_id=instrument_id,
        title=title,
        starts_at=starts_at,
        has_time=has_time,
        impact=impact,
        expected=expected,
        previous=previous,
        unit="%" if expected is not None else None,
        payload={},
        identity_key=key * 32,
    )


FRESH_SOURCES = (
    "binance.klines.BTCUSDT.1d",
    "binance.funding.BTCUSDT",
    "binance.vision.metrics.BTCUSDT",
    "alpaca.bars",
    "alpaca.news",
    "alpaca.corporate_actions",
    "alpaca.calendar",
    "brapi.b3",
    "rss.coindesk",
    "forexfactory.calendar",
    "bcb.sgs",
    "bcb.focus",
    "calendar.rules",
)


@pytest.fixture
def full(session: Session) -> dict[str, Instrument]:
    """A small, fresh market: crypto, US and Brazil, with events, news and a theme."""
    _blank(session)
    session.add_all(
        [
            SourceHealth(
                source=s,
                last_success_at=NOW - timedelta(minutes=10),
                consecutive_failures=0,
            )
            for s in FRESH_SOURCES
        ]
    )
    btc = _instrument(session, "BTCUSDT", "binance", AssetClass.CRYPTO)
    aapl = _instrument(session, "AAPL", "alpaca", AssetClass.EQUITY)
    thin = _instrument(session, "THIN", "alpaca", AssetClass.EQUITY)
    old = _instrument(session, "OLDX", "alpaca", AssetClass.EQUITY)
    petr = _instrument(session, "PETR4", "b3", AssetClass.EQUITY)

    # Relative volume 4x for every one of them; only turnover and age differ.
    _bars(session, btc, D(80000), D(10), D(40))
    _bars(session, aapl, D(100), D(1_000_000), D(4_000_000))
    _bars(session, thin, D(10), D(1000), D(4000))
    _bars(
        session,
        old,
        D(100),
        D(1_000_000),
        D(4_000_000),
        last_day=LAST_BAR - timedelta(days=5),
    )
    _bars(session, petr, D(30), D(1_000_000), D(4_000_000))

    session.add_all(
        [
            DerivativeStat(
                instrument_id=btc.id,
                ts=NOW - timedelta(hours=1),
                funding_rate=D("0.0001"),
            ),
            DerivativeStat(
                instrument_id=btc.id,
                ts=NOW - timedelta(minutes=30),
                open_interest_value=D(1100),
            ),
            DerivativeStat(
                instrument_id=btc.id,
                ts=NOW - timedelta(hours=25),
                open_interest_value=D(1000),
            ),
            _event(
                "1",
                "CPI m/m",
                EventKind.INFLATION_PRINT,
                NOW + timedelta(hours=20),
                expected=D("0.3"),
                previous=D("0.2"),
            ),
            _event(
                "2",
                "AAPL results",
                EventKind.EARNINGS,
                datetime(2026, 10, 18, tzinfo=UTC),
                impact=EventImpact.MEDIUM,
                instrument_id=aapl.id,
                has_time=False,
            ),
            _event(
                "3",
                f"Speech {HOSTILE}",
                EventKind.CENTRAL_BANK_SPEECH,
                NOW + timedelta(hours=30),
            ),
            EventImpactStat(
                kind=EventKind.EARNINGS,
                instrument_id=aapl.id,
                horizon="d1",
                n=10,
                median_signed=D("-0.021"),
                median_abs=D("0.03"),
                p10=D("-0.09"),
                p90=D("0.062"),
                pct_positive=D("0.4"),
                window_start=date(2024, 1, 1),
                window_end=date(2026, 9, 1),
            ),
            EventImpactStat(
                kind=EventKind.INFLATION_PRINT,
                instrument_id=btc.id,
                horizon="d1",
                n=3,
                median_signed=D("0.01"),
                median_abs=D("0.02"),
                p10=D("-0.01"),
                p90=D("0.03"),
                pct_positive=D("0.6"),
                window_start=date(2025, 1, 1),
                window_end=date(2026, 9, 1),
            ),
        ]
    )
    session.flush()

    news = [
        Catalyst(
            instrument_id=aapl.id,
            ts=NOW - timedelta(hours=2),
            source="alpaca.news",
            headline=HOSTILE,
            url="javascript:alert(1)",
            dedupe_key="a" * 64,
        ),
        Catalyst(
            instrument_id=btc.id,
            ts=NOW - timedelta(hours=3),
            source="rss.coindesk",
            headline="Bitcoin rallies",
            url="https://example.com/a?b=1&c=2",
            dedupe_key="b" * 64,
        ),
    ]
    theme_items = [
        Catalyst(
            instrument_id=petr.id,
            ts=NOW - timedelta(hours=i + 1),
            source=src,
            headline=f"Tarifa de aço {i}",
            url=None,
            dedupe_key=f"{i}" * 64,
        )
        for i, src in enumerate(
            ["alpaca.news", "alpaca.news", "rss.infomoney", "rss.infomoney"]
        )
    ]
    session.add_all(news + theme_items)
    session.flush()
    theme = NewsTheme(
        label="tarifa · aço · china",
        first_seen_at=NOW - timedelta(hours=5),
        last_seen_at=NOW - timedelta(hours=1),
    )
    session.add(theme)
    session.flush()
    session.add_all(
        [NewsThemeItem(theme_id=theme.id, catalyst_id=c.id) for c in theme_items]
    )
    session.add(
        NewsThemeAsset(theme_id=theme.id, instrument_id=petr.id, move_pct=D("0.031"))
    )
    session.flush()
    return {"btc": btc, "aapl": aapl, "thin": thin, "old": old, "petr": petr}


def _sections(html: str) -> list[str]:
    return re.split(r"<h2>", html)[1:]


def _assert_no_silent_empty_block(html: str) -> None:
    """A section with no card and no notice is a void that looks like calm."""
    for section in _sections(html):
        if (
            "<article" in section
            or 'class="item"' in section
            or 'class="timeline"' in section
            # A chart container counts as content: the route refuses to render
            # one for a series it could not load, and prints a notice instead.
            or 'class="chart"' in section
        ):
            continue
        assert 'class="notice' in section, f"silent empty block: {section[:120]!r}"


# --- every route, both states ------------------------------------------------------


@pytest.mark.parametrize("path", ROUTES)
def test_route_answers_200_with_data(
    client: TestClient, full: dict[str, Instrument], path: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert "<title>" in response.text


@pytest.mark.parametrize("path", ROUTES)
def test_route_answers_200_on_an_empty_database(
    client: TestClient, session: Session, path: str
) -> None:
    _blank(session)

    response = client.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ROUTES)
def test_empty_database_never_renders_a_mute_block(
    client: TestClient, session: Session, path: str
) -> None:
    _blank(session)

    _assert_no_silent_empty_block(client.get(path).text)


@pytest.mark.parametrize("path", ROUTES)
def test_full_database_never_renders_a_mute_block(
    client: TestClient, full: dict[str, Instrument], path: str
) -> None:
    _assert_no_silent_empty_block(client.get(path).text)


def test_empty_database_header_still_shows_every_source_and_the_limits(
    client: TestClient, session: Session
) -> None:
    _blank(session)

    html = client.get("/").text

    assert html.count('class="dot ') == len(health.GROUPS) + len(health.NO_COLLECTOR)
    assert "limite 1,5% por posição" in html and "4% de risco aberto" in html
    assert "sem coletor" in html


def test_empty_database_says_the_calendar_is_missing_not_that_the_week_is_empty(
    client: TestClient, session: Session
) -> None:
    _blank(session)

    html = client.get("/").text

    assert "calendário macro" in html
    assert "sem dado" in html


def test_header_and_nav_are_on_every_page(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    for path in ROUTES:
        html = client.get(path).text
        assert html.count("<nav") == 1 and 'aria-current="page"' in html
        assert "B3 com pregão hoje" in html and "cripto 24h" in html


# --- content with data ---------------------------------------------------------------


def test_unusual_volume_card_for_a_liquid_fresh_stock(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/eua").text

    assert "AAPL" in html
    assert "volume 4,0x a média de 20 dias" in html
    assert "US$ 104,00" in html
    assert "▲ +4,0%" in html


def test_illiquid_and_stale_stocks_are_left_out(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/eua").text

    assert "THIN" not in html  # turnover about 10 thousand, floor is 20 million
    assert "OLDX" not in html  # last bar five days older than expected


def test_liquidity_floor_is_applied_per_market(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    assert (
        "PETR4" in client.get("/brasil").text
    )  # R$ 30 million against a R$ 5 million floor


def test_home_merges_markets_by_relative_volume(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/").text

    for symbol in ("BTC", "AAPL", "PETR4"):
        assert symbol in html


def test_crypto_card_shows_positioning_and_its_reading(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/cripto").text

    assert "Funding +0,010%" in html
    assert "OI 24h +10,0%" in html
    assert "Posições aumentando, comprados pagando" in html


def test_event_card_shows_consensus_previous_and_insufficient_history(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/").text

    assert "ALTO IMPACTO" in html
    assert "Esperado 0,3%" in html and "Anterior 0,2%" in html
    assert "histórico insuficiente (n=3)" in html
    assert "Afeta BTC" in html


def test_asset_card_prints_a_displayable_distribution(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/eua").text

    assert "mediana −2,1%" in html
    assert "−9,0% a +6,2%" in html
    assert "n=10" in html


def test_theme_card_appears_only_with_enough_items_and_outlets(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/mundo").text

    assert "tarifa · aço · china" in html
    assert "4 notícias · 2 veículos" in html
    assert "PETR4 +3,1%" in html


def test_a_theme_below_the_minimum_is_not_shown(
    client: TestClient, session: Session, full: dict[str, Instrument]
) -> None:
    session.execute(
        text(
            "DELETE FROM news_theme_item WHERE catalyst_id = "
            "(SELECT min(catalyst_id) FROM news_theme_item)"
        )
    )

    html = client.get("/mundo").text

    assert "tarifa · aço · china" not in html
    assert "nenhum tema com" in html


def test_stale_bars_are_labelled_and_leave_the_movers_block(
    client: TestClient, full: dict[str, Instrument], monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = client.get("/eua").text
    assert "AAPL" in fresh and "sem dado atual" not in fresh

    # Five days on, with no new bars: every close is now older than expected.
    monkeypatch.setattr(web, "utcnow", lambda: NOW + timedelta(days=5))
    stale = client.get("/eua").text

    assert "sem dado atual" in stale
    assert "coletor de barras parado" in stale
    assert "última vela de EUA é de 14/10" in stale
    assert "AAPL" not in stale  # no card: a stale move would read as today's


def test_a_card_that_is_shown_but_old_says_so(
    client: TestClient, session: Session, full: dict[str, Instrument]
) -> None:
    # Bars end on the 13th; expected is the 14th. Bound events still list the stock.
    session.execute(
        text("DELETE FROM candle WHERE instrument_id = :i AND ts >= '2026-10-14'"),
        {"i": full["aapl"].id},
    )
    session.add(
        _event(
            "9",
            "AAPL results soon",
            EventKind.EARNINGS,
            NOW + timedelta(days=1),
            instrument_id=full["aapl"].id,
        )
    )
    session.flush()

    html = client.get("/eua").text

    assert "dado velho · fechamento de 13/10, esperado 14/10" in html


def test_agenda_filters_are_plain_links_and_work(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    everything = client.get("/agenda").text
    high_only = client.get("/agenda?mercado=us&impacto=high").text

    assert 'href="/agenda?mercado=us&amp;impacto=all"' in everything
    assert "CPI m/m" in high_only
    assert "AAPL results" not in high_only  # medium impact
    assert "<form" not in everything


def test_agenda_rejects_values_outside_the_allowed_filters(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    assert client.get("/agenda?mercado=%27%3B--").status_code == 422
    assert client.get("/agenda?impacto=<script>").status_code == 422


def test_radar_states_that_positions_are_not_tracked_and_shows_the_limits(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/radar").text

    assert "ainda não são registrados" in html
    assert "1,5% do capital livre" in html and "30 dias" in html and "7 dias" in html


# --- hostile content ---------------------------------------------------------


def test_hostile_headline_comes_out_as_text(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/eua").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_hostile_event_title_comes_out_as_text_on_every_page_that_lists_it(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    for path in ("/", "/agenda", "/cripto"):
        html = client.get(path).text
        assert "<script>alert(1)</script>" not in html, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, path


def test_template_syntax_in_ingested_text_is_not_evaluated(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/eua").text

    assert "{{ 7*7 }}" in html
    assert re.search(r"(?<![\d,.])49(?![\d,.])", html) is None


def test_a_javascript_link_is_never_rendered_as_a_link(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/eua").text

    assert "javascript:" not in html
    assert "AAPL" in html


def test_a_valid_link_is_kept_with_a_safe_rel(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/cripto").text

    assert 'href="https://example.com/a?b=1&amp;c=2"' in html
    assert 'rel="noopener noreferrer"' in html


def test_hostile_symbol_is_escaped(
    client: TestClient, session: Session, full: dict[str, Instrument]
) -> None:
    evil = _instrument(
        session, "<img src=x onerror=alert(1)>", "alpaca", AssetClass.EQUITY
    )
    _bars(session, evil, D(100), D(1_000_000), D(4_000_000))

    html = client.get("/eua").text

    assert "<img src=x" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


# --- the server itself -------------------------------------------------------


def test_security_headers_on_every_response(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    for path in (*ROUTES, "/static/app.css", "/missing"):
        headers = client.get(path).headers
        assert headers["content-security-policy"].startswith("default-src 'none'")
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in headers


def test_content_security_policy_allows_no_inline_script_or_remote_resource() -> None:
    csp = web.CSP

    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "http" not in csp and "*" not in csp


def test_no_route_writes(client: TestClient, full: dict[str, Instrument]) -> None:
    for path in ROUTES:
        assert client.post(path).status_code == 405
        assert client.put(path).status_code == 405
        assert client.delete(path).status_code == 405


def test_automatic_api_docs_are_off(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_server_refuses_to_bind_outside_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["guardrail-web", "--host", "0.0.0.0"])

    with pytest.raises(SystemExit, match="refusing to bind"):
        web.main()

    assert web.HOST == "127.0.0.1"


def test_templates_never_mark_ingested_text_safe() -> None:
    for template in TEMPLATES.glob("*.html"):
        assert "|safe" not in template.read_text(), template.name
        assert "autoescape" not in template.read_text(), template.name


def test_environment_autoescape_is_on() -> None:
    rendered = web.ENV.from_string("{{ x }}").render(x="<b>&")

    assert rendered == "&lt;b&gt;&amp;"


def test_templates_and_script_carry_no_colour_inline_script_or_inner_html() -> None:
    for template in TEMPLATES.glob("*.html"):
        body = template.read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), template.name
        assert "style=" not in body and "<script>" not in body, template.name
    assert "innerHTML" not in (STATIC / "app.js").read_text()


def test_stylesheet_defines_every_colour_once_in_root() -> None:
    css = (STATIC / "app.css").read_text()
    outside = css.split("}", 1)[1]

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", outside)
    assert "rgb(" not in outside.replace("rgba(20, 18, 15", "")
    for forbidden in (
        "gradient",
        "transition",
        "animation",
        "@keyframes",
        "prefers-color-scheme",
        "blur(",
    ):
        assert forbidden not in css
    assert "color-scheme: light" in css
    assert "--paper:     #FAFAF8" in css and "--accent:    #1A4B96" in css


def test_the_page_works_without_the_script(
    client: TestClient, full: dict[str, Instrument]
) -> None:
    html = client.get("/").text

    assert html.count("<script") == 1 and 'src="/static/app.js"' in html
