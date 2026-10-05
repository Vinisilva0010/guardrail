"""News collection from Alpaca, feeding the catalyst gate.

Alpaca carries three years of history, so the catalyst condition of the equity
setup is testable in the backtest. It also means no second account and no second
API key.
"""

import argparse
import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import httpx
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.collectors.alpaca import AlpacaClient, latest_available_day
from guardrail.collectors.equity_backfill import load_universe
from guardrail.collectors.errors import UpstreamDataError
from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import Catalyst
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

SOURCE: Final = "alpaca.news"
DEFAULT_DAYS: Final = 365 * 3
PAGE_LIMIT: Final = 50

# An item tagged with more than this many symbols is a market round-up, not news
# about a company. Measured over 30 days: 62% of items carry a single symbol and
# those are the ones actually about the company; a handful tag 38 symbols at
# once. Counting those as catalysts would give every large cap a catalyst every
# day and the condition would stop filtering anything.
#
# The cut is 3 rather than 1 because genuine company news routinely names a
# second party: an acquisition names buyer and target, an earnings story names
# the peer it is compared against.
MAX_SYMBOLS_PER_ITEM: Final = 3


@dataclass(frozen=True, slots=True)
class NewsItem:
    """One news story, already narrowed to the symbols it is about."""

    item_id: str
    symbols: tuple[str, ...]
    published_at: datetime
    headline: str
    url: str | None


def _dedupe_key(item_id: Any, headline: str, symbol: str) -> str:
    """Stable key per (story, symbol).

    The venue repeats stories across polls and a story can cover several
    symbols, so the key combines both. Hashed to a fixed width because headlines
    run long and the column is bounded.
    """
    raw = f"{item_id}|{symbol}|{headline}".encode()
    return hashlib.sha256(raw).hexdigest()


def parse_news(payload: dict[str, Any]) -> list[NewsItem]:
    """Convert a news response into storable items, dropping round-ups."""
    items = payload.get("news")
    if not isinstance(items, list):
        raise UpstreamDataError(f"expected a list of news, got {type(items)}")

    out: list[NewsItem] = []
    for raw in items:
        symbols = tuple(raw.get("symbols") or ())
        if not symbols or len(symbols) > MAX_SYMBOLS_PER_ITEM:
            continue
        headline = (raw.get("headline") or "").strip()
        created = raw.get("created_at")
        if not headline or not created:
            continue
        try:
            published = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            continue
        out.append(
            NewsItem(
                symbols=symbols,
                published_at=published.astimezone(UTC),
                headline=headline[:2000],
                url=raw.get("url"),
                item_id=str(raw.get("id") or headline),
            )
        )
    return out


def store_news(
    session: Session, items: list[NewsItem], universe: dict[str, int]
) -> int:
    """Insert one catalyst row per (story, symbol in universe)."""
    rows: list[dict[str, object]] = []
    for item in items:
        for symbol in item.symbols:
            instrument_id = universe.get(symbol)
            if instrument_id is None:
                continue
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "ts": item.published_at,
                    "source": SOURCE,
                    "headline": item.headline,
                    "url": item.url,
                    "dedupe_key": _dedupe_key(item.item_id, item.headline, symbol),
                }
            )
    if not rows:
        return 0

    inserted = 0
    for start in range(0, len(rows), 1000):
        statement = (
            pg_insert(Catalyst)
            .values(rows[start : start + 1000])
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(Catalyst.id)
        )
        inserted += len(session.execute(statement).fetchall())
    return inserted


async def fetch_news(client: AlpacaClient, start: date, end: date) -> list[NewsItem]:
    """Page through every news item in the window."""
    out: list[NewsItem] = []
    token: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": PAGE_LIMIT,
            "sort": "asc",
        }
        if token:
            params["page_token"] = token
        payload = await client.fetch_news_page(params)
        out.extend(parse_news(payload))
        pages += 1
        token = payload.get("next_page_token")
        if not token:
            break
        if pages % 50 == 0:
            log.info("news.progress", pages=pages, items=len(out))
        await asyncio.sleep(0.05)
    return out


async def run_news_backfill(days: int) -> tuple[int, int]:
    """Collect news for the universe. Returns (parsed, stored)."""
    start = date.today() - timedelta(days=days)
    end = latest_available_day()

    with session_scope() as session:
        universe = load_universe(session)
    if not universe:
        log.warning("news.empty_universe")
        return 0, 0

    try:
        async with httpx.AsyncClient(timeout=120.0) as http:
            items = await fetch_news(AlpacaClient(http), start, end)
    except Exception as exc:
        with session_scope() as session:
            record_failure(session, SOURCE, f"{type(exc).__name__}: {exc}")
        raise

    stored = 0
    with session_scope() as session:
        stored = store_news(session, items, universe)
        record_success(session, SOURCE)

    log.info("news.collected", parsed=len(items), stored=stored)
    return len(items), stored


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Backfill company news.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    try:
        parsed, stored = asyncio.run(run_news_backfill(args.days))
    finally:
        dispose_engine()

    print()
    print(f"items parsed: {parsed}")
    print(f"rows stored:  {stored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
