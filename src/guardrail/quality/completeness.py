"""Data completeness and plausibility checks.

The exit criterion for phase 1. Two kinds of problem are reported separately
because they need different responses: a gap means a collector has work left to
do, while an implausible value means the data is wrong and anything computed
from it is wrong too.

Checks that the database already enforces as constraints are not repeated here.
"""

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from guardrail.db.session import dispose_engine, session_scope

# A crypto series samples continuously; a gap of more than this many bars in a
# row means the collector missed a window rather than the venue being closed.
MAX_CRYPTO_GAP_BARS: Final = 3

# Price change between consecutive daily bars beyond what a single session's
# trading plausibly produces. Deliberately high: a small cap doubling or halving
# on news happens weekly and is exactly what the setup hunts for. At 3x this
# check flagged a biotech that fell 82% on a failed trial — a real move, and the
# kind of event the engine must be allowed to see.
#
# What survives this filter is not necessarily bad data. Corporate actions and
# symbol reuse are already detected exactly, from recorded facts; this catches
# what neither explains, and its output feeds the engine as windows to skip
# rather than as rows to delete.
IMPLAUSIBLE_PRICE_FACTOR: Final = Decimal("8")

# How stale a source may be before it stops being trustworthy for live signals.
MAX_STALENESS_DAYS: Final = 5

# A listed symbol with no bars for longer than this has almost certainly been
# reassigned to a different company. Set well beyond any trading halt, which
# runs days or weeks, not months.
MAX_DORMANT_DAYS: Final = 180


@dataclass
class Finding:
    """One problem worth a human looking at it."""

    severity: str
    check: str
    detail: str


@dataclass
class Report:
    """Everything the completeness run found."""

    findings: list[Finding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, severity: str, check: str, detail: str) -> None:
        self.findings.append(Finding(severity, check, detail))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]


def _rows(session: Session, sql: str, **params: object) -> list[Row[tuple[Any, ...]]]:
    return list(session.execute(text(sql), params).all())


def check_crypto_candle_gaps(session: Session, report: Report) -> None:
    """Missing bars in a continuously traded series."""
    sql = """
        WITH espacos AS (
            SELECT i.symbol, c.timeframe, c.ts,
                   lag(c.ts) OVER (
                       PARTITION BY c.instrument_id, c.timeframe ORDER BY c.ts
                   ) AS anterior
            FROM candle c JOIN instrument i ON i.id = c.instrument_id
            WHERE i.asset_class = 'crypto'
        )
        SELECT symbol, timeframe, count(*) AS buracos,
               max(EXTRACT(EPOCH FROM (ts - anterior))) AS maior_salto_seg
        FROM espacos
        WHERE anterior IS NOT NULL
          AND ts - anterior > CASE timeframe
              WHEN '1h' THEN interval '1 hour'
              WHEN '4h' THEN interval '4 hours'
              ELSE interval '1 day' END * :max_bars
        GROUP BY symbol, timeframe
        ORDER BY buracos DESC
    """
    for row in _rows(session, sql, max_bars=MAX_CRYPTO_GAP_BARS):
        horas = (row.maior_salto_seg or 0) / 3600
        report.add(
            "warning",
            "crypto_candle_gaps",
            f"{row.symbol} {row.timeframe}: {row.buracos} gaps, largest {horas:.1f}h",
        )


def check_equity_trading_day_coverage(session: Session, report: Report) -> None:
    """Symbols missing bars on sessions they should have traded.

    Trading days are derived from the data: a day where most of the universe has
    a bar is a session. Each symbol is only measured between its own first and
    last bar, because a symbol that listed mid-period legitimately has no bars
    before it existed — counting those as gaps produced hundreds of false
    warnings, and a check that cries wolf teaches everyone to ignore it.
    """
    sql = """
        WITH pregoes AS (
            SELECT c.ts::date AS dia
            FROM candle c JOIN instrument i ON i.id = c.instrument_id
            WHERE i.asset_class = 'equity' AND c.timeframe = '1d'
            GROUP BY 1
            HAVING count(DISTINCT c.instrument_id) > (
                SELECT count(*) * 0.5 FROM universe_membership
                WHERE exited_on IS NULL
            )
        ),
        cobertura AS (
            SELECT i.symbol,
                   min(c.ts::date) AS primeiro,
                   max(c.ts::date) AS ultimo,
                   count(DISTINCT c.ts::date) AS dias_com_barra
            FROM candle c JOIN instrument i ON i.id = c.instrument_id
            WHERE i.asset_class = 'equity' AND c.timeframe = '1d'
            GROUP BY i.symbol
        )
        SELECT co.symbol, co.dias_com_barra, co.primeiro, co.ultimo,
               (SELECT count(*) FROM pregoes p
                 WHERE p.dia BETWEEN co.primeiro AND co.ultimo) AS esperados
        FROM cobertura co
        ORDER BY (
            SELECT count(*) FROM pregoes p
             WHERE p.dia BETWEEN co.primeiro AND co.ultimo
        ) - co.dias_com_barra DESC
        LIMIT 10
    """
    com_buraco = 0
    for row in _rows(session, sql):
        faltando = (row.esperados or 0) - row.dias_com_barra
        if faltando <= 0:
            continue
        com_buraco += 1
        report.add(
            "warning",
            "equity_missing_sessions",
            f"{row.symbol}: {faltando} of {row.esperados} sessions missing "
            f"between {row.primeiro} and {row.ultimo}",
        )
    report.counts["equity_symbols_with_gaps"] = com_buraco


def check_symbol_reuse(session: Session, report: Report) -> None:
    """Symbols whose series stops for months and later resumes.

    A ticker that goes quiet for a year and comes back is almost always reuse:
    the original company delisted and the exchange reassigned the code. JAN and
    LIFE both trade through mid-2024, disappear, and return in 2026 — the series
    stitches two unrelated companies together, and the seam shows up as one of
    the unexplained price jumps above.

    No threshold judgement here: either the bars are there or they are not.
    """
    sql = """
        WITH espacos AS (
            SELECT i.symbol, c.ts,
                   lag(c.ts) OVER (PARTITION BY c.instrument_id ORDER BY c.ts)
                       AS anterior
            FROM candle c JOIN instrument i ON i.id = c.instrument_id
            WHERE i.asset_class = 'equity' AND c.timeframe = '1d'
        )
        SELECT symbol,
               anterior::date AS parou,
               ts::date AS voltou,
               (ts::date - anterior::date) AS dias_parado
        FROM espacos
        WHERE anterior IS NOT NULL
          AND ts - anterior > interval '1 day' * :gap_days
        ORDER BY dias_parado DESC
        LIMIT 15
    """
    for row in _rows(session, sql, gap_days=MAX_DORMANT_DAYS):
        report.add(
            "warning",
            "possible_symbol_reuse",
            f"{row.symbol}: no bars from {row.parou} to {row.voltou} "
            f"({row.dias_parado} days)",
        )


def check_suspect_discontinuities(session: Session, report: Report) -> None:
    """Price gaps with no corporate action on record.

    A recorded split explains the gap. An unexplained one means either a missing
    corporate action or bad data, and either way the engine would read it as a
    breakout.
    """
    sql = """
        WITH v AS (
            SELECT c.instrument_id, i.symbol, c.ts, c.close,
                   lag(c.close) OVER (
                       PARTITION BY c.instrument_id ORDER BY c.ts
                   ) AS anterior
            FROM candle c JOIN instrument i ON i.id = c.instrument_id
            WHERE i.asset_class = 'equity' AND c.timeframe = '1d'
        )
        SELECT v.symbol, v.ts::date AS dia,
               round(v.close / v.anterior, 2) AS fator
        FROM v
        WHERE v.anterior > 0
          AND (v.close / v.anterior > :fator OR v.anterior / v.close > :fator)
          AND NOT EXISTS (
              SELECT 1 FROM corporate_action ca
              WHERE ca.instrument_id = v.instrument_id
                AND ca.effective_on BETWEEN v.ts - interval '5 days'
                                        AND v.ts + interval '5 days'
          )
        ORDER BY abs(log(v.close / v.anterior)) DESC
        LIMIT 15
    """
    achados = _rows(session, sql, fator=IMPLAUSIBLE_PRICE_FACTOR)
    report.counts["suspect_discontinuities"] = len(achados)
    for row in achados:
        report.add(
            "warning",
            "suspect_discontinuity",
            f"{row.symbol} {row.dia}: price factor {row.fator}, "
            f"no corporate action within 5 days",
        )


def check_source_freshness(session: Session, report: Report) -> None:
    """Sources that stopped updating.

    A collector dying quietly while the engine keeps firing on stale data is the
    worst failure mode this system has.
    """
    sql = """
        SELECT source, last_success_at, consecutive_failures, last_error
        FROM source_health
        ORDER BY source
    """
    limite = datetime.now(UTC) - timedelta(days=MAX_STALENESS_DAYS)
    for row in _rows(session, sql):
        if row.consecutive_failures > 0:
            report.add(
                "error",
                "source_failing",
                f"{row.source}: {row.consecutive_failures} consecutive failures "
                f"({(row.last_error or '')[:60]})",
            )
        if row.last_success_at is None or row.last_success_at < limite:
            visto = (
                row.last_success_at.date().isoformat()
                if row.last_success_at
                else "never"
            )
            report.add("error", "source_stale", f"{row.source}: last success {visto}")


def check_row_counts(session: Session, report: Report) -> None:
    """Headline counts, for a quick sense of scale."""
    sql = """
        SELECT 'candles_crypto' AS k, count(*) AS v FROM candle c
          JOIN instrument i ON i.id = c.instrument_id WHERE i.asset_class = 'crypto'
        UNION ALL SELECT 'candles_equity', count(*) FROM candle c
          JOIN instrument i ON i.id = c.instrument_id WHERE i.asset_class = 'equity'
        UNION ALL SELECT 'derivative_stats', count(*) FROM derivative_stat
        UNION ALL SELECT 'corporate_actions', count(*) FROM corporate_action
        UNION ALL SELECT 'universe_open', count(*) FROM universe_membership
          WHERE exited_on IS NULL
        UNION ALL SELECT 'catalysts', count(*) FROM catalyst
        UNION ALL SELECT 'symbols_without_news', count(*) FROM (
            SELECT um.instrument_id FROM universe_membership um
            WHERE um.exited_on IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM catalyst c
                   WHERE c.instrument_id = um.instrument_id
              )
        ) t
    """
    for row in _rows(session, sql):
        report.counts[row.k] = row.v


CHECKS = (
    check_row_counts,
    check_crypto_candle_gaps,
    check_equity_trading_day_coverage,
    check_symbol_reuse,
    check_suspect_discontinuities,
    check_source_freshness,
)


def run_checks(session: Session) -> Report:
    """Run every check and return the combined report."""
    report = Report()
    for check in CHECKS:
        check(session, report)
    return report


def main() -> int:
    """Command line entry point. Non-zero exit when an error is found."""
    parser = argparse.ArgumentParser(description="Check data completeness.")
    parser.add_argument("--quiet", action="store_true", help="only show problems")
    args = parser.parse_args()

    try:
        with session_scope() as session:
            report = run_checks(session)
    finally:
        dispose_engine()

    if not args.quiet:
        print("COUNTS")
        for k, v in report.counts.items():
            print(f"  {k:<28} {v:>12,}")
        print()

    if report.errors:
        print("ERRORS")
        for f in report.errors:
            print(f"  [{f.check}] {f.detail}")
        print()
    if report.warnings:
        print("WARNINGS")
        for f in report.warnings:
            print(f"  [{f.check}] {f.detail}")
        print()

    if not report.findings:
        print("no problems found")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
