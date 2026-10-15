"""Tests for the recent open interest collector.

The recorded rows are a real response from the endpoint (three consecutive
5-minute samples).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from guardrail.collectors.binance_vision import MetricRow
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.oi_recent import URL, OiRecentClient, parse_rows
from guardrail.collectors.store import get_or_create_instrument, store_metrics
from guardrail.db.models import AssetClass, DerivativeStat
from tests.constants import TEST_SYMBOL, TEST_VENUE

D = Decimal
# 1792030800 s is 2026-10-15 02:20:00 UTC.
LAST = datetime.fromtimestamp(1792030800, tz=UTC)
NOW = LAST + timedelta(minutes=2)

RECORDED: list[dict[str, Any]] = [
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "93432.97600000",
        "sumOpenInterestValue": "7619974590.08440000",
        "CMCCirculatingSupply": "20097862.00000000",
        "timestamp": 1792030200000,
    },
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "93704.75900000",
        "sumOpenInterestValue": "7629530134.70392600",
        "CMCCirculatingSupply": "20097862.00000000",
        "timestamp": 1792030500000,
    },
    {
        "symbol": "BTCUSDT",
        "sumOpenInterest": "93353.09700000",
        "sumOpenInterestValue": "7633604895.57011100",
        "CMCCirculatingSupply": "20097862.00000000",
        "timestamp": 1792030800000,
    },
]


def _row(**override: Any) -> dict[str, Any]:
    return {**RECORDED[0], **override}


def test_recorded_response_is_parsed() -> None:
    rows, bad = parse_rows(RECORDED, NOW)

    assert bad == 0
    assert [r.ts for r in rows] == [
        LAST - timedelta(minutes=10),
        LAST - timedelta(minutes=5),
        LAST,
    ]
    assert rows[0].open_interest == D("93432.976")
    assert rows[0].open_interest_value == D("7619974590.0844")
    # This endpoint carries no positioning ratios.
    assert rows[0].toptrader_long_short_account_ratio is None
    assert rows[0].taker_long_short_volume_ratio is None


@pytest.mark.parametrize(
    "payload",
    [{"code": -1121, "msg": "Invalid symbol."}, "oops", None, 5],
)
def test_error_body_with_a_200_status_is_rejected(payload: Any) -> None:
    with pytest.raises(UpstreamDataError, match="expected a list"):
        parse_rows(payload, NOW)


def test_empty_list_is_not_an_error() -> None:
    assert parse_rows([], NOW) == ([], 0)


def test_zero_is_a_reporting_gap_not_a_reading() -> None:
    """Stored as zero it would read as open interest collapsing to nothing."""
    rows, _ = parse_rows([_row(sumOpenInterest="0.000", sumOpenInterestValue="0")], NOW)

    assert (rows[0].open_interest, rows[0].open_interest_value) == (None, None)


def test_partial_response_keeps_good_rows_and_counts_the_rest() -> None:
    payload = [
        RECORDED[0],
        RECORDED[1],
        RECORDED[2],
        {"symbol": "BTCUSDT", "timestamp": 1792031100000},
        _row(sumOpenInterest="n/a"),
        _row(timestamp="soon"),
    ]

    rows, bad = parse_rows(payload, NOW + timedelta(minutes=20))

    assert len(rows) == 3
    assert bad == 3


def test_a_sample_stamped_in_the_future_is_dropped() -> None:
    future = _row(timestamp=int((NOW + timedelta(hours=2)).timestamp() * 1000))

    rows, bad = parse_rows([RECORDED[0], RECORDED[1], future], NOW)

    assert len(rows) == 2 and bad == 1


def test_mostly_unreadable_response_is_a_failure() -> None:
    with pytest.raises(UpstreamDataError, match="most open interest rows unreadable"):
        parse_rows([RECORDED[0], {}, {}, {}], NOW)


# --- network ---------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_client_returns_the_recorded_rows() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=RECORDED))

    async with httpx.AsyncClient() as http:
        payload = await OiRecentClient(http).fetch("BTCUSDT", 3)

    assert payload == RECORDED
    params = route.calls.last.request.url.params
    assert (params["symbol"], params["period"], params["limit"]) == (
        "BTCUSDT",
        "5m",
        "3",
    )


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_is_retried_then_raised() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429))

    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            await OiRecentClient(http).fetch("BTCUSDT")

    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_truncated_json_raises() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b'[{"symbol": "BTC'))

    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError):
            await OiRecentClient(http).fetch("BTCUSDT")


@pytest.mark.asyncio
async def test_limit_outside_the_endpoint_range_is_refused() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="limit must be between"):
            await OiRecentClient(http).fetch("BTCUSDT", 501)
        with pytest.raises(ValueError):
            await OiRecentClient(http).fetch("BTCUSDT", 0)


# --- sharing rows with the Vision dumps ----------------------------------------------


def _instrument_id(session: Session) -> int:
    return get_or_create_instrument(
        session, TEST_SYMBOL, TEST_VENUE, AssetClass.CRYPTO
    ).id


def test_rest_rows_do_not_erase_the_ratios_a_dump_wrote(session: Session) -> None:
    instrument_id = _instrument_id(session)
    ts = LAST
    dump = MetricRow(ts, D("100"), D("9000"), D("1.1"), D("1.2"), D("1.3"))
    store_metrics(session, instrument_id, [dump])

    rest, _ = parse_rows([RECORDED[2]], NOW)
    store_metrics(session, instrument_id, rest)

    row = session.execute(
        select(DerivativeStat).where(DerivativeStat.instrument_id == instrument_id)
    ).scalar_one()
    session.refresh(row)
    assert row.toptrader_long_short_account_ratio == D("1.1")
    assert row.taker_long_short_volume_ratio == D("1.3")
    # The value already present wins; REST only fills what was missing.
    assert row.open_interest == D("100")


def test_a_later_dump_fills_the_ratios_the_rest_row_lacked(session: Session) -> None:
    instrument_id = _instrument_id(session)
    rest, _ = parse_rows([RECORDED[2]], NOW)
    store_metrics(session, instrument_id, rest)

    dump = MetricRow(
        LAST, D("93353.097"), D("7633604895.570111"), D("1.1"), D("1.2"), D("1.3")
    )
    store_metrics(session, instrument_id, [dump])

    row = session.execute(
        select(DerivativeStat).where(DerivativeStat.instrument_id == instrument_id)
    ).scalar_one()
    session.refresh(row)
    assert row.toptrader_long_short_position_ratio == D("1.2")
    assert row.open_interest == D("93353.09700000")


def test_a_second_run_over_the_same_rows_changes_nothing(session: Session) -> None:
    instrument_id = _instrument_id(session)
    rows, _ = parse_rows(RECORDED, NOW)

    store_metrics(session, instrument_id, rows)
    store_metrics(session, instrument_id, rows)

    stored = (
        session.execute(
            select(DerivativeStat).where(DerivativeStat.instrument_id == instrument_id)
        )
        .scalars()
        .all()
    )
    assert len(stored) == 3
