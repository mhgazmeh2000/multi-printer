# -*- coding: utf-8 -*-
"""Enforce dashboard ownership for panels and permissions.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table also rebuilds these tables on SQLite, where ALTER TABLE
    # cannot add a foreign-key constraint directly.
    with op.batch_alter_table("panels") as batch_op:
        batch_op.create_foreign_key(
            "fk_panels_dashboard_id_dashboards",
            "dashboards",
            ["dashboard_id"],
            ["id"],
            ondelete="CASCADE",
        )
    with op.batch_alter_table("dashboard_permissions") as batch_op:
        batch_op.create_foreign_key(
            "fk_dashboard_permissions_dashboard_id_dashboards",
            "dashboards",
            ["dashboard_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("dashboard_permissions") as batch_op:
        batch_op.drop_constraint(
            "fk_dashboard_permissions_dashboard_id_dashboards", type_="foreignkey"
        )
    with op.batch_alter_table("panels") as batch_op:
        batch_op.drop_constraint(
            "fk_panels_dashboard_id_dashboards", type_="foreignkey"
        )
