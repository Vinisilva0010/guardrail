"""Local web screen.

Binds to localhost only: the page shows what the operator is watching and is not
meant to be reachable from the network.
"""

import argparse
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from guardrail.db.session import session_scope
from guardrail.web import queries
from guardrail.web.page import render_page

# Minimum average daily turnover for an instrument to appear, in its own
# currency. Below this an order of a few thousand moves the price, so a breakout
# shows on the chart but cannot be acted on.
MIN_TURNOVER_US: Final = Decimal("20000000")
MIN_TURNOVER_BR: Final = Decimal("5000000")

app = FastAPI(title="Guardrail")


@dataclass(frozen=True)
class MarketView:
    """One market, already split into the blocks the page renders."""

    label: str
    currency: str
    pulse: queries.MarketPulse
    breaking_out: list[queries.Row]
    turning_up: list[queries.Row]
    beaten_down: list[queries.Row]
    unusual_volume: list[queries.Row]


def _view(label: str, currency: str, venue: str, rows: list[queries.Row]) -> MarketView:
    return MarketView(
        label=label,
        currency=currency,
        pulse=queries.pulse(venue, rows),
        breaking_out=queries.breaking_out(rows),
        turning_up=queries.turning_up(rows),
        beaten_down=queries.beaten_down(rows),
        unusual_volume=queries.unusual_volume(rows),
    )


def collect() -> tuple[list[queries.Row], list[MarketView]]:
    """Read every market once and split it into blocks."""
    with session_scope() as session:
        crypto = queries.crypto(session)
        us = queries.snapshot(session, "alpaca", MIN_TURNOVER_US)
        br = queries.snapshot(session, "b3", MIN_TURNOVER_BR)

    return crypto, [
        _view("ESTADOS UNIDOS", "$", "alpaca", us),
        _view("BRASIL", "R$", "b3", br),
    ]


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """The screen."""
    crypto, markets = collect()
    return HTMLResponse(render_page(crypto, markets))


def main() -> int:
    """Run the local server."""
    parser = argparse.ArgumentParser(description="Serve the Guardrail screen.")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    print(f"\n  Abra no navegador:  http://127.0.0.1:{args.port}\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
