# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/upsert.py
# @brief:        Backend-independent 'insert or replace' SQL builder.
# @details:      Responsibilities: Produce the upsert statement for SQLite and PostgreSQL from one
#                definition.
#                Main API: upsert_sql()
#                Side effects: none at import time
# @dependencies: internal: .connection
# @notes:        None.
# =============================================================================

"""
core.db.upsert — backend-independent "insert or replace" statement (phase E.5).

Why was this helper necessary?
---------------------
``INSERT OR REPLACE'' is the SQLite syntax. On PostgreSQL it breaks:
``syntax error at or near "OR"'' (SQLSTATE 42601 — measured at the beginning of E.5,
20 failures from just this one pattern). Replace the PostgreSQL statement
``INSERT... ON CONFLICT (key) DO UPDATE SET''.

The meaning of the two expressions **when all columns are written** is the same:
  • SQLite: the old row is deleted and the new row is inserted ⇒ column not written to
    Default returns;
  • PG: row is updated in-place ⇒ unwritten column keep **previous value**
    takes
So this helper is intentionally made only for sentences that **all the columns of the table**
They write (`docs/insert-or-replace-inventory.md` §3.1 — the same three places as in E.5
were transferred). For sentences with incomplete columns, careless translation means changing the meaning of the data
(Section 3.2 of the same document, dead code, consciously untouched).

Rule E.5: Program code (``core/'' and ``web/'') No manual ``INSERT OR REPLACE''
does not write; It uses this helper (lock: ``tests/test_upsert_cross_backend.py'').
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .connection import BACKEND_POSTGRESQL, quote_ident, resolve_backend

#: Place the column view inserted in ``DO UPDATE SET'' clause of Postgres.
_EXCLUDED = "EXCLUDED"


def _ident_list(names: Iterable[str]) -> str:
    """Quote a list of identifiers and join them with commas."""
    return ", ".join(quote_ident(n) for n in names)


def upsert_sql(
    table: str,
    columns: Sequence[str],
    conflict_columns: Sequence[str],
    *,
    values_sql: Optional[str] = None,
    backend: Optional[str] = None,
) -> str:
    """
    Enables the "insert or replace" statement for the backend.

    Args:
        table: table name (quoted).
        columns: The inserted columns, **in the same order as the parameters are given**.
        conflict_columns: the unique key on which the conflict is measured
            (on PG as ``ON CONFLICT'' target; on SQLite it is just validated).
        values_sql: ``VALUES'' body — if not given, a ``?'' per column
            is made For statements that have a constant value in the list
            (eg ``?,?, 'high',?``) must be given explicitly; In this case, length
            The list of columns and the number of view slots are intentionally not compared.
        backend: ``DB_BACKEND'' wrapper (mainly for testing).

    Returns:
        SQL text with ``?'' view placeholder (translation of placeholder is work of adapter).

    Raises:
        ValueError: if ``columns'' or ``conflict_columns'' are empty, or
            ``conflict_columns'' should not be a subset of ``columns''.
    """
    if not columns:
        raise ValueError("upsert_sql: فهرست ستون‌ها نمی‌تواند خالی باشد")
    if not conflict_columns:
        raise ValueError(
            "upsert_sql: کلید تعارض لازم است (روی PG هدف ON CONFLICT باید صریح باشد)")
    missing = [c for c in conflict_columns if c not in columns]
    if missing:
        raise ValueError(
            f"upsert_sql: ستون‌های تعارض در فهرست درج نیستند: {', '.join(missing)}")

    active = backend if backend is not None else resolve_backend()
    cols_sql = _ident_list(columns)
    values = values_sql if values_sql is not None else ", ".join(["?"] * len(columns))

    if active != BACKEND_POSTGRESQL:
        # SQLite path: byte by byte same historical statement (REPLACE = delete + insert).
        return f"INSERT OR REPLACE INTO {quote_ident(table)} ({cols_sql}) VALUES ({values})"

    update_cols = [c for c in columns if c not in conflict_columns]
    if not update_cols:
        # "Conflict ⇒ do nothing" is equivalent to DO NOTHING; Empty DO UPDATE SET is not allowed.
        return (
            f"INSERT INTO {quote_ident(table)} ({cols_sql}) VALUES ({values}) "
            f"ON CONFLICT ({_ident_list(conflict_columns)}) DO NOTHING"
        )
    assignments = ", ".join(
        f"{quote_ident(c)} = {_EXCLUDED}.{quote_ident(c)}" for c in update_cols
    )
    return (
        f"INSERT INTO {quote_ident(table)} ({cols_sql}) VALUES ({values}) "
        f"ON CONFLICT ({_ident_list(conflict_columns)}) DO UPDATE SET {assignments}"
    )


__all__ = ["upsert_sql"]
