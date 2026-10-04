"""Shared test fixtures.

Each test runs inside a transaction that is rolled back on teardown, so tests
never leave rows behind — including when they fail mid-way.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from guardrail.db.session import get_engine


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session bound to a transaction that is always rolled back.

    The session begins a nested transaction (SAVEPOINT). Tests that deliberately
    trigger an IntegrityError abort only that savepoint, leaving the outer
    transaction alive so teardown can roll it back cleanly.
    """
    connection = get_engine().connect()
    transaction = connection.begin()
    db_session = Session(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield db_session
    finally:
        db_session.close()
        transaction.rollback()
        connection.close()
