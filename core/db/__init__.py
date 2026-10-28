# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/__init__.py
# @brief:        Data-layer entry point that selects the SQLite or PostgreSQL backend.
# @details:      Responsibilities: Expose get_connection() and the connection helpers used by the
#                application.
#                Main API: get_connection(), connect_to(), db_connection()
#                Side effects: none at import time
# @dependencies: internal: .connection, .exceptions, .placeholders, .schema, .scripts, .upsert
# @notes:        None.
# =============================================================================

"""
core.db — PrintGuard data layer (phase D: thin adapter; phase E.2: two backends).

The meaning of this package
--------------
A **wrapper** is thin: from outside (web/core/tools) no behavior is changed.
Map of phases:
  • Phase D: ``sqlite'' only (``UnsupportedBackendError'' guard for any other value).
  • Phase E.2 (right now): ``postgresql'' is also in ``SUPPORTED_BACKENDS'' and
    :func:`connect_postgres` connects with ``psycopg`` 3 driver (TimeZone=UTC locked).
    Placeholder translation is activated in this phase.
  • Phase E.3: Enable ``DB_BACKEND=postgresql'' in production + UTC-aware writers.
    Until then, the default development environment is still ``sqlite''.

Adaptation path
------------
``core.database.db_connection'' and ``core.security_audit._db_conn'' both to
This same module delegates, so:
  • Signature of functions and place of import in existing code **not changed**;
  • Possible ``monkeypatch'' tests on ``core.database.db_connection''
    They still work;
  • The backend is read from ``DB_BACKEND'' only at call time (not at
    import), so tests can change it and take effect immediately.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from .connection import (  # noqa: F401 (re-export for phase E consumers)
    ALL_BACKENDS,
    BACKEND_ALIASES,
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_DB_BACKEND,
    ENV_PG_DSN,
    TZ_OPTIONS,
    SUPPORTED_BACKENDS,
    ConnectionAdapter,
    connect,
    connect_postgres,
    connect_sqlite,
    default_db_path,
    dsn_from_env,
    quote_ident,
    resolve_backend,
    resolve_dsn,
    timezone_options,
)
from .exceptions import (  # noqa: F401
    ColumnAlreadyExists,
    ConfigurationError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseOperationalError,
    PlaceholderTranslationError,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
    UnsupportedBackendError,
    UnsupportedFeatureError,
)
from .placeholders import (  # noqa: F401
    PLACEHOLDER_STYLE,
    escape_percent_signs,
    translate_placeholders,
)
from .scripts import split_sql_statements  # noqa: F401 (E.4.7: script breaking)
from .upsert import upsert_sql  # noqa: F401 (E.5: insert or replace, independent of backend)
from .schema import (  # noqa: F401 (E.4.5: schema verification on PG)
    APP_TABLES,
    BOOLEAN_COLUMNS,
    SECURITY_TABLES,
    YIELD_TABLES,
    boolean_columns_of,
    is_postgres,
    missing_tables,
    present_tables,
    schema_columns,
    verify_postgres_tables,
)

log = logging.getLogger("PrinterMonitor")

#: The "backend decided" warning is printed only once during the lifetime of the process.
_BACKEND_LOGGED: Optional[str] = None


def _log_backend_once(backend: str) -> None:
    """Log the active database backend once per process."""
    global _BACKEND_LOGGED
    if _BACKEND_LOGGED == backend:
        return
    _BACKEND_LOGGED = backend
    log.info("core.db: backend دیتابیس = %s (DB_PATH=%s)", backend, default_db_path())


def get_connection(timeout: float = 30.0, commit: bool = False) -> ConnectionAdapter:
    """
    Creates a new connection to the default database (``config.settings.DB_PATH'').

    Args:
        timeout: lock timeout (seconds). Same meaning as ``sqlite3.connect(timeout=...)''.
        commit: **no effect** in this phase; Signature kept only for consistency
            and does not *commit* the connection. Transaction management in
            :func:`db_connection` is done.

    Raises:
        UnsupportedBackendError: If ``DB_BACKEND'' has an unknown value.
        ConfigurationError: if ``DB_BACKEND=postgresql'' and not ``PG_DSN''.
        DatabaseConnectionError: If connection creation fails.
    """
    return _open(path=None, timeout=timeout)


def connect_to(path: str, timeout: float = 30.0) -> ConnectionAdapter:
    """
    Connect to a **specified file** (not the default path) — for temporary/upload databases
    Like ``temp_import_*.db'' route in ``web/routes/import_db.py''.

    Note: The ``timeout'' parameter is explicitly sent by the calls rather than the previous behavior
    (``sqlite3.connect(temp_path)'' with the default of 5 seconds) should not be changed.

    **backend lock (E.6.1):** This helper opens "a file" and the ``.db'' file is
    SQLite — so the backend is explicitly ``sqlite'' and does not read the ``DB_BACKEND'' program.
    Previously, under ``DB_BACKEND=postgresql'' itself ``connect()'' with
    ``ConfigurationError'' ("path with postgresql") was thrown and
    ``/api/import/analyze'' fires with 500, while the E.4.8 guard explicitly disables analyze
    was left open ("The source file is always SQLite"). This lock implements the same intention
    And it doesn't change any writing path of the program.
    """
    return _open(path=path, timeout=timeout, backend=BACKEND_SQLITE)


def _open(path: Optional[str], timeout: float,
          backend: Optional[str] = None) -> ConnectionAdapter:
    """Create connection + log selected backend once."""
    conn = connect(path, timeout=timeout, backend=backend)
    _log_backend_once(conn.backend)
    return conn


@contextmanager
def db_connection(timeout: float = 30.0, commit: bool = False) -> Iterator[ConnectionAdapter]:
    """
    Context manager for secure connection to the database (adapter version).

    Compatibility with previous ``core.database.db_connection'':
      • return error ⇒ ``commit'' only if ``commit=True'';
      • Error occurrence ⇒ ``rollback'' (only in case of ``commit=True'', as before) and
        then **reflect the same exception**;
      • ``close()'' in all cases.

    A technical and ineffective difference from the outside: the connection is made at the moment of entering ``with''
    (previously with ``sqlite3.connect'' in the first line of the body). Connection creation errors
    Now they come up at the same point with ``DatabaseConnectionError'' type.
    """
    conn = get_connection(timeout=timeout, commit=commit)
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = [
    # compatibility
    "get_connection",
    "connect_to",
    "connect",
    "connect_sqlite",
    "connect_postgres",
    "dsn_from_env",
    "timezone_options",
    "TZ_OPTIONS",
    "default_db_path",
    "quote_ident",
    "db_connection",
    # Classes and errors
    "ConnectionAdapter",
    "DatabaseError",
    "DatabaseOperationalError",
    "ColumnAlreadyExists",
    "UndefinedTable",
    "UndefinedColumn",
    "UniqueViolation",
    "DatabaseConnectionError",
    "ConfigurationError",
    "UnsupportedBackendError",
    "UnsupportedFeatureError",
    "PlaceholderTranslationError",
    # backend
    "resolve_backend",
    "resolve_dsn",
    "BACKEND_SQLITE",
    "BACKEND_POSTGRESQL",
    "SUPPORTED_BACKENDS",
    "ALL_BACKENDS",
    "BACKEND_ALIASES",
    "ENV_DB_BACKEND",
    "ENV_PG_DSN",
    # placeholder
    "PLACEHOLDER_STYLE",
    "escape_percent_signs",
    "translate_placeholders",
    # script (E.4.7)
    "split_sql_statements",
    # upsert (E.5)
    "upsert_sql",
    # schema (E.4.5)
    "APP_TABLES",
    "BOOLEAN_COLUMNS",
    "YIELD_TABLES",
    "boolean_columns_of",
    "SECURITY_TABLES",
    "is_postgres",
    "present_tables",
    "missing_tables",
    "schema_columns",
    "verify_postgres_tables",
]
