"""The jobs and when they run (PLANO_ANTECIPACAO Phase 5). All times are UTC.

Each adapter turns one collector into an Outcome. The collectors disagree on how
they report failure: the older ones catch their own errors and return a result
with an `error` field, the newer ones raise. The adapters accept both, so the
scheduler sees one thing: it worked or it did not.

Collectors that carry a request ceiling (brapi) refuse inside the collector,
before any HTTP call; the adapter only reports that as a failure.
"""

import asyncio
from collections.abc import Coroutine, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from guardrail.alerts import dispatch as alert_dispatch
from guardrail.analysis import event_impact, themes
from guardrail.collectors import (
    b3,
    backfill,
    bcb,
    calendar_rules,
    corporate_actions,
    equity_backfill,
    forexfactory,
    funding_backfill,
    metrics_backfill,
    news,
    oi_recent,
    rss_news,
    us_calendar,
)
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.session import session_scope
from guardrail.quality.completeness import run_checks
from guardrail.scheduler.runner import Job, Outcome

# Safety net: no single run may hang the worker thread for longer than this.
DEFAULT_TIMEOUT: Final = 1800.0


def _run[T](coro: Coroutine[Any, Any, T], timeout: float = DEFAULT_TIMEOUT) -> T:
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _from_errors(pairs: Iterable[tuple[str, str | None]], ok_detail: str) -> Outcome:
    failed = [f"{name}: {error}" for name, error in pairs if error]
    if failed:
        return Outcome(False, "; ".join(failed)[:500])
    return Outcome(True, ok_detail)


# --- adapters ----------------------------------------------------------------------


def klines(timeframes: tuple[str, ...]) -> Outcome:
    results = _run(
        backfill.run_backfill(
            backfill.DEFAULT_SYMBOLS, timeframes, backfill.DEFAULT_DAYS
        ),
        600,
    )
    return _from_errors(
        [(f"{r.symbol}.{r.timeframe}", r.error) for r in results],
        f"{sum(r.inserted for r in results)} bars",
    )


def funding() -> Outcome:
    results = _run(
        funding_backfill.run_funding_backfill(
            backfill.DEFAULT_SYMBOLS, funding_backfill.DEFAULT_DAYS
        ),
        600,
    )
    return _from_errors(
        [(r.symbol, r.error) for r in results],
        f"{sum(r.written for r in results)} rows",
    )


def vision_metrics() -> Outcome:
    results = _run(
        metrics_backfill.run_metrics_backfill(
            backfill.DEFAULT_SYMBOLS, metrics_backfill.DEFAULT_DAYS
        ),
        1800,
    )
    return _from_errors(
        [(r.symbol, r.error) for r in results],
        f"{sum(r.rows_written for r in results)} rows",
    )


def open_interest() -> Outcome:
    results = _run(oi_recent.run(), 300)
    return _from_errors(
        [(r.symbol, r.error) for r in results],
        f"{sum(r.written for r in results)} rows",
    )


def us_bars() -> Outcome:
    result = _run(equity_backfill.run_equity_backfill(equity_backfill.DEFAULT_DAYS))
    return Outcome(result.error is None, result.error or f"{result.inserted} bars")


def us_actions() -> Outcome:
    fetched, stored = _run(corporate_actions.run_corporate_actions(30), 600)
    return Outcome(True, f"{fetched} read, {stored} stored")


def us_news() -> Outcome:
    parsed, stored = _run(news.run_news_backfill(2, live=True), 600)
    return Outcome(True, f"{parsed} read, {stored} stored")


def rss() -> Outcome:
    read, stored, unmatched, failed = _run(rss_news.run(), 300)
    detail = (
        f"{read} read, {stored} stored, {unmatched} unmatched, {failed} feeds failed"
    )
    return Outcome(failed == 0, detail)


def macro_calendar() -> Outcome:
    result = _run(forexfactory.run(), 120)
    return Outcome(
        True,
        f"{result.parsed} events, {result.upsert.inserted} new, "
        f"{result.vanished} dropped",
    )


def bcb_series() -> Outcome:
    result = _run(bcb.run(), 300)
    if result.failures:
        return Outcome(
            False, "; ".join(f"{k}: {v}" for k, v in result.failures.items())[:500]
        )
    return Outcome(
        True, f"{result.points_added} points, {result.expectations_added} Focus rows"
    )


def us_holidays() -> Outcome:
    result = _run(us_calendar.run(), 120)
    return Outcome(
        True, f"{result.holidays} closed, {result.early_closes} early closes"
    )


def rules() -> Outcome:
    result = calendar_rules.run()
    note = (
        f", B3 table lacks {result.missing_b3_years}" if result.missing_b3_years else ""
    )
    return Outcome(
        True, f"{result.expiries} expiries, {result.b3_holidays} B3 holidays{note}"
    )


def b3_close() -> Outcome:
    result = _run(b3.run_refresh(), 3600)
    detail = (
        f"{result.requested} requests, {result.inserted} bars inserted, "
        f"{result.failed} failed"
    )
    if not result.ok:
        return Outcome(False, result.halted or detail)
    return Outcome(True, detail)


def impact_stats() -> Outcome:
    with session_scope() as session:
        report = event_impact.recompute(session, datetime.now(UTC))
        record_success(session, "analysis.event_impact")
    return Outcome(True, f"{report.rows} rows from {report.samples} samples")


def news_themes() -> Outcome:
    with session_scope() as session:
        report = themes.recompute(session, datetime.now(UTC))
        record_success(session, "analysis.themes")
    return Outcome(True, f"{report.themes} themes from {report.stories} stories")


def audit() -> Outcome:
    """Run the data checks. The job fails only if the checks themselves break.

    What the checks find is the audit's result, not its failure: it goes to
    `quality.completeness` in source_health (red when there are errors), where the
    header and the stale-source alert read it. Failing the job instead would
    retry and open a circuit on a perfectly working audit.
    """
    with session_scope() as session:
        report = run_checks(session)
        if report.errors:
            summary = "; ".join(f"[{f.check}] {f.detail}" for f in report.errors)[:500]
            record_failure(session, "quality.completeness", summary)
            return Outcome(True, f"{len(report.errors)} errors found: {summary}"[:500])
        record_success(session, "quality.completeness")
    return Outcome(True, f"no errors, {len(report.warnings)} warnings")


def alerts() -> Outcome:
    result = _run(alert_dispatch.run(), 120)
    if not result.configured:
        return Outcome(True, "telegram not configured: nothing sent")
    if result.failed:
        return Outcome(False, result.failed)
    return Outcome(True, f"{result.sent} sent of {result.candidates} due")


def heartbeat() -> Outcome:
    with session_scope() as session:
        record_success(session, "scheduler.heartbeat")
    return Outcome(True, "")


# --- schedule -----------------------------------------------------------------------

MINUTE: Final = timedelta(minutes=1)
HOUR: Final = timedelta(hours=1)
DAY: Final = timedelta(days=1)


def all_jobs() -> list[Job]:
    """Every job, with its cadence. A job's `sources` are the source_health keys it
    keeps fresh, so a restart only refetches what is actually stale."""
    return [
        Job("heartbeat", heartbeat, "interval", {"seconds": 60}, MINUTE),
        # Macro calendar: the source rate-limits (429 after a handful of calls in
        # minutes), so a failed run waits long before trying again.
        Job(
            "macro_calendar",
            macro_calendar,
            "interval",
            {"hours": 6},
            6 * HOUR,
            sources=("forexfactory.",),
            backoff_base=timedelta(minutes=30),
            backoff_cap=3 * HOUR,
        ),
        Job(
            "us_news",
            us_news,
            "interval",
            {"minutes": 30},
            30 * MINUTE,
            sources=("alpaca.news",),
        ),
        Job(
            "rss_news", rss, "interval", {"minutes": 30}, 30 * MINUTE, sources=("rss.",)
        ),
        Job(
            "themes",
            news_themes,
            "interval",
            {"minutes": 30},
            30 * MINUTE,
            sources=("analysis.themes",),
        ),
        Job(
            "open_interest",
            open_interest,
            "interval",
            {"minutes": 15},
            15 * MINUTE,
            sources=("binance.oi_recent.",),
        ),
        Job(
            "klines_1h",
            lambda: klines(("1h",)),
            "cron",
            {"minute": 2},
            HOUR,
            sources=("binance.klines.",),
        ),
        Job(
            "klines_4h",
            lambda: klines(("4h",)),
            "cron",
            {"hour": "*/4", "minute": 3},
            4 * HOUR,
            sources=("binance.klines.",),
        ),
        Job(
            "klines_1d",
            lambda: klines(("1d",)),
            "cron",
            {"hour": 0, "minute": 6},
            DAY,
            sources=("binance.klines.",),
        ),
        Job(
            "funding",
            funding,
            "cron",
            {"hour": "*/4", "minute": 20},
            4 * HOUR,
            sources=("binance.funding.",),
        ),
        Job(
            "vision_metrics",
            vision_metrics,
            "cron",
            {"hour": 2, "minute": 30},
            DAY,
            sources=("binance.vision.",),
        ),
        # The free feed serves bars through New York's previous day. 05:30 UTC is
        # 01:30 in New York, after midnight there, when the session that just
        # closed counts as "yesterday"; earlier in the evening it is refused.
        Job(
            "us_bars",
            us_bars,
            "cron",
            {"hour": 5, "minute": 30},
            DAY,
            sources=("alpaca.bars",),
        ),
        Job(
            "us_actions",
            us_actions,
            "cron",
            {"hour": 1, "minute": 10},
            DAY,
            sources=("alpaca.corporate_actions",),
        ),
        Job(
            "us_holidays",
            us_holidays,
            "cron",
            {"hour": 6, "minute": 0},
            DAY,
            sources=("alpaca.calendar",),
        ),
        Job(
            "calendar_rules",
            rules,
            "cron",
            {"hour": 6, "minute": 10},
            DAY,
            sources=("calendar.rules",),
        ),
        Job(
            "bcb",
            bcb_series,
            "cron",
            {"hour": 11, "minute": 15},
            DAY,
            sources=("bcb.",),
        ),
        # Tuesday and Friday after the B3 close: about 4,800 requests a month of
        # the 15,000 (PLANO §3.5). A retry only matters for a transient error.
        Job(
            "b3_close",
            b3_close,
            "cron",
            {"day_of_week": "tue,fri", "hour": 22, "minute": 30},
            4 * DAY,
            sources=("brapi.",),
            backoff_base=2 * HOUR,
            backoff_cap=12 * HOUR,
        ),
        Job(
            "alerts",
            alerts,
            "interval",
            {"minutes": 5},
            5 * MINUTE,
            sources=("alerts.",),
        ),
        Job(
            "impact_stats",
            impact_stats,
            "cron",
            {"hour": 3, "minute": 30},
            DAY,
            sources=("analysis.event_impact",),
        ),
        Job(
            "audit",
            audit,
            "cron",
            {"hour": 4, "minute": 0},
            DAY,
            sources=("quality.completeness",),
        ),
    ]
