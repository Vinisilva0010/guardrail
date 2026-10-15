"""Snapshot tests for the asset card in its three states.

The card has five blocks in a fixed order so the eye learns where each thing
lives. The states below are the ones that matter: everything present, no event
on the calendar, and sources that failed. Run with UPDATE_SNAPSHOTS=1 to rewrite
the files, then read the diff: a snapshot nobody reads proves nothing.
"""

import os
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from guardrail.db.models import EventImpact, EventKind, EventScope
from guardrail.web.app import ENV
from guardrail.web.queries import (
    AssetCard,
    EventView,
    HeadlineView,
    Notice,
    Positioning,
    StatView,
)

D = Decimal
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
SNAPSHOTS = Path(__file__).parent / "snapshots"
CLOSES = [D(100) + D(i) for i in range(30)]


def _normalise(html: str) -> str:
    """One tag per line, no indentation noise, so a diff points at the change."""
    flat = re.sub(r">\s+<", ">\n<", html.strip())
    return re.sub(r"[ \t]+\n", "\n", flat) + "\n"


def _render(card: AssetCard) -> str:
    return _normalise(ENV.get_template("_card_asset.html").render(card=card, now=NOW))


def _check(name: str, html: str) -> None:
    path = SNAPSHOTS / f"{name}.html"
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not path.exists():
        path.write_text(html)
        if os.environ.get("UPDATE_SNAPSHOTS") != "1":
            pytest.fail(f"snapshot {name} did not exist and was written; review it")
    assert html == path.read_text()


def _event() -> EventView:
    return EventView(
        id=1,
        title="Token unlock",
        kind=EventKind.TOKEN_UNLOCK,
        scope=EventScope.CRYPTO,
        impact=EventImpact.HIGH,
        starts_at=NOW + timedelta(days=3),
        has_time=False,
        date_is_estimated=False,
        expected=None,
        previous=None,
        unit=None,
        symbol="SOL",
        source="test",
    )


def _card(**override: Any) -> AssetCard:
    base = AssetCard(
        instrument_id=1,
        symbol="SOLUSDT",
        label="SOL",
        venue="binance",
        market="crypto",
        currency="US$",
        last_day=date(2026, 10, 14),
        last_close=D("122.40"),
        change=D("0.042"),
        closes=CLOSES,
        rvol=D("4.2"),
        turnover=None,
        stale=False,
        expected_day=date(2026, 10, 14),
    )
    return replace(base, **override)


def test_complete_card() -> None:
    card = _card(
        next_event=_event(),
        positioning=Positioning(
            funding=D("-0.00012"),
            funding_ts=NOW - timedelta(minutes=40),
            oi_change=D("-0.084"),
            oi_ts=NOW - timedelta(minutes=5),
            stale=False,
            reading="Posições reduzindo, vendidos pagando",
            notice=None,
        ),
        history=StatView(
            14, "SOL", D("-0.021"), D("0.03"), D("-0.09"), D("0.062"), D("0.4")
        ),
        headline=HeadlineView(
            "Solana ETF decision delayed",
            "CoinDesk",
            NOW - timedelta(hours=2),
            "https://example.com/solana",
        ),
    )

    _check("asset_card_complete", _render(card))


def test_card_without_a_scheduled_event() -> None:
    card = _card(
        event_notice=Notice(
            "warn",
            "sem evento marcado · cobertura parcial: "
            "desbloqueio de token: fonte paga (DefiLlama, HTTP 402)",
        ),
        positioning=Positioning(
            D("0.0001"), NOW, D("0.01"), NOW, False, "Posicionamento estável", None
        ),
        headline=HeadlineView("Quiet day", "Decrypt", NOW - timedelta(hours=1), None),
    )

    _check("asset_card_no_event", _render(card))


def test_card_whose_sources_failed() -> None:
    card = _card(
        stale=True,
        rvol=None,
        last_day=date(2026, 10, 3),
        next_event=_event(),
        positioning=Positioning(
            None,
            None,
            None,
            None,
            True,
            None,
            Notice("alert", "sem dado · nenhuma linha de funding ou open interest"),
        ),
        history=StatView(3, "SOL", None, None, None, None, None),
        headline_notice=Notice(
            "alert",
            "sem dado · fonte de notícia com falha: última coleta 22:03 (há 10 dias)",
        ),
    )

    _check("asset_card_sources_failed", _render(card))


def test_card_blocks_keep_their_order() -> None:
    card = _card(
        next_event=_event(),
        positioning=Positioning(
            D("0.0001"), NOW, D("0.01"), NOW, False, "Posicionamento estável", None
        ),
        history=StatView(
            14, "SOL", D("-0.021"), D("0.03"), D("-0.09"), D("0.062"), D("0.4")
        ),
        headline=HeadlineView("h", "o", NOW, None),
    )

    html = _render(card)

    labels = [
        "SOLUSDT",
        "Próximo evento",
        "Posicionamento",
        "Quando isso aconteceu antes",
        "“h”",
    ]
    positions = [html.index(label) for label in labels]
    assert positions == sorted(positions)


def test_equity_card_swaps_positioning_for_volume_and_liquidity() -> None:
    card = _card(
        symbol="AAPL",
        label="AAPL",
        venue="alpaca",
        market="us",
        turnover=D(45_000_000),
        positioning=None,
        next_event=_event(),
        headline=HeadlineView("h", "o", NOW, None),
    )

    html = _render(card)

    assert "Volume e liquidez" in html and "US$ 45 M" in html
    assert "Posicionamento" not in html


def test_a_small_sample_is_never_printed_as_numbers() -> None:
    card = _card(
        next_event=_event(),
        history=StatView(7, "SOL", D("0.5"), D("0.5"), D("0.4"), D("0.6"), D("1")),
        headline=HeadlineView("h", "o", NOW, None),
    )

    html = _render(card)

    assert "histórico insuficiente (n=7)" in html
    assert "mediana" not in html
