"""Brazilian equities (B3) via brapi.

Public endpoints, no key required on the free tier. Stored under venue 'b3' in
the same candle table as the rest, so every query that reads candles sees them.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from guardrail.collectors.binance import Kline
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.quota import CallKind, QuotaExceeded, reserve
from guardrail.collectors.store import (
    get_or_create_instrument,
    record_failure,
    record_success,
    store_klines,
)
from guardrail.config import get_settings
from guardrail.db.models import AssetClass, Instrument
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

BASE_URL: Final = "https://brapi.dev/api"
VENUE: Final = "b3"
SOURCE: Final = "brapi.b3"
QUOTA_SOURCE: Final = "brapi"
TIMEFRAME: Final = "1d"

# The free tier allows exactly one request at a time; more returns 429. Requests
# are therefore made one after another and there is no concurrency setting.

# The free tier serves at most three months of history. That covers the 20-day
# high and the 20-day average volume the screen needs, with room to spare.
DEFAULT_RANGE: Final = "3mo"


@dataclass(frozen=True, slots=True)
class B3Quote:
    """Latest quote for one B3 ticker."""

    symbol: str
    name: str
    price: Decimal
    change_pct: Decimal | None
    volume: Decimal | None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _bar(symbol: str, raw: dict[str, Any]) -> Kline | None:
    """One daily bar, or None when the payload is unusable.

    Zero volume means no trading happened; the price on such a bar is not a
    market price and storing it creates false gaps.
    """
    try:
        ts = datetime.fromtimestamp(int(raw["date"]), tz=UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        o, h, low, c = (
            _decimal(raw.get("open")),
            _decimal(raw.get("high")),
            _decimal(raw.get("low")),
            _decimal(raw.get("close")),
        )
        v = _decimal(raw.get("volume"))
    except (KeyError, TypeError, ValueError):
        return None

    if None in (o, h, low, c, v):
        return None
    assert o and h and low and c and v is not None
    if low <= 0 or h < low or v <= 0:
        return None
    if not (low <= o <= h and low <= c <= h):
        return None

    return Kline(
        open_time=ts,
        close_time=ts + timedelta(days=1) - timedelta(milliseconds=1),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
    )


class BrapiClient:
    """Reads quotes and daily history from brapi."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._headers = {
            "Authorization": f"Bearer {get_settings().brapi_token.get_secret_value()}"
        }

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        reraise=True,
    )
    async def _get(
        self,
        session: Session,
        kind: CallKind,
        path: str,
        params: dict[str, Any] | None = None,
        cost: int = 1,
    ) -> Any:
        # Reserved per attempt: a retry is a request the vendor counts too.
        reserve(session, QUOTA_SOURCE, kind, cost)
        response = await self._client.get(
            f"{BASE_URL}{path}", params=params or {}, headers=self._headers
        )
        if response.status_code == 404:
            raise UpstreamDataError(f"not found: {path}")
        response.raise_for_status()
        return response.json()

    async def list_symbols(self, session: Session) -> list[str]:
        """Every ticker brapi serves."""
        payload = await self._get(session, CallKind.CLOSE, "/available")
        stocks = (payload or {}).get("stocks")
        if not isinstance(stocks, list):
            raise UpstreamDataError(f"unexpected available payload: {type(stocks)}")
        log.info("b3.symbols_listed", count=len(stocks))
        return [str(s) for s in stocks]

    async def fetch_history(
        self,
        session: Session,
        symbol: str,
        span: str = DEFAULT_RANGE,
        kind: CallKind = CallKind.CLOSE,
    ) -> list[Kline]:
        """Daily bars for one ticker."""
        payload = await self._get(
            session, kind, f"/quote/{symbol}", {"range": span, "interval": "1d"}
        )
        results = (payload or {}).get("results") or []
        if not results:
            return []
        raw_bars = results[0].get("historicalDataPrice") or []
        bars = [b for b in (_bar(symbol, r) for r in raw_bars) if b is not None]
        bars.sort(key=lambda k: k.open_time)
        return bars

    async def fetch_quotes(
        self,
        session: Session,
        symbols: list[str],
        kind: CallKind = CallKind.OTHER,
    ) -> list[B3Quote]:
        """Latest quote for a batch of tickers.

        Costs one request per ticker: the free tier meters tickers, not calls.
        """
        payload = await self._get(
            session,
            kind,
            f"/quote/{','.join(symbols)}",
            cost=max(1, len(symbols)),
        )
        out = []
        for r in (payload or {}).get("results") or []:
            price = _decimal(r.get("regularMarketPrice"))
            if price is None or price <= 0:
                continue
            out.append(
                B3Quote(
                    symbol=str(r.get("symbol")),
                    name=str(r.get("longName") or r.get("shortName") or ""),
                    price=price,
                    change_pct=_decimal(r.get("regularMarketChangePercent")),
                    volume=_decimal(r.get("regularMarketVolume")),
                )
            )
        return out


async def backfill_b3(span: str, limit: int | None) -> tuple[int, int]:
    """Fetch and store daily history for every B3 ticker.

    Stops cleanly, keeping what it already stored, when the request budget runs
    out; the source is then recorded as failed so the header shows it.
    """
    fetched = inserted = 0
    halted: str | None = None

    async with httpx.AsyncClient(timeout=60.0) as http:
        client = BrapiClient(http)
        with session_scope() as session:
            try:
                symbols = await client.list_symbols(session)
            except QuotaExceeded as exc:
                halted = str(exc)
                symbols = []
        if limit:
            symbols = symbols[:limit]

        for start in range(0, len(symbols), 40):
            batch = symbols[start : start + 40]
            with session_scope() as session:
                for symbol in batch:
                    try:
                        bars = await client.fetch_history(session, symbol, span)
                    except QuotaExceeded as exc:
                        halted = str(exc)
                        break
                    except Exception as exc:
                        log.warning("b3.symbol_failed", symbol=symbol, error=str(exc))
                        continue
                    if not bars:
                        continue
                    instrument = get_or_create_instrument(
                        session, symbol, VENUE, AssetClass.EQUITY
                    )
                    fetched += len(bars)
                    inserted += store_klines(session, instrument.id, TIMEFRAME, bars)
            log.info(
                "b3.progress",
                done=min(start + 40, len(symbols)),
                total=len(symbols),
                inserted=inserted,
            )
            if halted:
                break

    with session_scope() as session:
        if halted:
            record_failure(session, SOURCE, f"quota: {halted}")
        else:
            record_success(session, SOURCE)
    return fetched, inserted


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Backfill B3 daily bars.")
    parser.add_argument("--range", default=DEFAULT_RANGE, help="free tier caps at 3mo")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    try:
        fetched, inserted = asyncio.run(backfill_b3(args.range, args.limit))
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, SOURCE, f"{type(exc).__name__}: {exc}")
        dispose_engine()
        raise
    finally:
        dispose_engine()

    print()
    print(f"bars fetched:  {fetched}")
    print(f"bars inserted: {inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# --- scheduled refresh of the existing universe -------------------------------------

# One closing run costs one request per ticker. PLANO §3.5 budgets about 600 per
# run; this ceiling stops a run that would silently grow past it (for example if
# the universe table were filled from the full brapi listing).
MAX_REQUESTS_PER_RUN: Final = 700
REFRESH_BATCH: Final = 40
REFRESH_RANGE: Final = "5d"
# A run where more than this share of tickers failed is a failed run.
MAX_FAILED_SHARE: Final = Decimal("0.2")


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of a closing refresh."""

    symbols: int
    requested: int
    fetched: int
    inserted: int
    failed: int
    halted: str | None = None

    @property
    def ok(self) -> bool:
        if self.halted is not None or not self.symbols:
            return False
        return Decimal(self.failed) / Decimal(self.symbols) <= MAX_FAILED_SHARE


def check_ceiling(count: int) -> None:
    """Refuse a run that would spend more than its share of the monthly allowance."""
    if count > MAX_REQUESTS_PER_RUN:
        raise UpstreamDataError(
            f"{count} tickers exceed the per-run ceiling of {MAX_REQUESTS_PER_RUN}; "
            "refusing to spend the monthly allowance"
        )


def b3_universe(session: Session) -> list[str]:
    """The B3 tickers already tracked. The scheduled run never widens this set:
    the universe is decided elsewhere, not by whatever brapi happens to list."""
    return list(
        session.execute(
            select(Instrument.symbol)
            .where(Instrument.venue == VENUE, Instrument.is_active.is_(True))
            .order_by(Instrument.symbol)
        ).scalars()
    )


async def refresh_closes(
    session: Session,
    client: BrapiClient,
    symbols: list[str],
    span: str = REFRESH_RANGE,
) -> RefreshResult:
    """Fetch recent daily bars for `symbols` and store them in the caller's session.

    Stops without raising when the request budget refuses a call, keeping what
    was already stored.
    """
    fetched = inserted = failed = requested = 0
    halted: str | None = None
    for symbol in symbols:
        try:
            requested += 1
            bars = await client.fetch_history(session, symbol, span)
        except QuotaExceeded as exc:
            requested -= 1
            halted = str(exc)
            break
        except Exception as exc:
            failed += 1
            log.warning("b3.symbol_failed", symbol=symbol, error=str(exc))
            continue
        if not bars:
            continue
        instrument = get_or_create_instrument(session, symbol, VENUE, AssetClass.EQUITY)
        fetched += len(bars)
        inserted += store_klines(session, instrument.id, TIMEFRAME, bars)
    return RefreshResult(len(symbols), requested, fetched, inserted, failed, halted)


async def run_refresh(span: str = REFRESH_RANGE) -> RefreshResult:
    """Entry point of the scheduled closing run."""
    with session_scope() as session:
        symbols = b3_universe(session)
    try:
        check_ceiling(len(symbols))
    except UpstreamDataError as exc:
        with session_scope() as session:
            record_failure(session, SOURCE, str(exc))
        raise

    totals = RefreshResult(len(symbols), 0, 0, 0, 0)
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = BrapiClient(http)
        for start in range(0, len(symbols), REFRESH_BATCH):
            with session_scope() as session:
                part = await refresh_closes(
                    session, client, symbols[start : start + REFRESH_BATCH], span
                )
            totals = RefreshResult(
                totals.symbols,
                totals.requested + part.requested,
                totals.fetched + part.fetched,
                totals.inserted + part.inserted,
                totals.failed + part.failed,
                part.halted,
            )
            if part.halted:
                break

    with session_scope() as session:
        if totals.ok:
            record_success(session, SOURCE)
        else:
            reason = (
                totals.halted or f"{totals.failed} of {totals.symbols} tickers failed"
            )
            record_failure(session, SOURCE, reason)
    return totals
