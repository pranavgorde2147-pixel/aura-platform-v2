"""add brain processing pipeline and application records

Creates the two tables that back the universal Brain processing pipeline
(``brain_processing_runs`` and ``brain_processing_operations``), the
application-owned record table (``app_records``) and the retention columns on
``brain_memories``.

Revision ID: c4e8f1a2b7d9
Revises: b7d4e1a9c2f3
Create Date: 2026-10-21 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c4e8f1a2b7d9"
down_revision = "b7d4e1a9c2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brain_processing_runs",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("ai_identity_id", sa.UUID(), nullable=True),
        sa.Column("source_app", sa.String(length=100), nullable=False),
        sa.Column("input_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("input_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "VALIDATED",
                "PROCESSING",
                "CLASSIFIED",
                "AWAITING_REVIEW",
                "READY_FOR_DELIVERY",
                "DELIVERING",
                "PARTIALLY_DELIVERED",
                "DELIVERED",
                "RETRY_PENDING",
                "FAILED",
                "CANCELLED",
                name="processingstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("source_reference", sa.String(length=1024), nullable=True),
        sa.Column("content_summary", sa.String(length=1000), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("analysis_mode", sa.String(length=32), nullable=True),
        sa.Column("confidence_kind", sa.String(length=32), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ai_identity_id"], ["ai_identities.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_brain_processing_runs_user_idempotency",
        ),
    )
    op.create_index(
        op.f("ix_brain_processing_runs_user_id"),
        "brain_processing_runs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_id"),
        "brain_processing_runs",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_source_app"),
        "brain_processing_runs",
        ["source_app"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_input_id"),
        "brain_processing_runs",
        ["input_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_idempotency_key"),
        "brain_processing_runs",
        ["idempotency_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_status"),
        "brain_processing_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_session_id"),
        "brain_processing_runs",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_occurred_at"),
        "brain_processing_runs",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_captured_at"),
        "brain_processing_runs",
        ["captured_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_review_required"),
        "brain_processing_runs",
        ["review_required"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_error_category"),
        "brain_processing_runs",
        ["error_category"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_runs_ai_identity_id"),
        "brain_processing_runs",
        ["ai_identity_id"],
        unique=False,
    )

    op.create_table(
        "brain_processing_operations",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("destination_app", sa.String(length=100), nullable=False),
        sa.Column("record_type", sa.String(length=100), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "READY",
                "DELIVERING",
                "DELIVERED",
                "SKIPPED",
                "AWAITING_AUTHORIZATION",
                "AWAITING_REVIEW",
                "RETRY_PENDING",
                "FAILED",
                "CANCELLED",
                name="operationstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("record_reference", sa.String(length=1024), nullable=True),
        sa.Column("requires_review", sa.Boolean(), nullable=False),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False),
        sa.Column("operation_metadata", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["brain_processing_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "destination_app",
            "record_type",
            "segment_index",
            name="uq_brain_processing_operations_target",
        ),
    )
    op.create_index(
        op.f("ix_brain_processing_operations_run_id"),
        "brain_processing_operations",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_id"),
        "brain_processing_operations",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_user_id"),
        "brain_processing_operations",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_destination_app"),
        "brain_processing_operations",
        ["destination_app"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_record_type"),
        "brain_processing_operations",
        ["record_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_category"),
        "brain_processing_operations",
        ["category"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_status"),
        "brain_processing_operations",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_processing_operations_error_code"),
        "brain_processing_operations",
        ["error_code"],
        unique=False,
    )

    op.create_table(
        "app_records",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("ai_identity_id", sa.UUID(), nullable=True),
        sa.Column("destination_app", sa.String(length=100), nullable=False),
        sa.Column("record_type", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("storage_type", sa.String(length=32), nullable=False),
        sa.Column("object_reference", sa.String(length=1024), nullable=True),
        sa.Column("source_run_id", sa.UUID(), nullable=True),
        sa.Column("source_segment_index", sa.Integer(), nullable=True),
        sa.Column("source_reference", sa.String(length=1024), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_policy", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ai_identity_id"], ["ai_identities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_run_id"], ["brain_processing_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "destination_app",
            "record_type",
            "dedupe_key",
            name="uq_app_records_user_destination_type_dedupe",
        ),
    )
    op.create_index(op.f("ix_app_records_id"), "app_records", ["id"], unique=False)
    op.create_index(
        op.f("ix_app_records_user_id"), "app_records", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_app_records_ai_identity_id"),
        "app_records",
        ["ai_identity_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_app_records_destination_app"),
        "app_records",
        ["destination_app"],
        unique=False,
    )
    op.create_index(
        op.f("ix_app_records_record_type"), "app_records", ["record_type"], unique=False
    )
    op.create_index(
        op.f("ix_app_records_object_reference"),
        "app_records",
        ["object_reference"],
        unique=False,
    )
    op.create_index(
        op.f("ix_app_records_source_run_id"), "app_records", ["source_run_id"], unique=False
    )
    op.create_index(
        op.f("ix_app_records_occurred_at"), "app_records", ["occurred_at"], unique=False
    )
    op.create_index(
        op.f("ix_app_records_expires_at"), "app_records", ["expires_at"], unique=False
    )
    op.create_index(
        op.f("ix_app_records_processing_status"),
        "app_records",
        ["processing_status"],
        unique=False,
    )

    with op.batch_alter_table("brain_memories") as batch_op:
        batch_op.add_column(
            sa.Column("retention_policy", sa.String(length=64), nullable=False, server_default="default")
        )
        batch_op.add_column(
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_index(
        op.f("ix_brain_memories_retention_policy"),
        "brain_memories",
        ["retention_policy"],
        unique=False,
    )
    op.create_index(
        op.f("ix_brain_memories_expires_at"), "brain_memories", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_brain_memories_expires_at"), table_name="brain_memories")
    op.drop_index(
        op.f("ix_brain_memories_retention_policy"), table_name="brain_memories"
    )
    with op.batch_alter_table("brain_memories") as batch_op:
        batch_op.drop_column("expires_at")
        batch_op.drop_column("retention_policy")

    op.drop_index(op.f("ix_app_records_processing_status"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_expires_at"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_occurred_at"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_source_run_id"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_object_reference"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_record_type"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_destination_app"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_ai_identity_id"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_user_id"), table_name="app_records")
    op.drop_index(op.f("ix_app_records_id"), table_name="app_records")
    op.drop_table("app_records")

    op.drop_index(
        op.f("ix_brain_processing_operations_error_code"),
        table_name="brain_processing_operations",
    )
    op.drop_index(op.f("ix_brain_processing_operations_status"), table_name="brain_processing_operations")
    op.drop_index(op.f("ix_brain_processing_operations_category"), table_name="brain_processing_operations")
    op.drop_index(op.f("ix_brain_processing_operations_record_type"), table_name="brain_processing_operations")
    op.drop_index(
        op.f("ix_brain_processing_operations_destination_app"),
        table_name="brain_processing_operations",
    )
    op.drop_index(op.f("ix_brain_processing_operations_user_id"), table_name="brain_processing_operations")
    op.drop_index(op.f("ix_brain_processing_operations_id"), table_name="brain_processing_operations")
    op.drop_index(op.f("ix_brain_processing_operations_run_id"), table_name="brain_processing_operations")
    op.drop_table("brain_processing_operations")

    op.drop_index(
        op.f("ix_brain_processing_runs_ai_identity_id"), table_name="brain_processing_runs"
    )
    op.drop_index(op.f("ix_brain_processing_runs_error_category"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_review_required"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_captured_at"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_occurred_at"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_session_id"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_status"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_idempotency_key"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_input_id"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_source_app"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_id"), table_name="brain_processing_runs")
    op.drop_index(op.f("ix_brain_processing_runs_user_id"), table_name="brain_processing_runs")
    op.drop_table("brain_processing_runs")
