"""Tests for RSS news collection.

The matcher carries the weight. A catalyst row is a boolean gate, so a headline
wrongly tied to an instrument ("solution" read as SOL) is a false catalyst.
"""

import asyncio
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from guardrail.collectors.rss_news import (
    FEEDS,
    MAX_TITLE_CHARS,
    Headline,
    fetch_all,
    match_symbols,
    parse_feed,
    safe_url,
    store,
)
from guardrail.collectors.store import get_or_create_instrument
from guardrail.db.models import AssetClass, Catalyst

B3_TEST_TICKER = "ZZZZ3"

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Bitcoin sobe com fluxo</title><link>https://example.com/a</link>
<pubDate>Wed, 14 Oct 2026 12:00:00 +0000</pubDate></item>
<item><title>ZZZZ3 anuncia dividendos</title><link>https://example.com/b</link>
<pubDate>Wed, 14 Oct 2026 13:30:00 -0300</pubDate></item>
</channel></rss>"""

PARTIAL = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Has everything</title><link>https://example.com/ok</link>
<pubDate>Wed, 14 Oct 2026 12:00:00 +0000</pubDate></item>
<item><title></title><link>https://example.com/no-title</link>
<pubDate>Wed, 14 Oct 2026 12:00:00 +0000</pubDate></item>
<item><title>No date</title><link>https://example.com/no-date</link></item>
<item><title>Bad date</title><pubDate>sometime last week</pubDate></item>
</channel></rss>"""


def _headline(title: str, market: str = "crypto", url: str | None = None) -> Headline:
    return Headline(
        "coindesk", title, url, datetime(2026, 10, 14, 12, tzinfo=UTC), market
    )


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)


def _mock_every_feed(status: int = 200, body: bytes = RSS) -> None:
    for feeds in FEEDS.values():
        for _source, url in feeds:
            respx.get(url).mock(return_value=httpx.Response(status, content=body))


# --- parsing ---------------------------------------------------------------


def test_recorded_payload_is_parsed() -> None:
    items = parse_feed("coindesk", "crypto", RSS)

    assert [i.title for i in items] == [
        "Bitcoin sobe com fluxo",
        "ZZZZ3 anuncia dividendos",
    ]
    assert items[0].published_at == datetime(2026, 10, 14, 12, tzinfo=UTC)
    # -03:00 is normalised to UTC.
    assert items[1].published_at == datetime(2026, 10, 14, 16, 30, tzinfo=UTC)


def test_malformed_payload_raises() -> None:
    """An unreadable feed is a source failure, not "no news"."""
    with pytest.raises(ET.ParseError):
        parse_feed("coindesk", "crypto", b"<rss><channel><item>")


def test_partial_payload_keeps_only_complete_items() -> None:
    items = parse_feed("coindesk", "crypto", PARTIAL)

    assert [i.title for i in items] == ["Has everything"]


def test_title_is_truncated_at_ingestion() -> None:
    body = (
        b"<rss><channel><item><title>" + b"x" * 5000 + b"</title>"
        b"<pubDate>Wed, 14 Oct 2026 12:00:00 +0000</pubDate></item></channel></rss>"
    )

    assert len(parse_feed("coindesk", "crypto", body)[0].title) == MAX_TITLE_CHARS


@pytest.mark.parametrize(
    "raw",
    ["javascript:alert(1)", "data:text/html,<script>", "ftp://x/y", "//evil/x", ""],
)
def test_non_http_links_are_dropped(raw: str) -> None:
    assert safe_url(raw) is None


def test_http_and_https_links_are_kept() -> None:
    assert safe_url("https://example.com/a") == "https://example.com/a"
    assert safe_url("http://example.com/a") == "http://example.com/a"


# --- matching --------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "A new solution for banks",
        "Method of payment changes",
        "Absolute fallacy",
        "Console launch delayed",
    ],
)
def test_substring_of_a_word_is_not_a_crypto_mention(
    session: Session, title: str
) -> None:
    assert match_symbols(session, _headline(title)) == []


def test_whole_word_crypto_mention_matches(session: Session) -> None:
    btc = get_or_create_instrument(session, "BTCUSDT", "binance", AssetClass.CRYPTO)
    sol = get_or_create_instrument(session, "SOLUSDT", "binance", AssetClass.CRYPTO)

    assert match_symbols(session, _headline("Bitcoin falls")) == [btc.id]
    assert match_symbols(session, _headline("SOL rallies")) == [sol.id]


def test_b3_ticker_matches(session: Session) -> None:
    inst = get_or_create_instrument(session, B3_TEST_TICKER, "b3", AssetClass.EQUITY)

    found = match_symbols(session, _headline("ZZZZ3 sobe", market="br"))

    assert found == [inst.id]


# --- storage ---------------------------------------------------------------


def test_store_is_idempotent(session: Session) -> None:
    inst = get_or_create_instrument(session, B3_TEST_TICKER, "b3", AssetClass.EQUITY)
    headlines = [_headline("ZZZZ3 sobe", market="br", url="https://example.com/z")]

    first = store(session, headlines)
    second = store(session, headlines)

    assert first == (1, 0)
    assert second == (0, 0)
    total = session.execute(
        select(func.count())
        .select_from(Catalyst)
        .where(Catalyst.instrument_id == inst.id)
    ).scalar_one()
    assert total == 1


def test_unmatched_headline_is_counted_not_stored(session: Session) -> None:
    assert store(session, [_headline("Markets are quiet")]) == (0, 1)


def test_hostile_headline_is_stored_as_inert_text(session: Session) -> None:
    inst = get_or_create_instrument(session, B3_TEST_TICKER, "b3", AssetClass.EQUITY)
    nasty = "ZZZZ3 <script>alert(1)</script> {{ 7*7 }}'; DROP TABLE catalyst;--"

    store(session, [_headline(nasty, market="br", url="javascript:alert(1)")])

    row = session.execute(
        select(Catalyst).where(Catalyst.instrument_id == inst.id)
    ).scalar_one()
    assert row.headline == nasty
    assert row.url is None


# --- network ---------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_all_reads_every_feed() -> None:
    _mock_every_feed()

    async with httpx.AsyncClient() as http:
        headlines, failures = await fetch_all(http)

    assert failures == {}
    assert len(headlines) == 2 * sum(len(f) for f in FEEDS.values())


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_is_reported_as_a_failure() -> None:
    _mock_every_feed(status=429, body=b"")

    async with httpx.AsyncClient() as http:
        headlines, failures = await fetch_all(http)

    assert headlines == []
    assert set(failures) == {s for feeds in FEEDS.values() for s, _ in feeds}
    assert all("429" in message for message in failures.values())


@pytest.mark.asyncio
@respx.mock
async def test_one_bad_feed_does_not_hide_the_others() -> None:
    _mock_every_feed()
    respx.get(FEEDS["crypto"][0][1]).mock(
        return_value=httpx.Response(200, content=b"<html>not a feed")
    )

    async with httpx.AsyncClient() as http:
        headlines, failures = await fetch_all(http)

    assert list(failures) == [FEEDS["crypto"][0][0]]
    assert len(headlines) == 2 * (sum(len(f) for f in FEEDS.values()) - 1)
