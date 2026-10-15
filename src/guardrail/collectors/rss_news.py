"""News from public RSS feeds.

Covers the markets the Alpaca feed does not: crypto and Brazilian equities.
Feeds are public and need no key.

Headlines are matched to instruments by ticker or asset-name mention, which is
deliberately conservative: a story that does not name a symbol is counted and
dropped, because a catalyst row must belong to an instrument and guessing one
would feed the catalyst gate noise.
"""

import argparse
import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import urlparse

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.collectors.store import record_failure, record_success
from guardrail.db.models import Catalyst, Instrument
from guardrail.db.session import dispose_engine, session_scope

log = structlog.get_logger(__name__)

HEADERS: Final = {"User-Agent": "Mozilla/5.0 (compatible; guardrail/1.0)"}

# Third-party text is hostile input: bound it at ingestion (PLANO §7).
MAX_TITLE_CHARS: Final = 300

FEEDS: Final = {
    "crypto": (
        ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("decrypt", "https://decrypt.co/feed"),
    ),
    "br": (
        ("infomoney", "https://www.infomoney.com.br/feed/"),
        ("moneytimes", "https://www.moneytimes.com.br/feed/"),
        ("seudinheiro", "https://www.seudinheiro.com/feed/"),
    ),
}

# Crypto names that map to the symbols stored here. Matched as whole words:
# "sol" inside "solution" or "eth" inside "method" is not a mention.
CRYPTO_TERMS: Final = {
    "BTCUSDT": ("bitcoin", "btc"),
    "ETHUSDT": ("ethereum", "eth", "ether"),
    "SOLUSDT": ("solana", "sol"),
}
_CRYPTO_PATTERNS: Final = {
    symbol: re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)
    for symbol, terms in CRYPTO_TERMS.items()
}

# A B3 ticker: four letters then a digit, optionally 11 for units and funds.
B3_TICKER: Final = re.compile(r"\b([A-Z]{4}(?:3|4|5|6|11))\b")


@dataclass(frozen=True, slots=True)
class Headline:
    """One story."""

    source: str
    title: str
    url: str | None
    published_at: datetime
    market: str


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def safe_url(raw: str) -> str | None:
    """Keep a link only when it is plain http or https."""
    if urlparse(raw).scheme.lower() in {"http", "https"}:
        return raw
    return None


def parse_feed(source: str, market: str, body: bytes) -> list[Headline]:
    """Read one RSS document into headlines.

    Raises ET.ParseError on a document that is not XML: an unreadable feed is a
    source failure and the caller must record it, not treat it as "no news".
    """
    root = ET.fromstring(body)

    out: list[Headline] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = _parse_date(item.findtext("pubDate"))
        if not title or published is None:
            continue
        out.append(
            Headline(source, title[:MAX_TITLE_CHARS], safe_url(link), published, market)
        )
    return out


def match_symbols(session: Session, headline: Headline) -> list[int]:
    """Instrument ids this headline names. Empty when it names none."""
    if headline.market == "crypto":
        found = [
            symbol
            for symbol, pattern in _CRYPTO_PATTERNS.items()
            if pattern.search(headline.title)
        ]
        if not found:
            return []
        return list(
            session.execute(
                select(Instrument.id).where(
                    Instrument.venue == "binance", Instrument.symbol.in_(found)
                )
            ).scalars()
        )

    tickers = set(B3_TICKER.findall(headline.title.upper()))
    if not tickers:
        return []
    return list(
        session.execute(
            select(Instrument.id).where(
                Instrument.venue == "b3", Instrument.symbol.in_(tickers)
            )
        ).scalars()
    )


def store(session: Session, headlines: list[Headline]) -> tuple[int, int]:
    """Store headlines against the instruments they name.

    Returns (rows written, headlines that named no instrument).
    """
    rows: list[dict[str, object]] = []
    unmatched = 0

    for h in headlines:
        ids = match_symbols(session, h)
        if not ids:
            unmatched += 1
            continue
        for instrument_id in ids:
            key = hashlib.sha256(
                f"{h.source}|{h.url}|{h.title}|{instrument_id}".encode()
            ).hexdigest()
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "ts": h.published_at,
                    "source": f"rss.{h.source}",
                    "headline": h.title,
                    "url": safe_url(h.url) if h.url else None,
                    "dedupe_key": key,
                }
            )

    if not rows:
        return 0, unmatched

    statement = (
        pg_insert(Catalyst)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Catalyst.id)
    )
    return len(session.execute(statement).fetchall()), unmatched


async def fetch_all(http: httpx.AsyncClient) -> tuple[list[Headline], dict[str, str]]:
    """Read every feed. Returns (headlines, failures keyed by source)."""
    headlines: list[Headline] = []
    failures: dict[str, str] = {}
    for market, feeds in FEEDS.items():
        for source, url in feeds:
            try:
                response = await http.get(url)
                response.raise_for_status()
                items = parse_feed(source, market, response.content)
            except (httpx.HTTPError, ET.ParseError) as exc:
                failures[source] = f"{type(exc).__name__}: {exc}"
                log.warning("rss.failed", source=source, error=failures[source])
            else:
                headlines.extend(items)
                log.info("rss.read", source=source, items=len(items))
            await asyncio.sleep(0.2)
    return headlines, failures


def collect(session: Session, headlines: list[Headline]) -> tuple[int, int]:
    """Store headlines. Returns (rows written, unmatched headlines)."""
    return store(session, headlines)


async def run() -> tuple[int, int, int, int]:
    """Entry point. Returns (headlines, stored, unmatched, failed feeds)."""
    async with httpx.AsyncClient(
        timeout=30.0, headers=HEADERS, follow_redirects=True
    ) as http:
        headlines, failures = await fetch_all(http)

    with session_scope() as session:
        stored, unmatched = collect(session, headlines)
        for source, _url in (f for feeds in FEEDS.values() for f in feeds):
            name = f"rss.{source}"
            if source in failures:
                record_failure(session, name, failures[source])
            else:
                record_success(session, name)
    return len(headlines), stored, unmatched, len(failures)


def main() -> int:
    """Command line entry point."""
    argparse.ArgumentParser(description="Collect RSS news.").parse_args()
    try:
        read, stored, unmatched, failed = asyncio.run(run())
    finally:
        dispose_engine()

    print(f"\nheadlines read:      {read}")
    print(f"stored with symbol:  {stored}")
    print(f"unmatched (general): {unmatched}")
    print(f"feeds failed:        {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
