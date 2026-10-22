"""add resource registry fields to each personal Brain

Revision ID: f1a2b3c4d5e6
Revises: e6a49cbd1731
Create Date: 2026-10-22 01:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e6a49cbd1731"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personal_ai_brains",
        sa.Column("provisioning_status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("resource_manifest_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("model_configuration", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("inference_runtime_status", sa.String(32), nullable=False, server_default="unregistered"),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("compute_allocation", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("storage_resource_reference", sa.String(255), nullable=True),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("provisioning_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("inference_request_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("llm_runtime_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "personal_ai_brains",
        sa.Column("runtime_isolation", sa.String(48), nullable=True),
    )
    op.create_index(
        op.f("ix_personal_ai_brains_provisioning_status"),
        "personal_ai_brains",
        ["provisioning_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_ai_brains_inference_runtime_status"),
        "personal_ai_brains",
        ["inference_runtime_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_personal_ai_brains_llm_runtime_id"),
        "personal_ai_brains",
        ["llm_runtime_id"],
        unique=False,
    )

    op.execute(
        """
        UPDATE personal_ai_brains
        SET model_configuration = '{"model_id":"deployment_default"}'::json,
            storage_resource_reference = 'brain:' || ai_identity_id::text
        """
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_personal_ai_brains_llm_runtime_id"),
        table_name="personal_ai_brains",
    )
    op.drop_index(
        op.f("ix_personal_ai_brains_inference_runtime_status"),
        table_name="personal_ai_brains",
    )
    op.drop_index(
        op.f("ix_personal_ai_brains_provisioning_status"),
        table_name="personal_ai_brains",
    )
    op.drop_column("personal_ai_brains", "runtime_isolation")
    op.drop_column("personal_ai_brains", "llm_runtime_id")
    op.drop_column("personal_ai_brains", "inference_request_count")
    op.drop_column("personal_ai_brains", "provisioning_error")
    op.drop_column("personal_ai_brains", "last_health_check")
    op.drop_column("personal_ai_brains", "storage_resource_reference")
    op.drop_column("personal_ai_brains", "compute_allocation")
    op.drop_column("personal_ai_brains", "inference_runtime_status")
    op.drop_column("personal_ai_brains", "model_configuration")
    op.drop_column("personal_ai_brains", "resource_manifest_version")
    op.drop_column("personal_ai_brains", "provisioning_status")
