"""add brain memories

Revision ID: a1f2b3c4d5e6
Revises: c382015c2dae
Create Date: 2026-10-12 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1f2b3c4d5e6'
down_revision = 'c382015c2dae'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'brain_memories',
        sa.Column('ai_identity_id', sa.UUID(), nullable=False),
        sa.Column('kind', sa.Enum('FACT', 'INTERPRETATION', 'EVENT', name='memorykind', native_enum=False), nullable=False),
        sa.Column('source', sa.Enum('USER', 'BRAIN', 'APP', name='memorysource', native_enum=False), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('summary', sa.String(length=512), nullable=True),
        sa.Column('source_app', sa.String(length=128), nullable=True),
        sa.Column('importance', sa.Integer(), nullable=False),
        sa.Column('embedding_status', sa.String(length=32), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['ai_identity_id'], ['ai_identities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_brain_memories_ai_identity_id'), 'brain_memories', ['ai_identity_id'], unique=False)
    op.create_index(op.f('ix_brain_memories_id'), 'brain_memories', ['id'], unique=False)
    op.create_index(op.f('ix_brain_memories_importance'), 'brain_memories', ['importance'], unique=False)
    op.create_index(op.f('ix_brain_memories_kind'), 'brain_memories', ['kind'], unique=False)
    op.create_index(op.f('ix_brain_memories_source'), 'brain_memories', ['source'], unique=False)
    op.create_index(op.f('ix_brain_memories_source_app'), 'brain_memories', ['source_app'], unique=False)
    op.create_index(op.f('ix_brain_memories_embedding_status'), 'brain_memories', ['embedding_status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_brain_memories_embedding_status'), table_name='brain_memories')
    op.drop_index(op.f('ix_brain_memories_source_app'), table_name='brain_memories')
    op.drop_index(op.f('ix_brain_memories_source'), table_name='brain_memories')
    op.drop_index(op.f('ix_brain_memories_kind'), table_name='brain_memories')
    op.drop_index(op.f('ix_brain_memories_importance'), table_name='brain_memories')
    op.drop_index(op.f('ix_brain_memories_id'), table_name='brain_memories')
    op.drop_index(op.f('ix_brain_memories_ai_identity_id'), table_name='brain_memories')
    op.drop_table('brain_memories')
