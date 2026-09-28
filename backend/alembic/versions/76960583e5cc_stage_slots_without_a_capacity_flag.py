"""stage slots without a capacity flag

Revision ID: 76960583e5cc
Revises: a68ba3165ea1
Create Date: 2026-09-28 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '76960583e5cc'
down_revision: Union[str, Sequence[str], None] = 'a68ba3165ea1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f('ix_stages_consumes_capacity'), table_name='stages')
    op.drop_constraint('chk_stage_terminal_no_capacity', 'stages', type_='check')
    op.drop_column('stages', 'consumes_capacity')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'stages',
        sa.Column(
            'consumes_capacity',
            sa.Boolean(),
            server_default=sa.text('true'),
            nullable=False,
        ),
    )
    op.execute('UPDATE stages SET consumes_capacity = false WHERE is_terminal')
    op.create_check_constraint(
        'chk_stage_terminal_no_capacity',
        'stages',
        'NOT (is_terminal = TRUE AND consumes_capacity = TRUE)',
    )
    op.create_index(
        op.f('ix_stages_consumes_capacity'), 'stages', ['consumes_capacity'], unique=False
    )
