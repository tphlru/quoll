"""side pointer for side steps, agreement on step passes

Revision ID: ba9d7907a823
Revises: 76960583e5cc
Create Date: 2026-09-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'ba9d7907a823'
down_revision: Union[str, Sequence[str], None] = '76960583e5cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_HISTORY_KINDS = (
    "TRANSITION", "CLOSE", "REOPEN", "RELOCATION", "REJECTION", "ROLLBACK",
    "CANCEL", "PAUSE", "UNPAUSE", "BRANCH_ADDED", "BRANCH_REMOVED", "SA_OPENED",
    "SA_APPROVED", "SA_REJECTED", "LICENSE_EXTENDED", "CONTRACT_EXTENDED",
    "IMPORT", "COMMENT", "SLOT_PASSIVE", "SLOT_ACTIVE",
    "SA_CANCELLED", "SA_RETURNED", "RESTART",
)
SIDE_HISTORY_KINDS = ("SIDE_STARTED", "SIDE_FINISHED", "SIDE_CANCELLED")


def _kinds(kinds):
    return "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    # --- side_pointers
    op.create_table(
        'side_pointers',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('interaction_id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.BigInteger(), nullable=True),
        sa.Column('entry_stage_id', sa.Integer(), nullable=False),
        sa.Column('stage_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='ACTIVE', nullable=False),
        sa.Column('started_by', sa.String(length=255), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_by', sa.String(length=255), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finish_comment', sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('ACTIVE', 'FINISHED', 'CANCELLED')", name=op.f('chk_side_pointer_status')),
        sa.CheckConstraint("(status = 'ACTIVE') = (finished_at IS NULL)", name=op.f('chk_side_pointer_finished')),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], name=op.f('fk_side_pointers_branch_id_branches'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['entry_stage_id'], ['stages.id'], name=op.f('fk_side_pointers_entry_stage_id_stages'), ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['finished_by'], ['users.id'], name=op.f('fk_side_pointers_finished_by_users'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['interaction_id'], ['interactions.id'], name=op.f('fk_side_pointers_interaction_id_interactions'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['stage_id'], ['stages.id'], name=op.f('fk_side_pointers_stage_id_stages'), ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['started_by'], ['users.id'], name=op.f('fk_side_pointers_started_by_users'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_side_pointers')),
    )
    op.create_index(op.f('ix_side_pointers_interaction_id'), 'side_pointers', ['interaction_id'], unique=False)
    op.create_index('uq_side_pointers_active', 'side_pointers', ['interaction_id'], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))

    # --- stages: is_parallel -> is_side, + handler
    op.drop_index('uq_stages_one_parallel', table_name='stages', postgresql_where=sa.text('is_side AND archived_at IS NULL'))
    op.drop_constraint('chk_stage_parallel', 'stages', type_='check')
    op.alter_column('stages', 'is_parallel', new_column_name='is_side')
    op.add_column('stages', sa.Column('handler', sa.String(length=40), nullable=True))
    op.execute("UPDATE stages SET handler = 'SUPPLEMENTARY_AGREEMENT' WHERE is_side")
    op.create_check_constraint(
        'chk_stage_side', 'stages', 'NOT is_side OR (NOT is_terminal AND NOT is_branch_start)'
    )
    op.create_check_constraint(
        'chk_stage_handler', 'stages',
        "handler IS NULL OR (handler <> '' AND is_side AND NOT is_branch_stage)",
    )
    op.create_index(
        'uq_stages_handler', 'stages', ['workflow_id', 'handler'], unique=True,
        postgresql_where=sa.text('handler IS NOT NULL AND archived_at IS NULL'),
    )

    # --- прохождение в существующих таблицах
    op.add_column('interaction_stage_history', sa.Column('side_pointer_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_interaction_stage_history_side_pointer_id'), 'interaction_stage_history', ['side_pointer_id'], unique=False)
    op.create_foreign_key(op.f('fk_interaction_stage_history_side_pointer_id_side_pointers'), 'interaction_stage_history', 'side_pointers', ['side_pointer_id'], ['id'], ondelete='CASCADE')

    op.add_column('interaction_stage_values', sa.Column('side_pointer_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_interaction_stage_values_side_pointer_id'), 'interaction_stage_values', ['side_pointer_id'], unique=False)
    op.create_foreign_key(op.f('fk_interaction_stage_values_side_pointer_id_side_pointers'), 'interaction_stage_values', 'side_pointers', ['side_pointer_id'], ['id'], ondelete='CASCADE')
    op.drop_index('uq_stage_values_interaction_stage_branch', table_name='interaction_stage_values', postgresql_nulls_not_distinct=True)
    op.create_index(
        'uq_stage_values_pass', 'interaction_stage_values',
        ['interaction_id', 'stage_id', 'branch_id', 'side_pointer_id'], unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.add_column('interaction_documents', sa.Column('side_pointer_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_interaction_documents_side_pointer_id'), 'interaction_documents', ['side_pointer_id'], unique=False)
    op.create_foreign_key(op.f('fk_interaction_documents_side_pointer_id_side_pointers'), 'interaction_documents', 'side_pointers', ['side_pointer_id'], ['id'], ondelete='CASCADE')

    # --- просьбы: SA_APPROVAL уходит, добавляется side_pointer_id
    op.execute("DELETE FROM interaction_requests WHERE kind = 'SA_APPROVAL'")
    op.drop_constraint('chk_request_sa', 'interaction_requests', type_='check')
    op.drop_constraint(op.f('fk_interaction_requests_supplementary_agreement_id_supplementary_agreements'), 'interaction_requests', type_='foreignkey')
    op.drop_column('interaction_requests', 'supplementary_agreement_id')
    op.drop_constraint('chk_request_kind', 'interaction_requests', type_='check')
    op.create_check_constraint('chk_request_kind', 'interaction_requests', "kind IN ('TRANSFER', 'CLOSE', 'TRANSITION')")
    op.add_column('interaction_requests', sa.Column('side_pointer_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_interaction_requests_side_pointer_id'), 'interaction_requests', ['side_pointer_id'], unique=False)
    op.create_foreign_key(op.f('fk_interaction_requests_side_pointer_id_side_pointers'), 'interaction_requests', 'side_pointers', ['side_pointer_id'], ['id'], ondelete='CASCADE')
    op.drop_index('uq_interaction_requests_pending', table_name='interaction_requests', postgresql_where=sa.text("status = 'PENDING'"), postgresql_nulls_not_distinct=True)
    op.create_index(
        'uq_interaction_requests_pending', 'interaction_requests',
        ['interaction_id', 'kind', 'branch_id', 'side_pointer_id'], unique=True,
        postgresql_where=sa.text("status = 'PENDING'"), postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        'chk_request_side_only_transition', 'interaction_requests',
        "side_pointer_id IS NULL OR (kind = 'TRANSITION' AND branch_id IS NULL)",
    )

    # --- ДС: статусы, side_pointer_id, stall_since уходит
    op.execute("UPDATE supplementary_agreements SET status = 'CANCELLED' WHERE status = 'REJECTED'")
    op.drop_constraint('chk_sa_status', 'supplementary_agreements', type_='check')
    op.create_check_constraint('chk_sa_status', 'supplementary_agreements', "status IN ('DRAFT', 'PENDING', 'APPROVED', 'CANCELLED')")
    op.drop_constraint('chk_sa_decided', 'supplementary_agreements', type_='check')
    op.create_check_constraint('chk_sa_decided', 'supplementary_agreements', "(status IN ('APPROVED', 'CANCELLED')) = (decided_at IS NOT NULL)")
    op.drop_column('supplementary_agreements', 'stall_since')
    op.add_column('supplementary_agreements', sa.Column('side_pointer_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_supplementary_agreements_side_pointer_id'), 'supplementary_agreements', ['side_pointer_id'], unique=False)
    op.create_foreign_key(op.f('fk_supplementary_agreements_side_pointer_id_side_pointers'), 'supplementary_agreements', 'side_pointers', ['side_pointer_id'], ['id'], ondelete='CASCADE')

    # --- история: новые виды событий
    op.drop_constraint('chk_stage_history_kind', 'interaction_stage_history', type_='check')
    op.create_check_constraint(
        'chk_stage_history_kind', 'interaction_stage_history', _kinds(OLD_HISTORY_KINDS + SIDE_HISTORY_KINDS)
    )


def downgrade() -> None:
    """Downgrade schema."""
    # данные, которых прежняя схема не выражает
    op.execute("DELETE FROM interaction_stage_values WHERE side_pointer_id IS NOT NULL")
    op.execute("DELETE FROM interaction_documents WHERE side_pointer_id IS NOT NULL")
    op.execute("DELETE FROM interaction_requests WHERE side_pointer_id IS NOT NULL")
    op.execute(
        "DELETE FROM interaction_stage_history WHERE side_pointer_id IS NOT NULL "
        "OR kind IN ('SIDE_STARTED', 'SIDE_FINISHED', 'SIDE_CANCELLED')"
    )

    op.drop_constraint('chk_stage_history_kind', 'interaction_stage_history', type_='check')
    op.create_check_constraint(
        'chk_stage_history_kind', 'interaction_stage_history', _kinds(OLD_HISTORY_KINDS)
    )

    op.drop_constraint(op.f('fk_supplementary_agreements_side_pointer_id_side_pointers'), 'supplementary_agreements', type_='foreignkey')
    op.drop_index(op.f('ix_supplementary_agreements_side_pointer_id'), table_name='supplementary_agreements')
    op.drop_column('supplementary_agreements', 'side_pointer_id')
    op.add_column('supplementary_agreements', sa.Column('stall_since', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint('chk_sa_decided', 'supplementary_agreements', type_='check')
    op.create_check_constraint('chk_sa_decided', 'supplementary_agreements', "(status IN ('APPROVED', 'REJECTED', 'CANCELLED')) = (decided_at IS NOT NULL)")
    op.drop_constraint('chk_sa_status', 'supplementary_agreements', type_='check')
    op.create_check_constraint('chk_sa_status', 'supplementary_agreements', "status IN ('DRAFT', 'PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')")

    op.drop_constraint('chk_request_side_only_transition', 'interaction_requests', type_='check')
    op.drop_index('uq_interaction_requests_pending', table_name='interaction_requests', postgresql_where=sa.text("status = 'PENDING'"), postgresql_nulls_not_distinct=True)
    op.create_index(
        'uq_interaction_requests_pending', 'interaction_requests',
        ['interaction_id', 'kind', 'branch_id'], unique=True,
        postgresql_where=sa.text("status = 'PENDING'"), postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint(op.f('fk_interaction_requests_side_pointer_id_side_pointers'), 'interaction_requests', type_='foreignkey')
    op.drop_index(op.f('ix_interaction_requests_side_pointer_id'), table_name='interaction_requests')
    op.drop_column('interaction_requests', 'side_pointer_id')
    op.drop_constraint('chk_request_kind', 'interaction_requests', type_='check')
    op.create_check_constraint('chk_request_kind', 'interaction_requests', "kind IN ('TRANSFER', 'CLOSE', 'TRANSITION', 'SA_APPROVAL')")
    op.add_column('interaction_requests', sa.Column('supplementary_agreement_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(op.f('fk_interaction_requests_supplementary_agreement_id_supplementary_agreements'), 'interaction_requests', 'supplementary_agreements', ['supplementary_agreement_id'], ['id'])
    op.create_check_constraint(
        'chk_request_sa', 'interaction_requests',
        "(kind = 'SA_APPROVAL') = (supplementary_agreement_id IS NOT NULL) "
        "AND (kind <> 'SA_APPROVAL' OR (target_stage_id IS NULL "
        "AND target_manager_id IS NULL AND transition_id IS NULL "
        "AND branch_id IS NULL AND close_reason_id IS NULL "
        "AND branch_close_reason_id IS NULL))",
    )

    op.drop_constraint(op.f('fk_interaction_documents_side_pointer_id_side_pointers'), 'interaction_documents', type_='foreignkey')
    op.drop_index(op.f('ix_interaction_documents_side_pointer_id'), table_name='interaction_documents')
    op.drop_column('interaction_documents', 'side_pointer_id')

    op.drop_index('uq_stage_values_pass', table_name='interaction_stage_values', postgresql_nulls_not_distinct=True)
    op.create_index(
        'uq_stage_values_interaction_stage_branch', 'interaction_stage_values',
        ['interaction_id', 'stage_id', 'branch_id'], unique=True, postgresql_nulls_not_distinct=True,
    )
    op.drop_constraint(op.f('fk_interaction_stage_values_side_pointer_id_side_pointers'), 'interaction_stage_values', type_='foreignkey')
    op.drop_index(op.f('ix_interaction_stage_values_side_pointer_id'), table_name='interaction_stage_values')
    op.drop_column('interaction_stage_values', 'side_pointer_id')

    op.drop_constraint(op.f('fk_interaction_stage_history_side_pointer_id_side_pointers'), 'interaction_stage_history', type_='foreignkey')
    op.drop_index(op.f('ix_interaction_stage_history_side_pointer_id'), table_name='interaction_stage_history')
    op.drop_column('interaction_stage_history', 'side_pointer_id')

    op.drop_index('uq_stages_handler', table_name='stages', postgresql_where=sa.text('handler IS NOT NULL AND archived_at IS NULL'))
    op.drop_constraint('chk_stage_handler', 'stages', type_='check')
    op.drop_constraint('chk_stage_side', 'stages', type_='check')
    op.drop_column('stages', 'handler')
    op.alter_column('stages', 'is_side', new_column_name='is_parallel')
    op.create_check_constraint(
        'chk_stage_parallel', 'stages',
        "NOT is_parallel OR (NOT is_branch_stage AND NOT is_terminal AND NOT is_branch_start)",
    )
    op.create_index('uq_stages_one_parallel', 'stages', ['workflow_id'], unique=True, postgresql_where=sa.text('is_parallel AND archived_at IS NULL'))

    op.drop_index('uq_side_pointers_active', table_name='side_pointers', postgresql_where=sa.text("status = 'ACTIVE'"))
    op.drop_index(op.f('ix_side_pointers_interaction_id'), table_name='side_pointers')
    op.drop_table('side_pointers')
