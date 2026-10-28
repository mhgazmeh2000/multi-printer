# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/schema.py
# @brief:        Schema inspection helpers for PostgreSQL (table, column and boolean checks).
# @details:      Responsibilities: Report missing tables and boolean columns so the app can
#                validate its schema.
#                Main API: boolean_columns_of(), is_postgres(), schema_columns(),
#                present_tables(), missing_tables(), verify_postgres_tables()
#                Side effects: none at import time
# @dependencies: internal: .connection
# @notes:        None.
# =============================================================================

"""
core/db/schema.py — Schema validation on PostgreSQL (E.4.5).

Rule E.4.5
-------------
On **SQLite** each module creates its own tables with ``CREATE TABLE IF NOT EXISTS''
(The historical form of the project is necessary, because the database file can come from an old version).

This is forbidden on **PostgreSQL**: ``Alembic'' is the only source of DDL
(``alembic/versions/``). Keeping two copies of the same DDL (one in Python, one in
migration sooner or later diverges and overlooks the main reason for failures;
``INTEGER PRIMARY KEY AUTOINCREMENT'' is not PostgreSQL syntax at all.

So the PG path bootstrap functions only **verify**: if the table is not what it should be,
They give a clear error with a solution (silent failure is prohibited - the same lesson as
``init_security_audit'' gave: the error was only logged and the program continued).

Note: This module depends on ``connection'', not ``core/db/__init__'' — so
Import does not create a cycle.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .connection import BACKEND_POSTGRESQL, quote_ident, resolve_backend

#: Tables that make migration ``0001'' (11 items). These should be available on PG
#: to be; On SQLite, they are only based on comparison (tests).
APP_TABLES = (
    "logs",
    "printer_counters",
    "toner_history",
    "sensor_readings",
    "users",
    "security_events",
    "yield_samples",
    "toner_snapshots_v2",
    "cartridge_state",
    "cartridge_yield_profiles",
    "meta",
)

#: Tables that ``core.yield_engine.ensure_yield_tables'' builds on SQLite.
YIELD_TABLES = (
    "cartridge_state",
    "yield_samples",
    "toner_snapshots_v2",
    "cartridge_yield_profiles",
)

#: Tables that ``core.security_audit.init_security_audit'' builds on SQLite.
SECURITY_TABLES = ("security_events",)

#: Columns that have type ``BOOLEAN'' on PostgreSQL (migration ``0002'').
#: There is no BOOLEAN type on SQLite, and ``0/1'' is the canonical form, so this list
#: **Only** means to insert values from a SQLite file into PostgreSQL
#: (today's user: ``web/routes/import_db.py'' in E.4.8).
#: Matching this list with migration is locked in the test.
BOOLEAN_COLUMNS = (
    ("users", "is_verified"),
    ("users", "email_verified"),
    ("users", "is_active"),
    ("yield_samples", "accepted"),
    ("security_events", "success"),
)


def boolean_columns_of(table: str) -> tuple:
    """Boolean column names of a table (subset of ``BOOLEAN_COLUMNS'')."""
    return tuple(col for tbl, col in BOOLEAN_COLUMNS if tbl == table)


#: The table that Alembic itself creates (indicator of running migration).
ALEMBIC_TABLE = "alembic_version"


def is_postgres() -> bool:
    """Is the backend ``postgresql'' enabled? (no need to connect, from ``DB_BACKEND'')"""
    return resolve_backend() == BACKEND_POSTGRESQL


def schema_columns(conn, table: str) -> list:
    """
    The names of the columns of a table, independent of the backend.

    Why it's needed: ``PRAGMA table_info'' is just SQLite syntax, and on PostgreSQL with
    ``syntax error at or near "PRAGMA"'' breaks (measured, E.4.8). Any code that
    The list of columns needed to make a SQL statement should be taken from here.

    Args:
        conn: adapter connection (on both backends).
        table: name of the table.

    Returns:
        List of column names **in order of announcement** in the table; Missing table ⇒ ``[]''
        (same on both backends: neither PRAGMA nor ``information_schema'' for
        The non-existent table does not give an error).
    """
    if conn.backend == BACKEND_POSTGRESQL:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return [r[0] for r in rows]
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [r[1] for r in rows]


def present_tables(conn) -> set:
    """The names of the base tables in the current schema (on both backends; on SQLite from sqlite_master)."""
    if conn.backend == BACKEND_POSTGRESQL:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        ).fetchall()
        return {r[0] for r in rows}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


def missing_tables(conn, tables: Sequence[str]) -> list:
    """Subcategory of ``tables'' which is not available in the database."""
    present = present_tables(conn)
    return [t for t in tables if t not in present]


def verify_postgres_tables(conn, tables: Iterable[str], *, context: str) -> None:
    """
    Verifying the presence of tables on PostgreSQL (Alembic is the only source of DDL).

    Args:
        conn: adapter connection (its backend must be ``postgresql'').
        tables: the tables that must exist.
        context: the name of the callback, for clear error messages.

    Raises:
        RuntimeError: if no table — with detailed list and solution (``alembic upgrade head'').
    """
    missing = missing_tables(conn, sorted(set(tables)))
    if missing:
        raise RuntimeError(
            f"{context}: این جدول‌ها در PostgreSQL نیستند: {', '.join(missing)} — "
            "روی PostgreSQL اسکیما را Alembic می‌سازد (نه کد برنامه): "
            "«python -m alembic upgrade head» را با همان ALEMBIC_DB_URL اجرا کنید "
            "(E.4.5 — docs/postgresql-strictness.md بند ۱۲)."
        )


__all__ = [
    "ALEMBIC_TABLE",
    "APP_TABLES",
    "BOOLEAN_COLUMNS",
    "SECURITY_TABLES",
    "YIELD_TABLES",
    "boolean_columns_of",
    "is_postgres",
    "missing_tables",
    "present_tables",
    "schema_columns",
    "verify_postgres_tables",
]
