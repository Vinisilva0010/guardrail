"""Brazilian central bank data: SGS series and the Focus expectations survey.

SGS gives realised values (Selic target, monthly IPCA) and fills `macro_point`.
Focus gives what the market expects and fills `expectation`, a separate table on
purpose: a projection stored beside the realised value invites reading one as
the other. Both are public and need no key.

Two traps are handled here because neither fails loudly:

* The Focus service is an OData endpoint that rejects the `+` and `%24` that
  common HTTP clients produce for spaces and `$`. The query string is built by
  hand. Without an explicit ordering it also returns the oldest rows first.
* An SGS series can carry points dated after today. An observation from the
  future is not an observation, so those are dropped and counted.

The Copom meeting calendar is not collected: the official page is a
JavaScript-only application with no documented endpoint (docs/FONTES.md).
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast
from urllib.parse import quote

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import EventScope, Expectation, MacroPoint, MacroSeries
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

SGS_URL: Final = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{number}/dados"
FOCUS_URL: Final = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
    "ExpectativasMercadoAnuais"
)
HEADERS: Final = {"User-Agent": "Mozilla/5.0 (compatible; guardrail/1.0)"}
SGS_HEALTH: Final = "bcb.sgs"
FOCUS_HEALTH: Final = "bcb.focus"

# First load reaches back this far; later runs re-read only a short overlap.
# The daily series is capped by the service at ten years per request.
INITIAL_YEARS: Final = 5
OVERLAP_DAYS: Final = 14
FOCUS_LOOKBACK_DAYS: Final = 45
FOCUS_PAGE: Final = 1000


@dataclass(frozen=True, slots=True)
class SgsSeries:
    """One SGS series and how it is stored."""

    number: int
    name: str
    unit: str


SERIES: Final = {
    "SELIC_TARGET": SgsSeries(432, "Selic target rate", "%"),
    "IPCA_MOM": SgsSeries(433, "IPCA monthly change", "%"),
}

# Focus indicator as the service names it, keyed by the code stored here.
FOCUS_INDICATORS: Final = {
    "IPCA": "IPCA",
    "SELIC": "Selic",
    "PIB": "PIB Total",
    "CAMBIO": "Câmbio",
}


class _SgsPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: date
    valor: Decimal

    @field_validator("data", mode="before")
    @classmethod
    def _day_first(cls, value: object) -> date:
        if isinstance(value, str):
            return datetime.strptime(value, "%d/%m/%Y").date()
        raise ValueError("date must be dd/mm/yyyy text")


class _FocusRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Indicador: str
    Data: date
    DataReferencia: int
    Media: Decimal | None = None
    Mediana: Decimal | None = None
    DesvioPadrao: Decimal | None = None
    numeroRespondentes: int | None = None
    baseCalculo: int

    @field_validator("DataReferencia")
    @classmethod
    def _sane_year(cls, value: int) -> int:
        if not 2000 <= value <= 2100:
            raise ValueError("reference year out of range")
        return value

    @field_validator("DesvioPadrao", "numeroRespondentes")
    @classmethod
    def _not_negative(cls, value: Decimal | int | None) -> Decimal | int | None:
        if value is not None and value < 0:
            raise ValueError("negative dispersion or respondent count")
        return value


@dataclass(frozen=True, slots=True)
class Parsed[T]:
    """Rows read from a document and what was left out."""

    rows: list[T]
    invalid: int
    future: int = 0


def parse_sgs(payload: Any, today: date) -> Parsed[tuple[date, Decimal]]:
    """SGS points. Unreadable rows and future-dated points are counted, not kept."""
    if not isinstance(payload, list):
        raise UpstreamDataError(f"expected a list of SGS points, got {type(payload)}")
    rows: list[tuple[date, Decimal]] = []
    invalid = future = 0
    for raw in payload:
        try:
            point = _SgsPoint.model_validate(raw)
        except (ValidationError, ValueError):
            invalid += 1
            continue
        if point.data > today:
            future += 1
            continue
        rows.append((point.data, point.valor))
    return Parsed(rows, invalid, future)


def parse_focus(payload: Any, indicator: str, today: date) -> Parsed[_FocusRow]:
    """Focus rows for one indicator, general respondent base only."""
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise UpstreamDataError("Focus response has no 'value' list")
    rows: list[_FocusRow] = []
    invalid = future = 0
    for raw in payload["value"]:
        try:
            row = _FocusRow.model_validate(raw)
        except (ValidationError, ValueError):
            invalid += 1
            continue
        if row.Indicador != indicator or row.baseCalculo != 0:
            invalid += 1
            continue
        if row.Data > today:
            future += 1
            continue
        rows.append(row)
    return Parsed(rows, invalid, future)


def focus_query(indicator: str, since: date) -> str:
    """The OData query string, encoded the way the service accepts."""
    flt = (
        f"Indicador eq '{indicator}' and baseCalculo eq 0 "
        f"and Data ge '{since.isoformat()}'"
    )
    return (
        f"$top={FOCUS_PAGE}&$format=json&$filter={quote(flt, safe=chr(39))}"
        "&$orderby=Data%20desc"
    )


def ensure_series(session: Session, code: str, series: SgsSeries) -> None:
    """Create the series row if it is missing."""
    session.execute(
        pg_insert(MacroSeries)
        .values(
            code=code,
            source="bcb",
            name=series.name,
            unit=series.unit,
            scope=EventScope.BR,
        )
        .on_conflict_do_nothing(index_elements=["code"])
    )


def store_points(
    session: Session, code: str, points: list[tuple[date, Decimal]]
) -> int:
    """Insert points not stored yet. Returns rows added."""
    if not points:
        return 0
    rows = [{"code": code, "ref_date": d, "value": v} for d, v in points]
    added = 0
    for start in range(0, len(rows), 1000):
        statement = (
            pg_insert(MacroPoint)
            .values(rows[start : start + 1000])
            .on_conflict_do_nothing(index_elements=["code", "ref_date"])
            .returning(MacroPoint.code)
        )
        added += len(session.execute(statement).fetchall())
    return added


def store_expectations(session: Session, code: str, rows: list[_FocusRow]) -> int:
    """Insert Focus rows not stored yet. Returns rows added."""
    if not rows:
        return 0
    values = [
        {
            "code": code,
            "horizon_year": r.DataReferencia,
            "published_on": r.Data,
            "median": r.Mediana,
            "mean": r.Media,
            "stdev": r.DesvioPadrao,
            "n_respondents": r.numeroRespondentes,
        }
        for r in rows
    ]
    added = 0
    for start in range(0, len(values), 1000):
        statement = (
            pg_insert(Expectation)
            .values(values[start : start + 1000])
            .on_conflict_do_nothing(
                index_elements=["code", "horizon_year", "published_on"]
            )
            .returning(Expectation.code)
        )
        added += len(session.execute(statement).fetchall())
    return added


def sgs_start(session: Session, code: str, today: date) -> date:
    """Where to resume. A short overlap re-reads the newest points; the insert is
    idempotent, so the overlap costs nothing and catches a late correction."""
    # max() over no rows is NULL, which the column's static type cannot express.
    newest = cast(
        "date | None",
        session.execute(
            select(func.max(MacroPoint.ref_date)).where(MacroPoint.code == code)
        ).scalar_one(),
    )
    if newest is None:
        return today - timedelta(days=365 * INITIAL_YEARS)
    return newest - timedelta(days=OVERLAP_DAYS)


async def fetch_sgs(http: httpx.AsyncClient, number: int, start: date) -> Any:
    """One SGS series from `start` on."""
    response = await http.get(
        SGS_URL.format(number=number),
        params={"formato": "json", "dataInicial": start.strftime("%d/%m/%Y")},
    )
    response.raise_for_status()
    return response.json()


async def fetch_focus(http: httpx.AsyncClient, indicator: str, since: date) -> Any:
    """Recent Focus rows of one indicator, newest first."""
    response = await http.get(f"{FOCUS_URL}?{focus_query(indicator, since)}")
    response.raise_for_status()
    return response.json()


@dataclass(frozen=True, slots=True)
class BcbResult:
    """What one run did, per dataset."""

    points_added: int
    expectations_added: int
    future_skipped: int
    invalid: int
    failures: dict[str, str]


async def run() -> BcbResult:
    """Entry point: read every series and indicator, record each source's health."""
    today = datetime.now(UTC).date()
    points_added = expectations_added = future = invalid = 0
    failures: dict[str, str] = {}

    async with httpx.AsyncClient(
        timeout=60.0, headers=HEADERS, follow_redirects=True
    ) as http:
        for code, series in SERIES.items():
            try:
                with session_scope() as session:
                    ensure_series(session, code, series)
                    start = sgs_start(session, code, today)
                payload = await fetch_sgs(http, series.number, start)
                parsed = parse_sgs(payload, today)
                if not parsed.rows:
                    raise UpstreamDataError(
                        f"SGS {series.number} returned no usable points"
                    )
                with session_scope() as session:
                    points_added += store_points(session, code, parsed.rows)
                future += parsed.future
                invalid += parsed.invalid
            except Exception as exc:
                failures[f"sgs.{code}"] = f"{type(exc).__name__}: {exc}"
                log.warning("bcb.sgs_failed", series=code, error=str(exc))
            await asyncio.sleep(0.2)

        since = today - timedelta(days=FOCUS_LOOKBACK_DAYS)
        for code, indicator in FOCUS_INDICATORS.items():
            try:
                payload = await fetch_focus(http, indicator, since)
                parsed_focus = parse_focus(payload, indicator, today)
                if not parsed_focus.rows:
                    raise UpstreamDataError(f"Focus returned no rows for {indicator}")
                with session_scope() as session:
                    expectations_added += store_expectations(
                        session, code, parsed_focus.rows
                    )
                future += parsed_focus.future
                invalid += parsed_focus.invalid
            except Exception as exc:
                failures[f"focus.{code}"] = f"{type(exc).__name__}: {exc}"
                log.warning("bcb.focus_failed", indicator=code, error=str(exc))
            await asyncio.sleep(0.2)

    with session_scope() as session:
        for health, prefix in ((SGS_HEALTH, "sgs."), (FOCUS_HEALTH, "focus.")):
            errors = [f"{k}: {v}" for k, v in failures.items() if k.startswith(prefix)]
            if errors:
                record_failure(session, health, "; ".join(errors)[:1000])
            else:
                record_success(session, health)
    return BcbResult(points_added, expectations_added, future, invalid, failures)


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(description="Collect BCB series and Focus.").parse_args()
    try:
        result = asyncio.run(run())
    finally:
        dispose_engine()

    print(f"\nSGS points added:      {result.points_added}")
    print(f"Focus rows added:      {result.expectations_added}")
    print(f"future-dated skipped:  {result.future_skipped}")
    print(f"unreadable rows:       {result.invalid}")
    for name, error in result.failures.items():
        print(f"FAILED {name}: {error}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
