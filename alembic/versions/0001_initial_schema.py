# -*- coding: utf-8 -*-
# =============================================================================
# @file:  alembic/versions/0001_initial_schema.py
# @brief: Alembic baseline migration 0001: creates the initial SQLite-compatible schema.
# @notes: Alembic revision kept as migration history. Do not edit it; schema
#         changes must go into a new revision.
# =============================================================================

"""
0001_initial_schema — baseline migration for phase E.1.

What it creates
---------------
11 tables = the 10 application tables + the `meta` table (the only addition of phase E.0, task E.0.13),
plus 13 indexes. This migration reproduces the **final state of today's SQLite schema**. Columns that
were added to tables with `ALTER TABLE` until now (`users.sensor_*`, `printer_counters.toner_overrides`,
`cartridge_state.cycle_*`) are in this same DDL, because the baseline builds the final shape instead of
replaying the history.

Deliberate decisions
--------------------
• Types **stay as in today's SQLite**: timestamps are `TEXT` and booleans are `INTEGER`.
  Converting `TEXT → TIMESTAMPTZ` is the work of phase E.4 (explicit note in phase E.1).
• Two dual-dialect variants, so that one migration both reproduces SQLite exactly and has the
  right meaning on PG:
    - IDs: `BIGINT` on PG, `INTEGER` on SQLite. (`INTEGER` is required, because `AUTOINCREMENT`
      in SQLite is only allowed on `INTEGER PRIMARY KEY`; with `BIGINT` the schema would not
      match and autoincrement would not work.)
    - Floats: `DOUBLE PRECISION` on PG (8 bytes, same meaning as `REAL` in SQLite), `REAL` on
      SQLite (so the schema stays byte-for-byte the same; `FLOAT` would have become `FLOAT`).
• No foreign keys (as today) and no data is inserted.
• `sqlite_sequence` is not created; SQLite creates it itself when `AUTOINCREMENT` is present.
  That is why 7 tables have an explicit `sqlite_autoincrement=True` (SQLAlchemy does not render
  this keyword by default; without it the DDL would not match today's program, and an insert
  without an id would get "id reuse" behaviour).

Intentional, documented differences from today's SQLite schema (both are "stricter and aligned with PG")
--------------------------------------------------------------------------------------------------------
1. 9 columns are explicitly `NOT NULL` in the migration: the seven `id` columns (AUTOINCREMENT keys) and
   two text keys `printer_counters.ip` and `cartridge_yield_profiles.cartridge_key`.
   In today's SQLite these are recorded with `notnull=0` (a SQLite property of key columns), but in
   practice they never receive NULL, and in PG a primary key is NOT NULL by definition.
   SQLAlchemy always writes NOT NULL for `primary_key=True` columns; removing it is not possible
   without dropping the primary-key constraint, which would change the meaning of the schema instead
   of reproducing it.
2. On PostgreSQL, IDs are created with `BIGSERIAL` (sequence + `nextval`), not
   `GENERATED … AS IDENTITY`. Both give the same autoincrement behaviour; the change to IDENTITY
   happens in phase E.4 (together with the type review), so that this baseline reproduces the
   "today's shape".

Important note for an existing database
---------------------------------------
On the existing SQLite server database (`logs.db`, which already has these tables) do NOT run
`upgrade head`; run `alembic stamp head` so the schema version is recorded. `upgrade head` is only
for an empty database (a fresh PostgreSQL or an empty SQLite file). Details are in
`docs/phase-e-plan.md`.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION

from alembic import op
import sqlalchemy as sa

# Unique identifier of this migration
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the baseline schema: tables and indexes matching the current SQLite layout."""
    op.create_table(
        "cartridge_state",
        sa.Column("printer_ip", sa.Text, primary_key=True),
        sa.Column("color", sa.Text, primary_key=True),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("cartridge_key", sa.Text, nullable=True),
        sa.Column("current_level", sa.Integer, nullable=True),
        sa.Column("last_valid_level", sa.Integer, nullable=True),
        sa.Column("last_counter", sa.Integer, nullable=True),
        sa.Column("anchor_counter", sa.Integer, nullable=True),
        sa.Column("anchor_level", sa.Integer, nullable=True),
        sa.Column("anchor_timestamp", sa.Text, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=True, server_default=sa.text('2000')),
        sa.Column("yield_source", sa.Text, nullable=True, server_default=sa.text("'default'")),
        sa.Column("confidence", sa.Text, nullable=True, server_default=sa.text("'low'")),
        sa.Column("sample_count", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("total_weight", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('0')),
        sa.Column("learned_at", sa.Text, nullable=True),
        sa.Column("last_refill_at", sa.Text, nullable=True),
        sa.Column("last_refill_counter", sa.Integer, nullable=True),
        sa.Column("pending_refill_level", sa.Integer, nullable=True),
        sa.Column("pending_refill_counter", sa.Integer, nullable=True),
        sa.Column("pending_refill_timestamp", sa.Text, nullable=True),
        sa.Column("cycle_start_counter", sa.Integer, nullable=True),
        sa.Column("cycle_start_level", sa.Integer, nullable=True),
        sa.Column("cycle_start_timestamp", sa.Text, nullable=True),
        sa.Column("zero_reached_counter", sa.Integer, nullable=True),
        sa.Column("zero_reached_timestamp", sa.Text, nullable=True),
        sa.Column("pages_after_zero", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("cycle_status", sa.Text, nullable=True, server_default=sa.text("'active'")),
        sa.Column("force_estimate", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("updated_at", sa.Text, nullable=True),
    )
    op.create_table(
        "cartridge_yield_profiles",
        sa.Column("cartridge_key", sa.Text, primary_key=True),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("color", sa.Text, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=False),
        sa.Column("confidence", sa.Text, nullable=False, server_default=sa.text("'high'")),
        sa.Column("sample_count", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("total_weight", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('0')),
        sa.Column("source_printer_ip", sa.Text, nullable=True),
        sa.Column("updated_at", sa.Text, nullable=True),
    )
    op.create_table(
        "logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("printer_name", sa.Text, nullable=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("pages", sa.Integer, nullable=True),
        sa.Column("color", sa.Text, nullable=True),
        sa.Column("code", sa.Text, nullable=True),
        sa.Column("severity", sa.Text, nullable=True),
        sa.Column("paper_size", sa.Text, nullable=True),
        sa.Column("username", sa.Text, nullable=True),
        sa.Column("details", sa.Text, nullable=True),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "printer_counters",
        sa.Column("ip", sa.Text, primary_key=True),
        sa.Column("device_type", sa.Text, nullable=True),
        sa.Column("print_total", sa.Integer, nullable=True),
        sa.Column("full_color", sa.Integer, nullable=True),
        sa.Column("black_white", sa.Integer, nullable=True),
        sa.Column("toner_level", sa.Integer, nullable=True),
        sa.Column("manual_override", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("override_color", sa.Text, nullable=True),
        sa.Column("override_base_level", sa.Integer, nullable=True),
        sa.Column("override_start_total", sa.Integer, nullable=True),
        sa.Column("override_start_toner", sa.Integer, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=True, server_default=sa.text('2000')),
        sa.Column("force_estimate", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("yield_learning_failures", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("last_alert_codes", sa.Text, nullable=True),
        sa.Column("a3_total", sa.Integer, nullable=True),
        sa.Column("a4_total", sa.Integer, nullable=True),
        sa.Column("alert_codes", sa.Text, nullable=True),
        sa.Column("cartridge_id_state", sa.Text, nullable=True),
        sa.Column("updated_at", sa.Text, nullable=True),
        sa.Column("toner_overrides", sa.Text, nullable=True, server_default=sa.text('NULL')),
    )
    op.create_table(
        "security_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False, server_default=sa.text("'info'")),
        sa.Column("user_identifier", sa.Text, nullable=True),
        sa.Column("user_id", sa.Integer, nullable=True),
        sa.Column("ip_address", sa.Text, nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("endpoint", sa.Text, nullable=True),
        sa.Column("success", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("details", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "sensor_readings",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True),
        sa.Column("unit", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=True),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "toner_history",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("print_total", sa.Integer, nullable=True),
        sa.Column("toner_level", sa.Integer, nullable=True),
        sa.Column("yield_per_page", sa.Integer, nullable=True),
        sa.Column("source", sa.Text, nullable=True, server_default=sa.text("'poll'")),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "toner_snapshots_v2",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("color", sa.Text, nullable=False),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("cartridge_key", sa.Text, nullable=True),
        sa.Column("timestamp", sa.Text, nullable=False),
        sa.Column("print_total", sa.Integer, nullable=True),
        sa.Column("full_color", sa.Integer, nullable=True),
        sa.Column("black_white", sa.Integer, nullable=True),
        sa.Column("usage_counter", sa.Integer, nullable=True),
        sa.Column("toner_level", sa.Integer, nullable=True),
        sa.Column("raw_level", sa.Integer, nullable=True),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("valid", sa.Integer, nullable=True, server_default=sa.text('1')),
        sa.Column("reject_reason", sa.Text, nullable=True),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text, nullable=False, unique=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=True),
        sa.Column("google_id", sa.Text, nullable=True, unique=True),
        sa.Column("reset_token_hash", sa.Text, nullable=True),
        sa.Column("reset_token_expires", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=False, server_default=sa.text("'viewer'")),
        sa.Column("is_verified", sa.Integer, nullable=False, server_default=sa.text('0')),
        sa.Column("email_verified", sa.Integer, nullable=False, server_default=sa.text('0')),
        sa.Column("is_active", sa.Integer, nullable=False, server_default=sa.text('1')),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
        sa.Column("last_login_at", sa.Text, nullable=True),
        sa.Column("allowed_offices", sa.Text, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("allowed_modules", sa.Text, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("sensor_alert_enabled", sa.Integer, nullable=False, server_default=sa.text('0')),
        sa.Column("sensor_temp_warning", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('30')),
        sa.Column("sensor_temp_critical", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('35')),
        sa.Column("sensor_hum_warning_high", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('70')),
        sa.Column("sensor_hum_critical_high", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('80')),
        sa.Column("sensor_hum_warning_low", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True, server_default=sa.text('20')),
        sa.Column("sensor_alert_cooldown_minutes", sa.Integer, nullable=True, server_default=sa.text('30')),
        sa.Column("sensor_alert_start_hour", sa.Integer, nullable=True, server_default=sa.text('0')),
        sa.Column("sensor_alert_end_hour", sa.Integer, nullable=True, server_default=sa.text('24')),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "yield_samples",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("printer_ip", sa.Text, nullable=False),
        sa.Column("color", sa.Text, nullable=False),
        sa.Column("printer_model", sa.Text, nullable=True),
        sa.Column("cartridge_name", sa.Text, nullable=True),
        sa.Column("cartridge_key", sa.Text, nullable=True),
        sa.Column("start_counter", sa.Integer, nullable=True),
        sa.Column("end_counter", sa.Integer, nullable=True),
        sa.Column("start_level", sa.Integer, nullable=True),
        sa.Column("end_level", sa.Integer, nullable=True),
        sa.Column("pages_delta", sa.Integer, nullable=True),
        sa.Column("toner_drop", sa.Integer, nullable=True),
        sa.Column("estimated_yield", sa.Integer, nullable=True),
        sa.Column("counter_basis", sa.Text, nullable=True),
        sa.Column("confidence_weight", sa.REAL().with_variant(DOUBLE_PRECISION, "postgresql"), nullable=True),
        sa.Column("accepted", sa.Integer, nullable=True, server_default=sa.text('1')),
        sa.Column("reject_reason", sa.Text, nullable=True),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=True),
        # AUTOINCREMENT in SQLite: exactly today's program DDL (and sqlite_sequence).
        sqlite_autoincrement=True,
    )
    op.create_table(
        "meta",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.Text, nullable=False),
    )

    # ─── Indexes (13 items, exactly the same names) ───
    op.create_index("idx_cartridge_state_key", "cartridge_state", ["cartridge_key"])
    op.create_index("idx_printer_ip", "logs", ["printer_ip"])
    op.create_index("idx_sec_event_type", "security_events", ["event_type"])
    op.create_index("idx_sec_ip", "security_events", ["ip_address"])
    op.create_index("idx_sec_timestamp", "security_events", ["timestamp"])
    op.create_index("idx_sec_user", "security_events", ["user_identifier"])
    op.create_index("idx_sensor_readings_ip_ts", "sensor_readings", ["printer_ip", "timestamp"])
    op.create_index("idx_sensor_readings_kind", "sensor_readings", ["kind"])
    op.create_index("idx_timestamp", "logs", ["timestamp"])
    op.create_index("idx_toner_history_ip_ts", "toner_history", ["printer_ip", "timestamp"])
    op.create_index("idx_toner_snapshots_v2_ip_color_ts", "toner_snapshots_v2", ["printer_ip", "color", "timestamp"])
    op.create_index("idx_type", "logs", ["type"])
    op.create_index("idx_yield_samples_key", "yield_samples", ["cartridge_key"])


def downgrade() -> None:
    """Full drop (indexes first, then tables) — `downgrade base` must run without error."""
    op.drop_index("idx_cartridge_state_key", table_name="cartridge_state")
    op.drop_index("idx_printer_ip", table_name="logs")
    op.drop_index("idx_sec_event_type", table_name="security_events")
    op.drop_index("idx_sec_ip", table_name="security_events")
    op.drop_index("idx_sec_timestamp", table_name="security_events")
    op.drop_index("idx_sec_user", table_name="security_events")
    op.drop_index("idx_sensor_readings_ip_ts", table_name="sensor_readings")
    op.drop_index("idx_sensor_readings_kind", table_name="sensor_readings")
    op.drop_index("idx_timestamp", table_name="logs")
    op.drop_index("idx_toner_history_ip_ts", table_name="toner_history")
    op.drop_index("idx_toner_snapshots_v2_ip_color_ts", table_name="toner_snapshots_v2")
    op.drop_index("idx_type", table_name="logs")
    op.drop_index("idx_yield_samples_key", table_name="yield_samples")
    op.drop_table("meta")
    op.drop_table("yield_samples")
    op.drop_table("users")
    op.drop_table("toner_snapshots_v2")
    op.drop_table("toner_history")
    op.drop_table("sensor_readings")
    op.drop_table("security_events")
    op.drop_table("printer_counters")
    op.drop_table("logs")
    op.drop_table("cartridge_yield_profiles")
    op.drop_table("cartridge_state")
