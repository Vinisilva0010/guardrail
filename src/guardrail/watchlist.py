"""The operator's watchlist, managed from the command line.

The screen is read only and has no route that writes, so marking assets is done
here: `uv run guardrail-watch add AAPL PETR4`. A symbol that exists on more than
one venue must be written `venue:symbol`.
"""

import argparse
import sys

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from guardrail.db.models import Instrument, Watchlist
from guardrail.db.session import dispose_engine, session_scope


def resolve(session: Session, token: str) -> Instrument:
    """The instrument named by `SYMBOL` or `venue:SYMBOL`. ValueError if none or
    ambiguous: guessing which of two venues was meant would watch the wrong asset."""
    venue, _, symbol = token.rpartition(":")
    stmt = select(Instrument).where(Instrument.symbol == symbol.upper())
    if venue:
        stmt = stmt.where(Instrument.venue == venue.lower())
    found = list(session.execute(stmt.order_by(Instrument.venue)).scalars())
    if not found:
        raise ValueError(f"no instrument named {token!r}")
    if len(found) > 1:
        venues = ", ".join(i.venue for i in found)
        raise ValueError(
            f"{token!r} exists on several venues ({venues}); write venue:symbol"
        )
    return found[0]


def add(session: Session, tokens: list[str]) -> list[str]:
    """Watch the named instruments. Returns what was added; repeats are no-ops."""
    added: list[str] = []
    for token in tokens:
        instrument = resolve(session, token)
        inserted = session.execute(
            pg_insert(Watchlist)
            .values(instrument_id=instrument.id)
            .on_conflict_do_nothing(index_elements=["instrument_id"])
            .returning(Watchlist.instrument_id)
        ).first()
        if inserted is not None:
            added.append(f"{instrument.venue}:{instrument.symbol}")
    return added


def remove(session: Session, tokens: list[str]) -> list[str]:
    """Stop watching. Returns what was removed."""
    removed: list[str] = []
    for token in tokens:
        instrument = resolve(session, token)
        gone = session.execute(
            delete(Watchlist)
            .where(Watchlist.instrument_id == instrument.id)
            .returning(Watchlist.instrument_id)
        ).first()
        if gone is not None:
            removed.append(f"{instrument.venue}:{instrument.symbol}")
    return removed


def listing(session: Session) -> list[str]:
    """Watched instruments, as venue:symbol."""
    rows = session.execute(
        select(Instrument.venue, Instrument.symbol)
        .join(Watchlist, Watchlist.instrument_id == Instrument.id)
        .order_by(Instrument.venue, Instrument.symbol)
    ).all()
    return [f"{venue}:{symbol}" for venue, symbol in rows]


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Manage the watchlist.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "remove"):
        sub.add_parser(name).add_argument("symbols", nargs="+")
    sub.add_parser("list")
    args = parser.parse_args()

    try:
        with session_scope() as session:
            if args.command == "list":
                for line in listing(session):
                    print(line)
            else:
                done = (add if args.command == "add" else remove)(session, args.symbols)
                verb = "added" if args.command == "add" else "removed"
                print(f"{verb}: {', '.join(done) if done else 'nothing (no change)'}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
