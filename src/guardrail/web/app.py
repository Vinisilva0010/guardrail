"""Local web screen (PLANO_ANTECIPACAO §5).

Rendered on the server with Jinja2 and one hand-written stylesheet. Read only:
there is no route that writes, no CORS, and the server binds to 127.0.0.1 and
refuses to bind anywhere else, because the page shows capital and watchlists.
"""

import argparse
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from guardrail import config
from guardrail.db.models import EventImpact, EventScope
from guardrail.db.session import get_session_factory
from guardrail.web import chart_data, coverage, health, live, queries
from guardrail.web import clock as market_clock
from guardrail.web import format as fmt
from guardrail.web.sparkline import sparkline

HERE: Final = Path(__file__).parent
HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8787

# Content-Security-Policy: nothing from outside, no inline script or style. The
# page works without JavaScript; the one script is a same-origin file.
CSP: Final = (
    "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

NAV: Final = (
    ("/", "Hoje"),
    ("/cripto", "Cripto"),
    ("/eua", "EUA"),
    ("/brasil", "Brasil"),
    ("/macro", "Macro"),
    ("/mundo", "Mundo"),
    ("/agenda", "Agenda"),
    ("/radar", "Radar"),
)

AGENDA_DAYS: Final = 90
MARKET_AGENDA_DAYS: Final = 14
WEEK: Final = 7
ABNORMAL_ON_TODAY: Final = 12
ABNORMAL_ON_MARKET: Final = 12


def utcnow() -> datetime:
    """The current instant. A function so tests can fix it."""
    return datetime.now(UTC)


def make_environment() -> Environment:
    """Jinja environment. Autoescape is on and stays on: headlines, symbols and
    event titles come from outside and are rendered as text, never as markup."""
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        number=fmt.number,
        percent=fmt.percent,
        money=fmt.money,
        compact_money=fmt.compact_money,
        figure=fmt.figure,
        arrow=fmt.arrow,
        direction=fmt.direction,
        when=fmt.event_when,
        countdown=fmt.countdown,
        age=fmt.age,
        clock=fmt.clock,
        day_label=fmt.day_label,
        short_date=fmt.short_date,
        kind_label=fmt.kind_label,
        href=fmt.safe_href,
    )
    env.globals["spark"] = sparkline
    env.globals["venue_label"] = lambda venue: queries.VENUE_LABEL.get(venue, venue)
    return env


ENV: Final = make_environment()


def get_session() -> Iterator[Session]:
    """One read session per request; closed afterwards."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]

# Query parameters shared by the pages that carry a chart. Defined here, above
# the first route that annotates with them: an annotation is evaluated when the
# function is defined, so a name declared further down does not exist yet.
Range = Annotated[str, Query(pattern="^(semana|mes|trimestre|ano)$")]
SymbolQuery = Annotated[str, Query(pattern="^[A-Za-z0-9._-]{1,32}$")]

app = FastAPI(
    title="Guardrail",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Any]
) -> Response:
    """Headers on every response."""
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


# --- page chrome ----------------------------------------------------------------


def _risk_line() -> dict[str, Any]:
    """The limits that apply to every position, always in view.

    Read from config.py, the single home of the SPEC §8 constants. Capital and
    open positions are not tracked yet (SPEC phase 3), so the line says that
    instead of showing a number it does not have.
    """
    per_trade = fmt.percent(config.MAX_RISK_PCT_PER_TRADE, 1, sign=False)
    open_risk = fmt.percent(config.MAX_OPEN_RISK_PCT, 0, sign=False)
    return {
        "limits": (
            f"limite {per_trade} por posição · {open_risk} de risco aberto · "
            f"{config.MAX_NEW_POSITIONS_PER_MONTH} posições novas por mês"
        ),
        "tracked": "capital livre, risco aberto e posições do mês: ainda sem registro",
    }


def render(
    template: str,
    session: Session,
    now: datetime,
    path: str,
    title: str,
    **context: Any,
) -> HTMLResponse:
    """Render a page inside the shared header."""
    dots = health.dots(session, now)
    counts = {s.value: sum(1 for d in dots if d.state is s) for s in health.State}
    html = ENV.get_template(template).render(
        title=title,
        path=path,
        nav=NAV,
        now=now,
        clock=market_clock.market_clock(session, now),
        dots=dots,
        dot_counts=counts,
        risk=_risk_line(),
        **context,
    )
    return HTMLResponse(html)


def _merge_abnormal(
    session: Session, now: datetime
) -> tuple[list[queries.AssetCard], list[tuple[str, queries.Notice]]]:
    cards: list[queries.AssetCard] = []
    notices: list[tuple[str, queries.Notice]] = []
    for market, label in (("crypto", "Cripto"), ("us", "EUA"), ("br", "Brasil")):
        block = queries.abnormal_cards(session, market, now, ABNORMAL_ON_TODAY)
        cards.extend(block.rows)
        if block.notice is not None:
            notices.append((label, block.notice))
    cards.sort(key=lambda c: c.rvol or 0, reverse=True)
    return cards[:ABNORMAL_ON_TODAY], notices


# --- routes ---------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def today(
    session: SessionDep,
    ativo: SymbolQuery = "BTCUSDT",
    faixa: Range = chart_data.DEFAULT_RANGE,
) -> HTMLResponse:
    """The page for opening in the morning: the chart first, then the agenda."""
    now = utcnow()
    # Checked before rendering: an empty chart box with no message reads as a
    # flat market instead of a missing series.
    timeframe, limit = chart_data.resolve_range(faixa)
    chart_problem: str | None = None
    try:
        chart_data.load_bars(session, ativo, timeframe, limit)
    except (chart_data.UnknownSymbol, ValueError) as problem:
        chart_problem = str(problem)
    cards, notices = _merge_abnormal(session, now)
    events = queries.decorate(
        session, queries.upcoming_events(session, now, WEEK, limit=14)
    )
    return render(
        "hoje.html",
        session,
        now,
        "/",
        "Hoje",
        timeline=queries.timeline(session, now),
        events=events,
        events_notice=queries.macro_gap_notice(session, now),
        themes=queries.themes(session, now, hours=24, limit=3),
        cards=cards,
        card_notices=notices,
        symbol=ativo.upper(),
        chart_problem=chart_problem,
        faixa=faixa,
        ranges=chart_data.RANGE_LABELS,
        symbols=CHART_SYMBOLS,
    )


def _market_page(
    session: Session, now: datetime, market: str, path: str, title: str
) -> HTMLResponse:
    scopes = queries.AGENDA_SCOPES[market]
    events = queries.decorate(
        session,
        queries.upcoming_events(
            session,
            now,
            MARKET_AGENDA_DAYS,
            scopes=scopes,
            impacts=(EventImpact.HIGH, EventImpact.MEDIUM, EventImpact.LOW)
            if market != "crypto"
            else (EventImpact.HIGH, EventImpact.MEDIUM),
            limit=30,
        ),
    )
    return render(
        "mercado.html",
        session,
        now,
        path,
        title,
        market=market,
        events=events,
        events_notice=None if events else queries.macro_gap_notice(session, now),
        abnormal=queries.abnormal_cards(session, market, now, ABNORMAL_ON_MARKET),
        with_event=queries.bound_event_cards(session, market, now),
        positioning=queries.positioning_cards(session, now)
        if market == "crypto"
        else None,
        brazil=queries.brazil_macro(session, now) if market == "br" else None,
    )


@app.get("/cripto", response_class=HTMLResponse)
def crypto(session: SessionDep) -> HTMLResponse:
    """Crypto."""
    return _market_page(session, utcnow(), "crypto", "/cripto", "Cripto")


@app.get("/eua", response_class=HTMLResponse)
def us(session: SessionDep) -> HTMLResponse:
    """US equities."""
    return _market_page(session, utcnow(), "us", "/eua", "EUA")


@app.get("/brasil", response_class=HTMLResponse)
def brazil(session: SessionDep) -> HTMLResponse:
    """Brazilian equities."""
    return _market_page(session, utcnow(), "br", "/brasil", "Brasil")


@app.get("/macro", response_class=HTMLResponse)
def macro(session: SessionDep) -> HTMLResponse:
    """Central banks, inflation, jobs, rates."""
    now = utcnow()
    return render(
        "macro.html",
        session,
        now,
        "/macro",
        "Macro",
        events=queries.macro_events(session, now),
        brazil=queries.brazil_macro(session, now),
        gaps=coverage.MACRO_GAPS,
    )


@app.get("/mundo", response_class=HTMLResponse)
def world(session: SessionDep) -> HTMLResponse:
    """Assets moving because of what is happening in the world."""
    now = utcnow()
    return render(
        "mundo.html",
        session,
        now,
        "/mundo",
        "Mundo",
        themes=queries.themes(session, now, hours=48, limit=12),
    )


MARKET_FILTERS: Final = ("all", "global", "us", "br", "crypto")
IMPACT_FILTERS: Final = ("all", "high", "medium", "low")


@app.get("/agenda", response_class=HTMLResponse)
def agenda(
    session: SessionDep,
    mercado: Annotated[str, Query(pattern="^(all|global|us|br|crypto)$")] = "all",
    impacto: Annotated[str, Query(pattern="^(all|high|medium|low)$")] = "all",
) -> HTMLResponse:
    """The full 90-day calendar, grouped by day. Filters are plain links."""
    now = utcnow()
    scopes = None if mercado == "all" else (EventScope(mercado),)
    impacts = None if impacto == "all" else (EventImpact(impacto),)
    events = queries.decorate(
        session,
        queries.upcoming_events(
            session, now, AGENDA_DAYS, scopes=scopes, impacts=impacts, limit=300
        ),
    )
    events.sort(key=lambda e: e.starts_at)
    days: dict[str, list[queries.EventView]] = {}
    for event in events:
        key = (
            event.starts_at.astimezone(UTC).date()
            if not event.has_time
            else fmt.local(event.starts_at).date()
        )
        days.setdefault(fmt.day_label(key), []).append(event)
    return render(
        "agenda.html",
        session,
        now,
        "/agenda",
        "Agenda",
        days=days,
        total=len(events),
        mercado=mercado,
        impacto=impacto,
        markets=MARKET_FILTERS,
        impacts=IMPACT_FILTERS,
        notice=None if events else queries.macro_gap_notice(session, now),
        horizon_note=coverage.MACRO_HORIZON_NOTE,
    )


@app.get("/radar", response_class=HTMLResponse)
def radar(session: SessionDep) -> HTMLResponse:
    """Positions, locked capital and the risk budget."""
    now = utcnow()
    return render(
        "radar.html",
        session,
        now,
        "/radar",
        "Radar",
        limits=(
            (
                "Risco máximo por posição",
                fmt.percent(config.MAX_RISK_PCT_PER_TRADE, 1, sign=False)
                + " do capital livre",
            ),
            (
                "Risco aberto simultâneo",
                fmt.percent(config.MAX_OPEN_RISK_PCT, 0, sign=False)
                + " do capital livre",
            ),
            ("Posições novas por mês", str(config.MAX_NEW_POSITIONS_PER_MONTH)),
            ("Prazo máximo de revisão", f"{config.MAX_REVIEW_HORIZON_DAYS} dias"),
            (
                "Pausa após perdas seguidas",
                f"{config.LOSS_STREAK_COOLDOWN_DAYS} dias após "
                f"{config.LOSS_STREAK_THRESHOLD} perdas",
            ),
            ("Alavancagem máxima", f"{config.MAX_LEVERAGE}x (não suportada)"),
        ),
    )


# --- chart ----------------------------------------------------------------

# Shown as quick links on the asset page. Crypto only for now: these are the
# three symbols with intraday candles stored.
CHART_SYMBOLS: Final = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


@app.get("/api/chart/{symbol}")
def chart_api(
    session: SessionDep,
    symbol: str,
    faixa: Range = chart_data.DEFAULT_RANGE,
) -> JSONResponse:
    """Candles, indicators and levels for one symbol, as JSON.

    The only route on this server that answers with data rather than a page. It
    is still read-only and still same-origin: the chart on /ativo is its only
    caller.
    """
    timeframe, limit = chart_data.resolve_range(faixa)
    try:
        payload = chart_data.build(session, symbol, timeframe, utcnow(), limit)
    except chart_data.UnknownSymbol as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from missing
    except ValueError as bad:
        raise HTTPException(status_code=400, detail=str(bad)) from bad
    return JSONResponse(payload)


@app.get("/ativo/{symbol}", response_class=HTMLResponse)
def asset(
    session: SessionDep,
    symbol: str,
    faixa: Range = chart_data.DEFAULT_RANGE,
) -> HTMLResponse:
    """One asset, with its chart.

    The levels and the reach bands are also written out as text, so the page
    still says something when the chart cannot draw.
    """
    now = utcnow()
    timeframe, limit = chart_data.resolve_range(faixa)
    shell: dict[str, Any] = {
        "symbol": symbol.upper(),
        "faixa": faixa,
        "ranges": chart_data.RANGE_LABELS,
        "symbols": CHART_SYMBOLS,
    }
    try:
        payload = chart_data.build(session, symbol, timeframe, now, limit)
    except (chart_data.UnknownSymbol, ValueError) as problem:
        return render(
            "ativo.html",
            session,
            now,
            "/ativo",
            shell["symbol"],
            meta=None,
            levels=[],
            reach=[],
            profile=None,
            problem=str(problem),
            **shell,
        )
    shell["symbol"] = payload["meta"]["symbol"]
    return render(
        "ativo.html",
        session,
        now,
        "/ativo",
        shell["symbol"],
        meta=payload["meta"],
        levels=payload["levels"],
        reach=payload["reach"],
        profile=payload["profile"],
        problem=None,
        **shell,
    )


@app.get("/api/live")
async def live_api() -> JSONResponse:
    """Prices as of this second, for the strip at the top of every page.

    Answers from a few seconds of cache, so every open tab together costs one
    call to the exchange. A failed attempt still answers, with the previous
    prices, their age and the error, because a price labelled stale is useful
    and a stale price shown as current is a trap.
    """
    snapshot = await live.quotes(CHART_SYMBOLS)
    return JSONResponse(
        {
            "age_seconds": round(snapshot.age_seconds, 1),
            "stale": snapshot.is_stale,
            "error": snapshot.error,
            "quotes": [
                {
                    "symbol": ticker.symbol,
                    "last": float(ticker.last),
                    "change_pct": float(ticker.change_pct),
                    "high": float(ticker.high),
                    "low": float(ticker.low),
                }
                for ticker in snapshot.tickers
            ],
        }
    )


def main() -> int:
    """Run the local server. Binding anywhere but loopback is refused."""
    parser = argparse.ArgumentParser(description="Serve the Guardrail screen.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args()
    if args.host != HOST:
        raise SystemExit(
            f"refusing to bind to {args.host}: this page shows capital; use {HOST}"
        )

    print(f"\n  Abra no navegador:  http://{HOST}:{args.port}\n")
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
