"""add event kind pattern table

Revision ID: a93d5e7f1c46
Revises: e51a7c3d9b02
Create Date: 2026-10-15 02:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a93d5e7f1c46'
down_revision: Union[str, Sequence[str], None] = 'e51a7c3d9b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (priority, kind, Python regex matched case-insensitively against the title).
# Lowest priority wins, so "FOMC Member X Speaks" is a speech before any rate
# word in the title can make it a rate decision.
SEED = [
    (10, 'central_bank_speech', r"\b(speaks|testifies|testimony|press conference|meeting minutes|beige book)\b"),
    (20, 'auction', r"\bauctions?\b"),
    (30, 'rate_decision', r"\b(federal funds rate|interest rate|rate statement|rate decision|monetary policy (statement|report)|official cash rate|bank rate|main refinancing rate|fomc statement|cash rate|policy rate|overnight rate)\b"),
    (40, 'inflation_print', r"\b(cpi|ppi|wpi|pce|inflation|price index|import prices|export prices|deflator)\b"),
    (50, 'jobs_print', r"\b(non-?farm|employment change|unemployment|jobless|claimant|jolts|job openings|adp|average hourly earnings|payrolls|employment cost)\b"),
    (60, 'gdp_print', r"\bgdp\b"),
    (70, 'activity_print', r"\b(retail sales|pmi|manufacturing|services index|industrial production|consumer confidence|consumer sentiment|housing|home sales|building permits|trade balance|durable goods|ism|capacity utilization|business inventories|empire state|philly fed|factory orders|leading index|current account|budget balance|crude oil inventories)\b"),
]


def upgrade() -> None:
    """Create the pattern table and seed it.

    Written by hand: the enum type already exists and must not be created again.
    """
    kind = postgresql.ENUM(name='event_kind', create_type=False)
    patterns = op.create_table('event_kind_pattern',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('pattern', sa.Text(), nullable=False),
    sa.Column('kind', kind, nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_event_kind_pattern')),
    sa.UniqueConstraint('pattern', name=op.f('uq_event_kind_pattern_pattern'))
    )
    op.bulk_insert(patterns, [
        {'priority': p, 'kind': k, 'pattern': r} for p, k, r in SEED
    ])


def downgrade() -> None:
    """Drop the table. The seed is recreated by the upgrade."""
    op.drop_table('event_kind_pattern')
