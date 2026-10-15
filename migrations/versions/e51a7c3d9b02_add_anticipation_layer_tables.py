"""add anticipation layer tables

Revision ID: e51a7c3d9b02
Revises: c7e2f91a4b10
Create Date: 2026-10-14 22:40:48.334707

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e51a7c3d9b02'
down_revision: Union[str, Sequence[str], None] = 'c7e2f91a4b10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EVENT_KIND = ('rate_decision', 'inflation_print', 'jobs_print', 'gdp_print', 'activity_print', 'earnings', 'token_unlock', 'options_expiry', 'futures_expiry', 'lockup_expiry', 'dividend_ex', 'auction', 'central_bank_speech', 'holiday', 'other')
EVENT_SCOPE = ('global', 'us', 'br', 'crypto')
EVENT_IMPACT = ('high', 'medium', 'low')


def upgrade() -> None:
    """Create the anticipation layer.

    Edited by hand: the enum types are shared by several tables, so they are
    created once up front (create_type=False below) and dropped explicitly on
    downgrade, which Alembic never generates.
    """
    bind = op.get_bind()
    postgresql.ENUM(*EVENT_KIND, name='event_kind').create(bind, checkfirst=False)
    postgresql.ENUM(*EVENT_SCOPE, name='event_scope').create(bind, checkfirst=False)
    postgresql.ENUM(*EVENT_IMPACT, name='event_impact').create(bind, checkfirst=False)
    op.create_table('expectation',
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('horizon_year', sa.Integer(), nullable=False),
    sa.Column('published_on', sa.Date(), nullable=False),
    sa.Column('median', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('mean', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('stdev', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('n_respondents', sa.Integer(), nullable=True),
    sa.CheckConstraint('horizon_year BETWEEN 2000 AND 2100', name=op.f('ck_expectation_horizon_year_sane')),
    sa.CheckConstraint('n_respondents >= 0', name=op.f('ck_expectation_n_respondents_non_negative')),
    sa.CheckConstraint('stdev >= 0', name=op.f('ck_expectation_stdev_non_negative')),
    sa.PrimaryKeyConstraint('code', 'horizon_year', 'published_on', name=op.f('pk_expectation'))
    )
    op.create_table('macro_series',
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=True),
    sa.Column('scope', postgresql.ENUM('global', 'us', 'br', 'crypto', name='event_scope', create_type=False), nullable=False),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_macro_series'))
    )
    op.create_table('news_theme',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('label', sa.Text(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_news_theme'))
    )
    op.create_table('event_impact_stat',
    sa.Column('kind', postgresql.ENUM('rate_decision', 'inflation_print', 'jobs_print', 'gdp_print', 'activity_print', 'earnings', 'token_unlock', 'options_expiry', 'futures_expiry', 'lockup_expiry', 'dividend_ex', 'auction', 'central_bank_speech', 'holiday', 'other', name='event_kind', create_type=False), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('horizon', sa.String(length=4), nullable=False),
    sa.Column('n', sa.Integer(), nullable=False),
    sa.Column('median_signed', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('median_abs', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('p10', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('p90', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('pct_positive', sa.Numeric(precision=18, scale=8), nullable=True),
    sa.Column('window_start', sa.Date(), nullable=False),
    sa.Column('window_end', sa.Date(), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("horizon IN ('d1', 'd3', 'w1')", name=op.f('ck_event_impact_stat_horizon_allowed')),
    sa.CheckConstraint('n >= 0', name=op.f('ck_event_impact_stat_n_non_negative')),
    sa.CheckConstraint('p10 IS NULL OR p90 IS NULL OR p10 <= p90', name=op.f('ck_event_impact_stat_p10_le_p90')),
    sa.CheckConstraint('pct_positive IS NULL OR pct_positive BETWEEN 0 AND 1', name=op.f('ck_event_impact_stat_pct_positive_fraction')),
    sa.CheckConstraint('window_start <= window_end', name=op.f('ck_event_impact_stat_window_ordered')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_event_impact_stat_instrument_id_instrument'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('kind', 'instrument_id', 'horizon', name=op.f('pk_event_impact_stat'))
    )
    op.create_table('macro_point',
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('ref_date', sa.Date(), nullable=False),
    sa.Column('value', sa.Numeric(precision=28, scale=8), nullable=False),
    sa.ForeignKeyConstraint(['code'], ['macro_series.code'], name=op.f('fk_macro_point_code_macro_series'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('code', 'ref_date', name=op.f('pk_macro_point'))
    )
    op.create_table('news_theme_asset',
    sa.Column('theme_id', sa.BigInteger(), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=False),
    sa.Column('move_pct', sa.Numeric(precision=18, scale=8), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_news_theme_asset_instrument_id_instrument'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['theme_id'], ['news_theme.id'], name=op.f('fk_news_theme_asset_theme_id_news_theme'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('theme_id', 'instrument_id', name=op.f('pk_news_theme_asset'))
    )
    op.create_table('scheduled_event',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('kind', postgresql.ENUM('rate_decision', 'inflation_print', 'jobs_print', 'gdp_print', 'activity_print', 'earnings', 'token_unlock', 'options_expiry', 'futures_expiry', 'lockup_expiry', 'dividend_ex', 'auction', 'central_bank_speech', 'holiday', 'other', name='event_kind', create_type=False), nullable=False),
    sa.Column('scope', postgresql.ENUM('global', 'us', 'br', 'crypto', name='event_scope', create_type=False), nullable=False),
    sa.Column('instrument_id', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('has_time', sa.Boolean(), nullable=False),
    sa.Column('date_is_estimated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('impact', postgresql.ENUM('high', 'medium', 'low', name='event_impact', create_type=False), nullable=False),
    sa.Column('expected', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('previous', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('actual', sa.Numeric(precision=28, scale=8), nullable=True),
    sa.Column('unit', sa.String(length=16), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('identity_key', sa.String(length=64), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("identity_key ~ '^[0-9a-f]{32}$'", name=op.f('ck_scheduled_event_identity_key_format')),
    sa.CheckConstraint("kind NOT IN ('earnings', 'token_unlock') OR instrument_id IS NOT NULL", name=op.f('ck_scheduled_event_instrument_required')),
    sa.ForeignKeyConstraint(['instrument_id'], ['instrument.id'], name=op.f('fk_scheduled_event_instrument_id_instrument'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scheduled_event')),
    sa.UniqueConstraint('identity_key', name=op.f('uq_scheduled_event_identity_key'))
    )
    op.create_index('ix_scheduled_event_instrument_id_starts_at', 'scheduled_event', ['instrument_id', 'starts_at'], unique=False)
    op.create_index('ix_scheduled_event_scope_impact_starts_at', 'scheduled_event', ['scope', 'impact', 'starts_at'], unique=False)
    op.create_index('ix_scheduled_event_starts_at', 'scheduled_event', ['starts_at'], unique=False)
    op.create_table('news_theme_item',
    sa.Column('theme_id', sa.BigInteger(), nullable=False),
    sa.Column('catalyst_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['catalyst_id'], ['catalyst.id'], name=op.f('fk_news_theme_item_catalyst_id_catalyst'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['theme_id'], ['news_theme.id'], name=op.f('fk_news_theme_item_theme_id_news_theme'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('theme_id', 'catalyst_id', name=op.f('pk_news_theme_item'))
    )


def downgrade() -> None:
    """Drop the anticipation layer and its enum types.

    Every table here is derived or re-collectable from its source, so no
    irreplaceable data is lost.
    """
    op.drop_table('news_theme_item')
    op.drop_index('ix_scheduled_event_starts_at', table_name='scheduled_event')
    op.drop_index('ix_scheduled_event_scope_impact_starts_at', table_name='scheduled_event')
    op.drop_index('ix_scheduled_event_instrument_id_starts_at', table_name='scheduled_event')
    op.drop_table('scheduled_event')
    op.drop_table('news_theme_asset')
    op.drop_table('macro_point')
    op.drop_table('event_impact_stat')
    op.drop_table('news_theme')
    op.drop_table('macro_series')
    op.drop_table('expectation')
    bind = op.get_bind()
    postgresql.ENUM(name='event_impact').drop(bind, checkfirst=False)
    postgresql.ENUM(name='event_scope').drop(bind, checkfirst=False)
    postgresql.ENUM(name='event_kind').drop(bind, checkfirst=False)
