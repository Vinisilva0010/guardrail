"""add event reference asset table

Revision ID: b27c40d8e915
Revises: a93d5e7f1c46
Create Date: 2026-10-15 03:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b27c40d8e915'
down_revision: Union[str, Sequence[str], None] = 'a93d5e7f1c46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# PLANO_ANTECIPACAO §4: BTC, ETH, SOL, SPY, QQQ, IWM and, in Brazil, IBOV and
# USDBRL. Some have no candles yet; the analysis reports those as absent.
SEED = [
    ('binance', 'BTCUSDT'),
    ('binance', 'ETHUSDT'),
    ('binance', 'SOLUSDT'),
    ('alpaca', 'SPY'),
    ('alpaca', 'QQQ'),
    ('alpaca', 'IWM'),
    ('b3', 'IBOV'),
    ('fx', 'USDBRL'),
]


def upgrade() -> None:
    """Create the table and seed it."""
    table = op.create_table('event_reference_asset',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('venue', sa.String(length=32), nullable=False),
    sa.Column('symbol', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_reference_asset')),
    sa.UniqueConstraint('venue', 'symbol', name=op.f('uq_event_reference_asset_venue_symbol'))
    )
    op.bulk_insert(table, [{'venue': v, 'symbol': s} for v, s in SEED])


def downgrade() -> None:
    """Drop the table."""
    op.drop_table('event_reference_asset')
