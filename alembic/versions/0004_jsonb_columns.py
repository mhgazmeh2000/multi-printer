# -*- coding: utf-8 -*-
# =============================================================================
# @file:  alembic/versions/0004_jsonb_columns.py
# @brief: Alembic migration 0004: converts logs.details to jsonb on PostgreSQL.
# @notes: Alembic revision kept as migration history. Do not edit it; schema
#         changes must go into a new revision.
# =============================================================================

"""
0004_jsonb_columns — ``logs.details`` on PostgreSQL ⇒ ``jsonb`` (phase E.6.4).

Why?
----
The owner's criterion for this decision was "``grep json_extract``": a column goes to JSONB if
**SQL** reads JSON content from it. The result of the grep (and the only result):

    web/routes/metrics.py        details->>'detection'      (GROUP BY over all cartridge events)
    core/database.py             details->>'event_id' / 'prev_total' / 'current_total'  (dedup)

That means only ``logs.details``. The four other JSON columns (``users.allowed_offices``,
``users.allowed_modules``, ``printer_counters.cartridge_id_state``, ``printer_counters.toner_overrides``)
are never read as JSON in SQL (only ``json.dumps``/``json.loads`` in Python); converting them would add
migration risk and divergence from SQLite for no benefit, so they **stay TEXT** (decision and measured
figure in ``docs/phase-e-decisions.md``, row E.6.4).

Today on PG the helper ``core.db.ConnectionAdapter.json_extract`` builds ``(details::jsonb)->>'key'``,
meaning **every scanned row is parsed once per query**.
Measured on this same server (PG 16.15, 200,000 rows, 133,000 rows of ``CARTRIDGE_CHANGED``, the metrics query):

    TEXT + cast   ≈ 119.0 / 119.1 / 119.7 ms      (average 119.3)
    jsonb         ≈  46.5 /  46.3 /  47.5 ms      (average 46.8  ⇒ 2.5× faster)
    dedup (LIMIT 1): 19.3 ms ⇒ 17.8 ns (negligible — as expected, since it works on a few rows of one
                     printer within 5 minutes)
    an expression index on TEXT did not help: 116 ms (the plan stays a seq scan)
    writing 20,000 rows: text 79 ms ⇒ jsonb 36 ms   table size: both 40 MB

The metrics query runs on every Prometheus scrape, so this cost repeats. The second benefit is
**validation at write time**: today a row with invalid ``details`` (manual/legacy) breaks the whole
query with a cast error; with jsonb that same row is rejected at *write* time, not at *read* time —
the error appears next to its cause.

On SQLite this migration is a **no-op**
-------------------------------------
SQLite has no JSON type; it stays ``TEXT`` and ``json_extract`` works. This migration only checks that
the column really is text in SQLite (and stops with a clear message if not), so that the two backends
do not diverge from the "schema expectation". Readers accept both types: psycopg returns a ``jsonb``
value already decoded (a dict), and ``core/database.py::_row_to_dict`` accepts both.

Safety
------
• **Mandatory preflight:** before the ALTER, invalid rows are counted with a ``pg_temp`` function that
  has an ``EXCEPTION WHEN others`` handler (PG 16 has no ``IS JSON`` and no try-cast; this is the only
  exception-free way). If even one row is invalid ⇒ **stop**, with the count and the list of ids (the
  owner's decision in E.6.4: "stop with a count", not silently NULL).
• ``''`` and whitespace-only values become ``NULL``; this is a **data change**, so the migration gives
  a **warning with a count** before running (it is not skipped silently).
• **Idempotent:** if the column is already ``jsonb``, it is only checked and skipped.
• **Post-run monitoring:** the column type is read again from ``information_schema``; if it is not
  jsonb, the migration stops (we do not accept "done" without evidence).
• **Rollback:** ``downgrade`` goes back to ``text`` (``USING (details)::text``); rows that were ``''``
  stay ``NULL`` there. This is the only irreversible difference, and it is documented here, not hidden.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: The only columns this migration converts (decision E.6.4 — by the grep criterion).
#: If a new column is ever needed, this list and its lock test must change.
JSONB_COLUMNS = (
    ("logs", "details"),
)

#: The equivalent text type in SQLite (which does not change here; it is only checked).
SQLITE_TEXT_TYPES = ("TEXT", "")


def _is_postgres() -> bool:
    """Return True when the migration runs against PostgreSQL."""
    return op.get_bind().dialect.name == "postgresql"


def _exec(conn, sql: str):
    """
    Run SQL on an alembic (SQLAlchemy) connection **or** a raw psycopg connection.

    Why: the check/preflight functions of this migration run with ``op.get_bind()``, but the same
    logic (counting invalid JSON rows) must be tested in the test suite with a raw connection and a
    temporary table, without the migration being "rewritten" or copied for the tests. SQLAlchemy
    ``text()`` raises an error on a raw psycopg connection ("expected bytes, TextClause found";
    measured), so the connection type is detected.
    """
    if hasattr(conn, "dialect"):
        return conn.execute(sa.text(sql))
    return conn.execute(sql)


def _column_type(conn, table: str, column: str):
    """Column type from ``information_schema`` in the **current schema** (search_path)."""
    row = _exec(conn, "SELECT data_type FROM information_schema.columns "
                       "WHERE table_schema = current_schema() "
                       f"AND table_name = '{table}' AND column_name = '{column}'").fetchone()
    return None if row is None else row[0]


def _sqlite_column_decl(conn, table: str, column: str):
    """Column definition from ``sqlite_master`` (to make sure it is text in SQLite)."""
    row = _exec(conn, f"SELECT sql FROM sqlite_master WHERE type='table' "
                      f"AND name = '{table}'").fetchone()
    if row is None or not row[0]:
        return None
    for line in str(row[0]).splitlines():
        stripped = line.strip().strip(",")
        if stripped.lower().startswith(f'"{column}"') or stripped.lower().startswith(
                f"{column} ") or stripped.lower().startswith(f"{column}\" "):
            return stripped
    return None


def _invalid_where(table: str, column: str) -> str:
    """Condition for an "invalid row" — used for counting and for the list of ids (one definition, so they cannot diverge)."""
    return (f'"{column}" IS NOT NULL AND btrim("{column}") <> \'\' '
            f'AND NOT pg_temp.pg_json_is_valid("{column}")')


def _empty_where(table: str, column: str) -> str:
    """Condition for an "empty row": empty string or whitespace, which the migration converts to NULL."""
    return f'"{column}" IS NOT NULL AND btrim("{column}") = \'\''


_PG_TEMP_VALIDATOR = """
CREATE OR REPLACE FUNCTION pg_temp.pg_json_is_valid(t text) RETURNS boolean AS $$
BEGIN
    PERFORM t::jsonb;
    RETURN true;
EXCEPTION WHEN others THEN
    RETURN false;
END;
$$ LANGUAGE plpgsql
"""


def _preflight(conn, table: str, column: str) -> None:
    """
    Count invalid/empty rows; invalid ⇒ stop (the owner's decision in E.6.4: NULL silently is not allowed).

    Why a pg_temp function with an exception handler: PG 16 has neither ``IS JSON`` nor a try-cast,
    and ``details::jsonb`` inside a ``WHERE`` raises an error by itself ⇒ this is the only way to
    "find the broken rows without breaking the query". The function lives in ``pg_temp`` and disappears
    at the end of this session/temporary schema (no permanent object is created).

    Why two queries instead of ``array_agg(id)[:20]``: inside ``text()`` SQLAlchemy reads the ``:20``
    as a bind parameter (measured: "A value is required for bind parameter '20'"), so the list of ids
    is fetched separately with ``LIMIT``.
    """
    _exec(conn, _PG_TEMP_VALIDATOR)
    try:
        bad_where = _invalid_where(table, column)
        bad_count = int(_exec(conn, f'SELECT count(*) FROM "{table}" WHERE {bad_where}')
                        .fetchone()[0])
        if bad_count:
            ids = [str(r[0]) for r in _exec(
                conn, f'SELECT id FROM "{table}" WHERE {bad_where} ORDER BY id LIMIT 20'
            ).fetchall()]
            more = "…" if bad_count > len(ids) else ""
            raise RuntimeError(
                f"0004_jsonb_columns: {bad_count} ردیف در «{table}.{column}» JSON معتبر "
                f"نیستند ⇒ migration متوقف شد (تصمیم E.6.4: داده بی‌صدا دور ریخته نمی‌شود). "
                f"نمونه idها: {', '.join(ids)}{more}. برای یافتنشان: "
                f'SELECT id, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
                f"AND btrim(\"{column}\") <> '' AND btrim(\"{column}\") !~ '^[{{[]' ; "
                f"پس از اصلاح داده، دوباره `alembic upgrade head` را اجرا کنید."
            )
        empties = int(_exec(conn,
            f'SELECT count(*) FROM "{table}" WHERE {_empty_where(table, column)}'
        ).fetchone()[0])
        if empties:
            log.warning(
                "0004_jsonb_columns: %d ردیف در «%s.%s» رشتهٔ خالی دارند و با این "
                "migration NULL می‌شوند (تغییر داده؛ عمداً پیش از اجرا هشدار داده "
                "می‌شود).", empties, table, column)
    finally:
        _exec(conn, "DROP FUNCTION IF EXISTS pg_temp.pg_json_is_valid(t text)")


def upgrade() -> None:
    """Validate existing rows, then convert logs.details to jsonb on PostgreSQL; verify TEXT on SQLite."""
    conn = op.get_bind()

    if not _is_postgres():
        for table, column in JSONB_COLUMNS:
            decl = _sqlite_column_decl(conn, table, column)
            if decl is None:
                raise RuntimeError(
                    f"0004_jsonb_columns: جدول «{table}» یا ستون «{column}» در SQLite "
                    "پیدا نشد ⇒ schema با انتظار این migration هم‌خوان نیست."
                )
            upper = decl.upper()
            if not any(t in upper for t in ("TEXT", "VARCHAR", "CHAR")):
                raise RuntimeError(
                    f"0004_jsonb_columns: ستون «{table}.{column}» در SQLite متنی نیست "
                    f"({decl!r}) — انتظار TEXT بود؛ بررسی کنید چه چیزی این‌جا را عوض کرده."
                )
        log.info(
            "0004_jsonb_columns: SQLite ⇒ no-op (JSONB ندارد؛ %d ستون به‌عنوان TEXT "
            "بررسی و تأیید شد).", len(JSONB_COLUMNS))
        return

    for table, column in JSONB_COLUMNS:
        current = _column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0004_jsonb_columns: ستون «{table}.{column}» در اسکیمای جاری "
                f"({_exec(conn, 'SELECT current_schema()').fetchone()[0]}) نیست."
            )
        if current == "jsonb":
            log.info("0004_jsonb_columns: %s.%s از قبل jsonb است ⇒ رد شد.", table, column)
            continue
        if current not in ("text", "character varying"):
            raise RuntimeError(
                f"0004_jsonb_columns: انتظار ستون متنی بود، نوع «{table}.{column}» = "
                f"{current!r} ⇒ خودسرانه تبدیل نمی‌شود."
            )

        _preflight(conn, table, column)

        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE jsonb '
            f"USING (CASE WHEN \"{column}\" IS NULL OR btrim(\"{column}\") = '' "
            f"THEN NULL ELSE \"{column}\"::jsonb END)"
        )

        after = _column_type(conn, table, column)
        if after != "jsonb":
            raise RuntimeError(
                f"0004_jsonb_columns: پس از ALTER نوع «{table}.{column}» = {after!r} شد، "
                "نه 'jsonb'."
            )
        log.info("0004_jsonb_columns: %s.%s ⇒ jsonb (اعتبار JSON از این پس در زمان "
                 "نوشتن بررسی می‌شود، نه در زمان خواندن).", table, column)


def downgrade() -> None:
    """Convert logs.details back to text on PostgreSQL; no-op on SQLite."""
    conn = op.get_bind()

    if not _is_postgres():
        log.info("0004_jsonb_columns (downgrade): SQLite ⇒ no-op.")
        return

    for table, column in JSONB_COLUMNS:
        current = _column_type(conn, table, column)
        if current is None:
            raise RuntimeError(
                f"0004_jsonb_columns (downgrade): ستون «{table}.{column}» در دیتابیس نیست."
            )
        if current != "jsonb":
            log.info("0004_jsonb_columns (downgrade): %s.%s jsonb نیست (%r) ⇒ رد شد.",
                     table, column, current)
            continue
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE text '
            f'USING (CASE WHEN "{column}" IS NULL THEN NULL ELSE "{column}"::text END)'
        )
        log.info("0004_jsonb_columns (downgrade): %s.%s ⇒ text. توجه: ردیف‌هایی که در "
                 "upgrade از '' به NULL تبدیل شده بودند در این حالت NULL می‌مانند.",
                 table, column)
