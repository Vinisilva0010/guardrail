"""Tests for the BCB collectors (SGS series and Focus expectations).

Fixtures are real responses. Store tests use private series codes so they cannot
read or collide with the live data the collector wrote.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from guardrail.collectors.bcb import (
    FOCUS_INDICATORS,
    FOCUS_URL,
    INITIAL_YEARS,
    OVERLAP_DAYS,
    SERIES,
    SGS_URL,
    SgsSeries,
    ensure_series,
    fetch_focus,
    fetch_sgs,
    focus_query,
    parse_focus,
    parse_sgs,
    sgs_start,
    store_expectations,
    store_points,
)
from guardrail.collectors.errors import UpstreamDataError
from guardrail.db.models import Expectation, MacroPoint, MacroSeries

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 2)
CODE = "TEST_SERIES"
FOCUS_CODE = "TEST_FOCUS"


def _sgs() -> list[dict[str, str]]:
    loaded: list[dict[str, str]] = json.loads(
        (FIXTURES / "bcb_sgs_432.json").read_text()
    )
    return loaded


def _focus() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / "bcb_focus_ipca.json").read_text())
    return loaded


def _focus_row(**override: Any) -> dict[str, Any]:
    return {**_focus()["value"][0], **override}


# --- SGS ---------------------------------------------------------------------


def test_recorded_sgs_points_are_parsed_and_future_ones_dropped() -> None:
    parsed = parse_sgs(_sgs(), TODAY)

    assert parsed.rows[0] == (date(2026, 10, 30), Decimal("13.75"))
    assert [d for d, _ in parsed.rows] == [
        date(2026, 10, 30),
        date(2026, 10, 31),
        date(2026, 11, 1),
        date(2026, 11, 2),
    ]
    assert parsed.future == 2
    assert parsed.invalid == 0


def test_a_point_dated_after_today_is_not_an_observation() -> None:
    parsed = parse_sgs([{"data": "03/11/2026", "valor": "13.75"}], TODAY)

    assert parsed.rows == []
    assert parsed.future == 1


@pytest.mark.parametrize(
    "row",
    [
        {"data": "2026-10-30", "valor": "13.75"},
        {"data": "30/10/2026", "valor": "n/a"},
        {"data": "30/10/2026", "valor": ""},
        {"data": "31/02/2026", "valor": "1"},
        {"data": "30/10/2026"},
        {"valor": "1"},
        {"data": 20261030, "valor": "1"},
        "text",
        None,
    ],
)
def test_malformed_sgs_row_is_counted_not_stored(row: Any) -> None:
    parsed = parse_sgs([_sgs()[0], row], TODAY)

    assert len(parsed.rows) == 1
    assert parsed.invalid == 1


@pytest.mark.parametrize("payload", [{}, "oops", None, {"erro": "limite"}])
def test_sgs_payload_that_is_not_a_list_is_rejected(payload: Any) -> None:
    with pytest.raises(UpstreamDataError, match="expected a list"):
        parse_sgs(payload, TODAY)


def test_sgs_values_keep_exact_decimals() -> None:
    parsed = parse_sgs([{"data": "01/09/2026", "valor": "0.82"}], TODAY)

    assert parsed.rows[0][1] == Decimal("0.82")


# --- Focus -------------------------------------------------------------------


def test_recorded_focus_rows_are_parsed() -> None:
    parsed = parse_focus(_focus(), "IPCA", TODAY)

    assert len(parsed.rows) == 6
    first = parsed.rows[0]
    assert (first.Data, first.DataReferencia) == (date(2026, 10, 9), 2026)
    assert (first.Mediana, first.Media, first.DesvioPadrao) == (
        Decimal("5.2245"),
        Decimal("5.2056"),
        Decimal("0.1947"),
    )
    assert first.numeroRespondentes == 144


def test_focus_payload_without_value_is_rejected() -> None:
    with pytest.raises(UpstreamDataError, match="no 'value' list"):
        parse_focus({"codigo": 400}, "IPCA", TODAY)
    with pytest.raises(UpstreamDataError):
        parse_focus([], "IPCA", TODAY)


@pytest.mark.parametrize(
    "override",
    [
        {"Indicador": "Câmbio"},
        {"baseCalculo": 1},
        {"DesvioPadrao": -0.1},
        {"numeroRespondentes": -3},
        {"DataReferencia": "26"},
        {"DataReferencia": "9999"},
        {"DataReferencia": "2026/T1"},
        {"Data": "09/10/2026"},
        {"Data": None},
        {"Mediana": "high"},
    ],
)
def test_bad_focus_row_is_counted_not_stored(override: dict[str, Any]) -> None:
    payload = {"value": [_focus_row(), _focus_row(**override)]}

    parsed = parse_focus(payload, "IPCA", TODAY)

    assert len(parsed.rows) == 1
    assert parsed.invalid == 1


def test_focus_row_dated_in_the_future_is_skipped() -> None:
    payload = {"value": [_focus_row(Data="2026-12-01")]}

    parsed = parse_focus(payload, "IPCA", TODAY)

    assert (parsed.rows, parsed.future) == ([], 1)


def test_focus_row_without_dispersion_or_respondents_is_kept() -> None:
    row = _focus_row(DesvioPadrao=None, numeroRespondentes=None, Media=None)

    parsed = parse_focus({"value": [row]}, "IPCA", TODAY)

    assert len(parsed.rows) == 1


def test_focus_query_is_encoded_the_way_the_service_accepts() -> None:
    query = focus_query("Câmbio", date(2026, 10, 1))

    assert "+" not in query
    assert "%24" not in query
    assert query.startswith("$top=")
    assert "Indicador%20eq%20'C%C3%A2mbio'" in query
    assert "Data%20ge%20'2026-10-01'" in query
    assert query.endswith("$orderby=Data%20desc")


def test_every_indicator_gets_a_distinct_code() -> None:
    assert len(set(FOCUS_INDICATORS)) == len(FOCUS_INDICATORS)
    assert len(set(FOCUS_INDICATORS.values())) == len(FOCUS_INDICATORS)


# --- storage -----------------------------------------------------------------


def _series() -> SgsSeries:
    return SgsSeries(0, "Test series", "%")


def test_series_and_points_are_idempotent(session: Session) -> None:
    ensure_series(session, CODE, _series())
    ensure_series(session, CODE, _series())
    points = parse_sgs(_sgs(), TODAY).rows

    first = store_points(session, CODE, points)
    second = store_points(session, CODE, points)

    assert (first, second) == (4, 0)
    series = session.execute(
        select(func.count()).select_from(MacroSeries).where(MacroSeries.code == CODE)
    ).scalar_one()
    assert series == 1


def test_no_points_is_a_no_op(session: Session) -> None:
    assert store_points(session, CODE, []) == 0


def test_expectations_are_idempotent(session: Session) -> None:
    rows = parse_focus(_focus(), "IPCA", TODAY).rows

    first = store_expectations(session, FOCUS_CODE, rows)
    second = store_expectations(session, FOCUS_CODE, rows)

    assert (first, second) == (6, 0)
    median = session.execute(
        select(Expectation.median).where(
            Expectation.code == FOCUS_CODE,
            Expectation.horizon_year == 2026,
            Expectation.published_on == date(2026, 10, 9),
        )
    ).scalar_one()
    assert median == Decimal("5.22450000")


def test_resume_point_of_an_empty_series_reaches_back_years(session: Session) -> None:
    start = sgs_start(session, CODE, TODAY)

    assert (TODAY - start).days == 365 * INITIAL_YEARS


def test_resume_point_overlaps_the_newest_stored_point(session: Session) -> None:
    ensure_series(session, CODE, _series())
    store_points(session, CODE, parse_sgs(_sgs(), TODAY).rows)

    start = sgs_start(session, CODE, TODAY)

    # Newest stored point is 2026-11-02; the start sits OVERLAP_DAYS before it.
    assert start == date(2026, 10, 19)
    assert (date(2026, 11, 2) - start).days == OVERLAP_DAYS


def test_stored_points_stay_inside_their_series(session: Session) -> None:
    ensure_series(session, CODE, _series())
    store_points(session, CODE, parse_sgs(_sgs(), TODAY).rows)

    stored = session.execute(
        select(func.count()).select_from(MacroPoint).where(MacroPoint.code == CODE)
    ).scalar_one()

    assert stored == 4


def test_series_definitions_are_unique() -> None:
    numbers = [s.number for s in SERIES.values()]
    assert len(set(numbers)) == len(numbers)


# --- network -----------------------------------------------------------------

SGS_432 = SGS_URL.format(number=432)


@pytest.mark.asyncio
@respx.mock
async def test_sgs_fetch_asks_for_dd_mm_yyyy() -> None:
    route = respx.get(SGS_432).mock(return_value=httpx.Response(200, json=_sgs()))

    async with httpx.AsyncClient() as http:
        payload = await fetch_sgs(http, 432, date(2026, 1, 5))

    assert payload == _sgs()
    assert route.calls.last.request.url.params["dataInicial"] == "05/01/2026"


@pytest.mark.asyncio
@respx.mock
async def test_sgs_rate_limit_raises() -> None:
    respx.get(SGS_432).mock(return_value=httpx.Response(429))

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_sgs(http, 432, date(2026, 1, 5))


@pytest.mark.asyncio
@respx.mock
async def test_sgs_truncated_json_raises() -> None:
    respx.get(SGS_432).mock(
        return_value=httpx.Response(200, content=b'[{"data": "30/1')
    )

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError):
            await fetch_sgs(http, 432, date(2026, 1, 5))


@pytest.mark.asyncio
@respx.mock
async def test_focus_request_keeps_the_service_friendly_encoding() -> None:
    route = respx.get(url__startswith=FOCUS_URL).mock(
        return_value=httpx.Response(200, json=_focus())
    )

    async with httpx.AsyncClient() as http:
        payload = await fetch_focus(http, "Câmbio", date(2026, 10, 1))

    assert payload == _focus()
    raw_path = route.calls.last.request.url.raw_path.decode()
    assert "Indicador%20eq%20'C%C3%A2mbio'" in raw_path
    assert "+" not in raw_path
    assert "%24" not in raw_path


@pytest.mark.asyncio
@respx.mock
async def test_focus_bad_request_raises() -> None:
    respx.get(url__startswith=FOCUS_URL).mock(return_value=httpx.Response(400))

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_focus(http, "IPCA", date(2026, 10, 1))
