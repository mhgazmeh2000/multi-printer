"""Fail-closed AST validation and bounded, read-only reporting execution.

Not a replacement for a dedicated PostgreSQL role with SELECT on reporting views.
Only trusted, side-effect-free SQL functions are supported by the query builder.
"""
from __future__ import annotations

from contextlib import contextmanager
import time

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


MAX_REPORT_ROWS = 500
REPORT_TIMEOUT_SECONDS = 5
SAFE_FUNCTIONS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "NULLIF", "ABS",
    "ROUND", "FLOOR", "CEIL", "CAST", "LOWER", "UPPER", "LENGTH", "TRIM",
    "DATE", "DATE_TRUNC", "TIMESTAMP_TRUNC", "EXTRACT", "TIME_TO_STR",
    "AND", "OR", "CASE", "IF", "CONCAT", "CONCAT_WS", "SUBSTRING",
}


def sanitize_select(raw_sql: str, *, allowed_tables=None) -> str:
    """Accept one SELECT query (including SELECT-only CTEs), never arbitrary SQL.

    Table references are resolved by scope, not by words in strings or comments.
    Internal queries may reference only unqualified/public allowlisted relations.
    """
    # Import lazily: datasources.__init__ eagerly registers the PostgreSQL driver,
    # which itself imports this policy module. No registry access is needed here.
    from core.datasources.base import DataSourceSecurityError

    if not isinstance(raw_sql, str) or not raw_sql.strip() or len(raw_sql) > 50000:
        raise DataSourceSecurityError("متن کوئری نامعتبر یا بیش از حد طولانی است.")
    try:
        statements = sqlglot.parse(raw_sql, read="postgres")
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
            raise ValueError("one SELECT required")
        tree = statements[0]
        for node in tree.walk():
            if isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)):
                raise ValueError("write/lock statements prohibited")
            if isinstance(node, exp.Func) and node.sql_name() not in SAFE_FUNCTIONS:
                raise ValueError("function not approved")
            if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.USERDEFINED:
                raise ValueError("custom type not approved")
        table_count = 0
        scopes = traverse_scope(tree)
        if not scopes:
            raise ValueError("unsupported query")
        for scope in scopes:
            for source in scope.sources.values():
                if not isinstance(source, exp.Table):
                    continue  # Derived table/CTE: its own scope is checked separately.
                if not isinstance(source.this, exp.Identifier):
                    raise ValueError("table functions not approved")
                table_count += 1
                if allowed_tables is not None and (
                    source.name.lower() not in allowed_tables
                    or source.catalog
                    or source.db.lower() not in ("", "public")
                ):
                    raise ValueError("table not approved")
        if allowed_tables is not None and not table_count:
            raise ValueError("approved table required")
        # Remove comments: they must never affect security or LIMIT handling.
        for node in tree.walk():
            node.comments = None
        # An outer bound also covers user LIMIT ALL, negative/large limits and UNIONs.
        return exp.select("*").from_(tree.subquery("report_rows")).limit(MAX_REPORT_ROWS).sql(dialect="postgres")
    except DataSourceSecurityError:
        raise
    except Exception as exc:
        raise DataSourceSecurityError("فقط SELECT محدود با جدول‌ها و توابع گزارش‌گیری مجاز پذیرفته می‌شود.") from exc


@contextmanager
def readonly_reporting(conn):
    """Use a dedicated connection; callers must not pass a write transaction.

    PostgreSQL protections are transaction-local. SQLite settings are restored so
    isolated tests and existing callers can reuse their connection safely.
    """
    if getattr(conn, "backend", None) == "postgresql":
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        conn.execute("SET LOCAL lock_timeout = '1s'")
        yield
    else:
        raw = getattr(conn, "raw_connection", conn)
        previous = raw.execute("PRAGMA query_only").fetchone()[0]
        deadline = time.monotonic() + REPORT_TIMEOUT_SECONDS
        raw.execute("PRAGMA query_only = ON")
        raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            yield
        finally:
            raw.set_progress_handler(None, 0)
            raw.execute("PRAGMA query_only = " + str(int(previous)))
