"""Database engine and session management.

The engine is synchronous by design. Collectors are async where it matters — the
network — and hand their parsed results to a synchronous session for a single
batched write. Mixing an async ORM into that buys nothing here and introduces a
second concurrency model, which is where session-leak bugs come from.
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from guardrail.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            str(get_settings().database_url),
            # Validates a pooled connection before handing it out. Without this,
            # a connection left open across a PostgreSQL restart fails on its
            # next use instead of being quietly replaced.
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
            future=True,
            # Pin UTC per connection. The host server runs in a local zone, and
            # date_trunc on a timestamptz uses the session zone: grouping by day
            # would silently shift by the host offset, both here and in the
            # backtest. Set on the connection so the server configuration, which
            # serves other projects, is left untouched.
            connect_args={"options": "-c timezone=UTC"},
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            # Keeps attributes readable after commit. The default would issue a
            # fresh SELECT per object on access, which is wasteful for the
            # batched writes collectors perform.
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations.

    Commits on success, rolls back on any exception, and always closes.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """Session dependency for the FastAPI layer introduced in phase 4."""
    with session_scope() as session:
        yield session


def dispose_engine() -> None:
    """Close all pooled connections. Used by tests and on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
