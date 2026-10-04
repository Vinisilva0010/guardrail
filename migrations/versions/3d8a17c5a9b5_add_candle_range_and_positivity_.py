"""add candle range and positivity constraints

Revision ID: 3d8a17c5a9b5
Revises: 69f75ad34814
Create Date: 2026-10-04 14:37:49.683577

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d8a17c5a9b5'
down_revision: Union[str, Sequence[str], None] = '69f75ad34814'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Written by hand: Alembic does not autogenerate CHECK constraints, the same
    blind spot it has for enum types.
    """
    op.create_check_constraint(
        "open_within_range", "candle", "open BETWEEN low AND high"
    )
    op.create_check_constraint(
        "close_within_range", "candle", "close BETWEEN low AND high"
    )
    op.create_check_constraint("prices_positive", "candle", "low > 0")


def downgrade() -> None:
    """Downgrade schema."""
    # Short names here: the project naming convention in db/base.py prepends
    # "ck_candle_", so passing the full name would prefix it a second time.
    op.drop_constraint("prices_positive", "candle", type_="check")
    op.drop_constraint("close_within_range", "candle", type_="check")
    op.drop_constraint("open_within_range", "candle", type_="check")
