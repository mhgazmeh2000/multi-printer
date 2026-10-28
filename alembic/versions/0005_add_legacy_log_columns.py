# -*- coding: utf-8 -*-
# =============================================================================
# @file:  alembic/versions/0005_add_legacy_log_columns.py
# @brief: Alembic migration 0005: adds five legacy columns to the logs table.
# @notes: Alembic revision kept as migration history. Do not edit it; schema
#         changes must go into a new revision.
# =============================================================================

"""
0005_add_legacy_log_columns — add 5 legacy columns to the logs table (phase F.5).

Legacy columns — unknown origin, zero data value.
Goal: alignment with the production schema. Removal in phase L.

Background:
-----------
During the review of phase F.4 and the check of the real production database files
(migration_sample/logs_sample.json), it was found that the logs table in the production SQLite
database has 18 columns, while in the development environment and the PostgreSQL cluster (up to
migration 0004) the logs table had 13 columns.
Columns added:
  - sheets: INTEGER (100% of values are NULL in production)
  - raw_counters: TEXT (100% of values are NULL in production)
  - raw_delta: INTEGER (100% of values are NULL in production)
  - event_type: TEXT (100% of values are the constant 'UNKNOWN' in production)
  - duplex_mode: TEXT (100% of values are the constant 'unknown' in production)

None of these columns is used in any query or in the application logic (dead columns),
but to satisfy the full schema-match condition between source and target in the CLI migration
tool (phase F.1) they must exist in PostgreSQL, so that the migration does not stop.
After a successful migration these columns will be cleaned up under technical debt TD-022 in phase L.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the five legacy columns to the logs table."""
    with op.batch_alter_table("logs") as batch_op:
        batch_op.add_column(sa.Column("sheets", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("raw_counters", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("raw_delta", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("event_type", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("duplex_mode", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the five legacy columns from the logs table."""
    with op.batch_alter_table("logs") as batch_op:
        batch_op.drop_column("duplex_mode")
        batch_op.drop_column("event_type")
        batch_op.drop_column("raw_delta")
        batch_op.drop_column("raw_counters")
        batch_op.drop_column("sheets")
