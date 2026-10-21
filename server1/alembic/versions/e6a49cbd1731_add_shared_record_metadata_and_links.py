"""add structured metadata and owner-scoped app record links

Revision ID: e6a49cbd1731
Revises: c4e8f1a2b7d9
Create Date: 2026-10-21 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "e6a49cbd1731"
down_revision = "c4e8f1a2b7d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_records",
        sa.Column("categories", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "app_records",
        sa.Column("topics", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "app_records",
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "app_records",
        sa.Column(
            "access_policy",
            sa.String(length=32),
            nullable=False,
            server_default="owner_only",
        ),
    )
    op.add_column(
        "app_records",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "app_record_links",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("related_record_id", sa.UUID(), nullable=True),
        sa.Column("target_app", sa.String(length=100), nullable=True),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(related_record_id IS NULL) <> (target_app IS NULL)",
            name="ck_app_record_links_one_target",
        ),
        sa.CheckConstraint(
            "related_record_id IS NULL OR related_record_id <> record_id",
            name="ck_app_record_links_no_self_reference",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["record_id"], ["app_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["related_record_id"], ["app_records.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_app_record_links_target_app_relationship",
        "app_record_links",
        ["user_id", "record_id", "target_app", "relation_type"],
        unique=True,
        postgresql_where=sa.text("related_record_id IS NULL"),
    )
    op.create_index(
        "uq_app_record_links_related_record_relationship",
        "app_record_links",
        ["user_id", "record_id", "related_record_id", "relation_type"],
        unique=True,
        postgresql_where=sa.text("target_app IS NULL"),
    )
    op.create_index(
        op.f("ix_app_record_links_user_id"),
        "app_record_links",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_app_record_links_record_id"),
        "app_record_links",
        ["record_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_app_record_links_related_record_id"),
        "app_record_links",
        ["related_record_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_app_record_links_target_app"),
        "app_record_links",
        ["target_app"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("uq_app_record_links_related_record_relationship", table_name="app_record_links")
    op.drop_index("uq_app_record_links_target_app_relationship", table_name="app_record_links")
    op.drop_index(op.f("ix_app_record_links_target_app"), table_name="app_record_links")
    op.drop_index(
        op.f("ix_app_record_links_related_record_id"), table_name="app_record_links"
    )
    op.drop_index(op.f("ix_app_record_links_record_id"), table_name="app_record_links")
    op.drop_index(op.f("ix_app_record_links_user_id"), table_name="app_record_links")
    op.drop_table("app_record_links")
    op.drop_column("app_records", "version")
    op.drop_column("app_records", "access_policy")
    op.drop_column("app_records", "metadata")
    op.drop_column("app_records", "topics")
    op.drop_column("app_records", "categories")
