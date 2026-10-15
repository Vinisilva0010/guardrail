"""add alert sent and watchlist tables

Revision ID: d4f81a6c2e37
Revises: b27c40d8e915
Create Date: 2026-10-15 03:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f81a6c2e37'
down_revision: Union[str, Sequence[str], None] = 'b27c40d8e915'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the delivered-alert log and the watchlist."""
    op.create_table('alert_sent',
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('dedupe_key', name=op.f('pk_alert_sent'))
    )
    op.create_table('watchlist',
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_watchlist_instrument_id_instrument'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instrument_id', name=op.f('pk_watchlist'))
    )


def downgrade() -> None:
    """Drop both. The alert log only prevents repeats; the watchlist is re-added
    with the watch command."""
    op.drop_table('watchlist')
    op.drop_table('alert_sent')
