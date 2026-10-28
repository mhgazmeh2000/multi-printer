# -*- coding: utf-8 -*-
# =============================================================================
# @file:  alembic/versions/0002_boolean_columns.py
# @brief: Alembic migration 0002: converts five flag columns to BOOLEAN on PostgreSQL.
# @notes: Alembic revision kept as migration history. Do not edit it; schema
#         changes must go into a new revision.
# =============================================================================

"""
0002_boolean_columns — convert five flag columns to ``BOOLEAN`` on PostgreSQL (phase E.4.1).

Why is this migration needed?
-----------------------------
Flag columns are currently `INTEGER 0/1` (both in today's SQLite schema and in migration `0001`).
On PostgreSQL this pattern works, but it loses the meaning of the type and forces writing `= 1` in
comparisons. Phase E.4 decided that these five columns get a **real BOOLEAN type** (`docs/phase-e-plan.md`,
item 5 of the E.6 decisions):

    users.is_verified · users.email_verified · users.is_active
    yield_samples.accepted · security_events.success

On SQLite this migration is a **no-op**
--------------------------------------
SQLite has no `BOOLEAN` type; every boolean value is stored as `INTEGER 0/1`, and this is SQLite's
own "canonical form". So today's SQLite schema stays unchanged and the program's behaviour does not
change. The Python code is also the same for both backends: Python ``True``/``False`` values are sent,
which sqlite3 writes as `1/0`, and psycopg maps to `boolean` (measured in E.4.1: writing an `int` to a
`boolean` column raises an error ⇒ all writers of these five columns were moved to `bool`).

Order of the three-step ALTER (measured, not guessed)
-----------------------------------------------------
`ALTER COLUMN … TYPE BOOLEAN` fails when the column has `DEFAULT 0/1`:

    ERROR: default for column "x" cannot be cast automatically to type boolean

So each column goes through three steps:

    1) DROP DEFAULT
    2) TYPE BOOLEAN USING ("col"::boolean)      — numeric cast: 0 ⇒ false, non-zero ⇒ true
    3) SET DEFAULT false/true                   — the same meaning as the previous default

`NOT NULL`/`nullable` stays unchanged (this migration only changes the type): the three strict
`NOT NULL` columns of `users` stay strict, and `yield_samples.accepted` and `security_events.success`
keep accepting NULL.

Execution safety
----------------
• **Idempotent:** if a column is already `boolean`, that column is skipped; so running it again (or
  running it on a database that was converted by hand) does not raise an error.
• **Explicit, not silent:** if a column does not exist, or its type is neither numeric nor
  `boolean`, the run stops with a clear message (instead of a guessed conversion).
• **Rollback path:** `downgrade` goes back through the same three steps in reverse (`BOOLEAN` ⇒
  `INTEGER` with `USING ("col"::int)` and restoring the `0/1` default).

Important note for later migrations
-----------------------------------
`0001` was intentionally left untouched (phase E rule: changing an earlier migration is forbidden).
That means a fresh install on PG starts from `0001` (INTEGER) and `0002` brings it to BOOLEAN,
the same path as an existing database.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

# Unique identifier of this migration
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: (table, column, new boolean default, previous numeric default)
BOOLEAN_COLUMNS = (
    ("users", "is_verified", False, 0),
    ("users", "email_verified", False, 0),
    ("users", "is_active", True, 1),
    ("yield_samples", "accepted", True, 1),
    ("security_events", "success", False, 0),
)

#: Type names that count as "numeric" (before conversion this is what is expected).
_INTEGER_TYPES = ("integer", "smallint", "bigint")


def _is_postgres() -> bool:
    """Return True when the migration runs against PostgreSQL."""
    return op.get_bind().dialect.name == "postgresql"


def _pg_column_type(conn, table: str, column: str):
    """Current column type from ``information_schema`` (``None`` if the column does not exist)."""
    row = conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row[0] if row else None


def _sqlite_has_column(conn, table: str, column: str) -> bool:
    """Does the column exist in the SQLite table? (Only a presence check, not the type.)"""
    rows = conn.execute(sa.text(f'PRAGMA table_info("{table}")')).fetchall()
    return any(r[1] == column for r in rows)


def _pg_convert(conn, table: str, column: str, to_boolean: bool, default) -> None:
    """
    Convert the type of one column on PG with the same three steps that were measured.

    ``DROP DEFAULT`` on a column that has no default also completes without an error, so there
    is no need to check the default separately.
    """
    target = "BOOLEAN" if to_boolean else "INTEGER"
    using = f'"{column}"::boolean' if to_boolean else f'"{column}"::int'
    if default is True:
        default_sql = "true"
    elif default is False:
        default_sql = "false"
    else:
        default_sql = str(default)

    op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE {target} '
        f"USING ({using})"
    )
    op.execute(
        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default_sql}'
    )


def upgrade() -> None:
    """Convert the flag columns to BOOLEAN on PostgreSQL; check presence only on SQLite."""
    conn = op.get_bind()

    if not _is_postgres():
        # ── SQLite: no-op ────────────────────────────────────────────────
        # SQLite has no BOOLEAN type; 0/1 is its canonical storage, and today's schema of the
        # program is the same. Only the **presence** of the columns is checked, so that an incomplete
        # schema does not pass this step silently (a clear error, not a guessed conversion).
        missing = [f"{t}.{c}" for t, c, _new, _old in BOOLEAN_COLUMNS
                   if not _sqlite_has_column(conn, t, c)]
        if missing:
            raise RuntimeError(
                "0002_boolean_columns: ستون‌های زیر پیدا نشدند: " + ", ".join(missing) +
                " — schema SQLite با انتظار این migration هم‌خوان نیست "
                "(ابتدا init_db/alembic 0001 را اجرا کنید)."
            )
        log.info(
            "0002_boolean_columns: SQLite ⇒ no-op (نوع BOOLEAN ندارد؛ 0/1 شکل "
            "کانونیک آن است). %d ستون بررسی و تأیید شد.", len(BOOLEAN_COLUMNS)
        )
        return

    for table, column, new_default, _old_default in BOOLEAN_COLUMNS:
        current = _pg_column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0002_boolean_columns: ستون «{table}.{column}» در دیتابیس نیست؛ "
                "schema با انتظار این migration هم‌خوان نیست."
            )
        if current == "boolean":
            log.info("0002_boolean_columns: %s.%s از قبل boolean است ⇒ رد شد.",
                     table, column)
            continue
        if current not in _INTEGER_TYPES:
            raise RuntimeError(
                f"0002_boolean_columns: نوع «{table}.{column}» = {current!r} است، "
                f"نه یکی از {_INTEGER_TYPES} — تبدیل خودکار بی‌صدا مجاز نیست."
            )
        # NOT NULL / nullable stays unchanged (this step only changes the type and the default).
        _pg_convert(conn, table, column, to_boolean=True, default=new_default)
        log.info("0002_boolean_columns: %s.%s ⇒ BOOLEAN (default=%s)",
                 table, column, new_default)


def downgrade() -> None:
    """Convert the BOOLEAN flag columns back to INTEGER 0/1 on PostgreSQL; no-op on SQLite."""
    conn = op.get_bind()

    if not _is_postgres():
        # As in upgrade: on SQLite there is nothing to revert.
        log.info("0002_boolean_columns (downgrade): SQLite ⇒ no-op.")
        return

    for table, column, _new_default, old_default in BOOLEAN_COLUMNS:
        current = _pg_column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0002_boolean_columns (downgrade): ستون «{table}.{column}» نیست."
            )
        if current in _INTEGER_TYPES:
            log.info("0002_boolean_columns (downgrade): %s.%s از قبل عددی است ⇒ رد شد.",
                     table, column)
            continue
        if current != "boolean":
            raise RuntimeError(
                f"0002_boolean_columns (downgrade): نوع «{table}.{column}» = {current!r} "
                "است، نه boolean — برگرداندن خودکار مجاز نیست."
            )
        _pg_convert(conn, table, column, to_boolean=False, default=old_default)
        log.info("0002_boolean_columns (downgrade): %s.%s ⇒ INTEGER (default=%s)",
                 table, column, old_default)
