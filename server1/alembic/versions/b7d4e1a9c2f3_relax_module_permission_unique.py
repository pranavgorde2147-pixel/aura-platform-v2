"""relax module permission uniqueness to include the action

A module scope may grant several actions (for example ``memory:read`` and
``memory:write``). The original unique constraint only allowed one row per
``(user_id, installed_module_id, scope)``, which rejected the catalog's default
permission sets. Include ``permission`` so multiple actions per scope coexist.

Revision ID: b7d4e1a9c2f3
Revises: a1f2b3c4d5e6
Create Date: 2026-10-13 00:00:00.000000

"""
from __future__ import annotations

from alembic import op


revision = "b7d4e1a9c2f3"
down_revision = "a1f2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("module_permissions") as batch_op:
        batch_op.drop_constraint("uq_module_permissions_user_module_scope", type_="unique")
        batch_op.create_unique_constraint(
            "uq_module_permissions_user_module_scope_action",
            ["user_id", "installed_module_id", "scope", "permission"],
        )


def downgrade() -> None:
    with op.batch_alter_table("module_permissions") as batch_op:
        batch_op.drop_constraint("uq_module_permissions_user_module_scope_action", type_="unique")
        batch_op.create_unique_constraint(
            "uq_module_permissions_user_module_scope",
            ["user_id", "installed_module_id", "scope"],
        )
