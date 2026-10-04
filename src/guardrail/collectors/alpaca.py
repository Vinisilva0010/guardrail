"""Alpaca market data client for US equities.

Read-only use: this project never places orders through Alpaca.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from guardrail.collectors.errors import UpstreamDataError
from guardrail.config import get_settings

log = structlog.get_logger(__name__)

DATA_URL: Final = "https://data.alpaca.markets"
TRADING_URL: Final = "https://paper-api.alpaca.markets"

# Consolidated tape, hard-coded rather than configurable. The iex feed reports a
# single venue: 2% to 6% of real volume, verified at 1.2M vs 42M shares a day on
# a large cap. Selecting it by mistake would skew every liquidity figure by up to
# fifty times without raising anything.
FEED: Final = "sip"

# ARCA and BATS list mostly ETFs; OTC volume is irregular and a breakout there
# does not hold. See SPEC section 9.1.
ELIGIBLE_EXCHANGES: Final = frozenset({"NASDAQ", "NYSE", "AMEX"})

# The API caps a response at this many bars and paginates beyond it.
MAX_BARS_PER_RESPONSE: Final = 10_000

# Symbols per request. 150 stays well inside the URL length limit while keeping
# the number of round trips low.
SYMBOLS_PER_REQUEST: Final = 150


@dataclass(frozen=True, slots=True)
class Asset:
    """A tradable US equity."""

    symbol: str
    name: str
    exchange: str


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One daily OHLCV bar on the consolidated tape."""

    symbol: str
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _decimal(value: Any, field: str) -> Decimal:
    """Convert a JSON number to Decimal without passing through float.

    json.load already produced a float for these fields; str() of that float is
    the closest decimal representation and is what every other collector uses.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise UpstreamDataError(f"bad decimal in {field}: {value!r}") from exc


def _parse_bar(symbol: str, raw: dict[str, Any]) -> DailyBar:
    try:
        day = datetime.fromisoformat(raw["t"].replace("Z", "+00:00")).date()
        bar = DailyBar(
            symbol=symbol,
            day=day,
            open=_decimal(raw["o"], "open"),
            high=_decimal(raw["h"], "high"),
            low=_decimal(raw["l"], "low"),
            close=_decimal(raw["c"], "close"),
            volume=_decimal(raw["v"], "volume"),
        )
    except (KeyError, ValueError, AttributeError) as exc:
        raise UpstreamDataError(f"malformed bar for {symbol}: {exc}") from exc

    if bar.high < bar.low:
        raise UpstreamDataError(f"{symbol} {day}: high below low")
    if bar.low <= 0:
        raise UpstreamDataError(f"{symbol} {day}: non-positive price")
    return bar


def _traded(bar: DailyBar) -> bool:
    """Whether the bar represents actual trading.

    A zero-volume bar is a placeholder, not a price. LINE carries 201 of them
    priced at $0.18 before it began trading at $80, and the step from the last
    placeholder to the first real bar reads as a 449x move: the single largest
    unexplained jump in three years of data, caused entirely by storing a price
    at which nothing changed hands.
    """
    return bar.volume > 0


def latest_available_day() -> date:
    """The most recent day the free plan will serve.

    Requesting through today returns 403 "subscription does not permit querying
    recent SIP data". Irrelevant to the strategy: the equity setup reads daily
    closes and runs in the morning on the prior session.
    """
    return datetime.now(UTC).date() - timedelta(days=1)


class AlpacaClient:
    """Fetches the tradable universe and daily bars."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        settings = get_settings()
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret.get_secret_value(),
        }

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get(
        self, url: str, params: dict[str, Any]
    ) -> dict[str, Any] | list[Any]:
        response = await self._client.get(url, params=params, headers=self._headers)
        if response.status_code in (400, 403):
            raise UpstreamDataError(
                f"alpaca refused ({response.status_code}): {response.text[:200]}"
            )
        response.raise_for_status()
        payload: dict[str, Any] | list[Any] = response.json()
        return payload

    async def fetch_assets(self) -> list[Asset]:
        """Return tradable equities on the eligible exchanges."""
        payload = await self._get(
            f"{TRADING_URL}/v2/assets",
            {"status": "active", "asset_class": "us_equity"},
        )
        if not isinstance(payload, list):
            raise UpstreamDataError(f"expected a list of assets, got {type(payload)}")

        assets = [
            Asset(symbol=a["symbol"], name=a.get("name", ""), exchange=a["exchange"])
            for a in payload
            if a.get("tradable") and a.get("exchange") in ELIGIBLE_EXCHANGES
        ]
        log.info("alpaca.assets.fetched", total=len(payload), eligible=len(assets))
        return assets

    async def fetch_daily_bars(
        self, symbols: list[str], start: date, end: date | None = None
    ) -> dict[str, list[DailyBar]]:
        """Return daily bars per symbol, batching and paginating as needed."""
        if not symbols:
            return {}
        end = min(end or latest_available_day(), latest_available_day())
        if start > end:
            raise ValueError(f"start {start} is after end {end}")

        out: dict[str, list[DailyBar]] = {}
        for i in range(0, len(symbols), SYMBOLS_PER_REQUEST):
            batch = symbols[i : i + SYMBOLS_PER_REQUEST]
            token: str | None = None
            while True:
                params: dict[str, Any] = {
                    "symbols": ",".join(batch),
                    "timeframe": "1Day",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": MAX_BARS_PER_RESPONSE,
                    "adjustment": "split",
                    "feed": FEED,
                }
                if token:
                    params["page_token"] = token
                payload = await self._get(f"{DATA_URL}/v2/stocks/bars", params)
                if not isinstance(payload, dict):
                    raise UpstreamDataError(f"expected an object, got {type(payload)}")

                for symbol, raw_bars in (payload.get("bars") or {}).items():
                    parsed = (_parse_bar(symbol, raw) for raw in raw_bars)
                    out.setdefault(symbol, []).extend(
                        bar for bar in parsed if _traded(bar)
                    )

                token = payload.get("next_page_token")
                if not token:
                    break
                await asyncio.sleep(0.1)

        for bars in out.values():
            bars.sort(key=lambda b: b.day)
        log.info(
            "alpaca.bars.fetched",
            symbols=len(out),
            bars=sum(len(v) for v in out.values()),
        )
        return out

    async def fetch_corporate_actions(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, Any]:
        """Return the raw corporate-actions payload for a batch of symbols.

        Returned unparsed: the response groups events by type and each type has
        its own field names, so normalising belongs with the caller that knows
        which types matter.
        """
        payload = await self._get(
            f"{DATA_URL}/v1/corporate-actions",
            {
                "symbols": ",".join(symbols),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 1000,
            },
        )
        if not isinstance(payload, dict):
            raise UpstreamDataError(f"expected an object, got {type(payload)}")
        return payload
