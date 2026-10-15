"""add api quota table

Revision ID: c7e2f91a4b10
Revises: 8fb60df867eb
Create Date: 2026-10-15 01:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e2f91a4b10'
down_revision: Union[str, Sequence[str], None] = '8fb60df867eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the per-source request budget table.

    Written by hand: Alembic does not generate the CHECK constraints.
    """
    op.create_table('api_quota',
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('period', sa.Date(), nullable=False),
    sa.Column('used', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('budget', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('used >= 0', name=op.f('ck_api_quota_used_non_negative')),
    sa.CheckConstraint('budget > 0', name=op.f('ck_api_quota_budget_positive')),
    sa.PrimaryKeyConstraint('source', 'period', name=op.f('pk_api_quota'))
    )


def downgrade() -> None:
    """Drop the table. It holds counters only, never source data."""
    op.drop_table('api_quota')
