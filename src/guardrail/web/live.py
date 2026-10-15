"""Live prices, straight from the exchange.

Stored candles are only as fresh as the collector's last run, which is never
"now". This module answers the one question the chart cannot: what the price is
this second.

One upstream call serves every open tab, because the answer is held for a few
seconds. When the exchange cannot be reached the last good answer is returned
with its age and the error alongside it: a stale price labelled as stale is
useful, a stale price shown as current is a trap.
"""

import asyncio
import json
import time
from decimal import Decimal
from typing import Final, NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict, Field

BASE_URL: Final = "https://api.binance.com"
TICKER_PATH: Final = "/api/v3/ticker/24hr"
# Held this long. Ten tabs polling every few seconds then cost one call, which
# keeps this far under the public request budget whatever the page does.
CACHE_SECONDS: Final = 5.0
TIMEOUT_SECONDS: Final = 4.0
MAX_SYMBOLS: Final = 20
# Past this, the price on screen is labelled stale rather than shown plainly.
STALE_AFTER_SECONDS: Final = 30.0


class Ticker(BaseModel):
    """One symbol's rolling 24-hour figures."""

    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    last: Decimal = Field(alias="lastPrice")
    change_pct: Decimal = Field(alias="priceChangePercent")
    high: Decimal = Field(alias="highPrice")
    low: Decimal = Field(alias="lowPrice")
    quote_volume: Decimal = Field(alias="quoteVolume")


class Snapshot(NamedTuple):
    """What the page gets: the quotes, how old they are, and what went wrong.

    `error` being set does not mean there are no quotes. It means the last
    attempt failed and the quotes are from the attempt before it.
    """

    tickers: list[Ticker]
    age_seconds: float
    error: str | None

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > STALE_AFTER_SECONDS or not self.tickers


class _Held:
    """The one cached answer, shared by every request to this process."""

    def __init__(self) -> None:
        self.at: float = 0.0
        self.tickers: list[Ticker] = []
        self.error: str | None = None
        self.lock = asyncio.Lock()


_HELD: Final = _Held()


def reset_cache() -> None:
    """Forget the held answer. For tests; never called by the server."""
    _HELD.at = 0.0
    _HELD.tickers = []
    _HELD.error = None


async def quotes(
    symbols: tuple[str, ...],
    client: httpx.AsyncClient | None = None,
    now: float | None = None,
) -> Snapshot:
    """The current price of each symbol, from cache when it is fresh enough."""
    if not symbols:
        raise ValueError("at least one symbol is required")
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError(f"at most {MAX_SYMBOLS} symbols per call")

    moment = time.monotonic() if now is None else now
    async with _HELD.lock:
        if _HELD.tickers and moment - _HELD.at < CACHE_SECONDS:
            return Snapshot(_HELD.tickers, moment - _HELD.at, _HELD.error)

        owned = client is None
        http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            fetched = await _fetch(http, symbols)
        except (httpx.HTTPError, ValueError) as problem:
            # Keep whatever was held: an old price with its age beats no price.
            _HELD.error = f"{type(problem).__name__}: {problem}"
            return Snapshot(_HELD.tickers, moment - _HELD.at, _HELD.error)
        finally:
            if owned:
                await http.aclose()

        _HELD.tickers = fetched
        _HELD.at = moment
        _HELD.error = None
        return Snapshot(fetched, 0.0, None)


async def _fetch(client: httpx.AsyncClient, symbols: tuple[str, ...]) -> list[Ticker]:
    """Ask the exchange. Raises on anything that is not a usable answer."""
    response = await client.get(
        f"{BASE_URL}{TICKER_PATH}",
        params={"symbols": json.dumps(list(symbols), separators=(",", ":"))},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("expected a list of tickers")
    tickers = [Ticker.model_validate(row) for row in payload]
    if not tickers:
        raise ValueError("the exchange returned no tickers")
    return tickers
