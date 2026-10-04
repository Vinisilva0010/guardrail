"""add cash merger action type

Revision ID: 8fb60df867eb
Revises: a4d3b0a74c37
Create Date: 2026-10-04 20:26:19.113278

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8fb60df867eb'
down_revision: Union[str, Sequence[str], None] = 'a4d3b0a74c37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the cash_merger value to the corporate action enum.

    Written by hand: Alembic does not autogenerate enum value additions.
    """
    op.execute(
        "ALTER TYPE corporate_action_type ADD VALUE IF NOT EXISTS 'cash_merger'"
    )


def downgrade() -> None:
    """No-op.

    Postgres cannot remove a value from an enum type. Reverting would mean
    recreating the type and rewriting every column that uses it, which is not
    worth it for an additive change: an unused value is harmless.
    """
