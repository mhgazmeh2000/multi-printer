# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/connection.py
# @brief:        Connection adapter that gives one API over SQLite and PostgreSQL.
# @details:      Responsibilities: Resolve backend and DSN, adapt cursors and SQL dialect, and
#                provide quoting helpers.
#                Main API: resolve_backend(), resolve_dsn(), dsn_from_env(), default_db_path(),
#                quote_ident(), ConnectionAdapter, connect_sqlite(), timezone_options() (+2 more)
#                Side effects: database access
# @dependencies: internal: .exceptions, .placeholders, .scripts, config | external: psycopg
# @notes:        Backend is chosen by DB_BACKEND (sqlite by default). PostgreSQL DSN comes from
#                the environment; secrets are masked in logs.
# =============================================================================

"""
ConnectionAdapter — thin layer on the database connection (phase D: SQLite, phase E.2: two backends).

The contract of this layer:
  • No behavior is changed from the outside. The adapter is a **remove**, not a new engine.
  • "Using adapter" and "Using previous logic" in tests with
    ``assert_wire_equivalent'' are bound together; Intentionally no semantic difference in
    Transaction grouping or return rows are not allowed (``executemany'' is the same as before
    is a single transaction, not a transaction per batch).
  • Queries are still written with ``?'' style; Placeholder translation only in
    The PostgreSQL path is enabled (``core/db/placeholders.py'').

Status after E.2.1 (dual backend)
---------------------------------
  • ``SUPPORTED_BACKENDS'' has both ``sqlite'' and ``postgresql'' values;
  • ``sqlite'' ⇒:func:`connect_sqlite' on ``config.settings.DB_PATH'';
  • ``postgresql'' ⇒:func:`connect_postgres' on ``PG_DSN'' with ``psycopg'' driver 3.
    Connect with ``autocommit=False'' (psycopg default) and **TimeZone=UTC locked** on
    The session level is opened (paragraph 7 ``docs/postgresql-strictness.md'');
  • ``backend'' maps to both values: default ``sqlite'', no silent fallback.

Compatibility note: ``DB_BACKEND=postgresql'' in phase E.2 **does not activate in production**;
This layer only accepts the second backend (activation, phase E.3).

Added E.4.3: ``ConnectionAdapter.insert_returning_id'' — ``INSERT + id
row'' with a signature on both backends (SQLite: ``lastrowid''; PG: ``RETURNING id'').
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from typing import Any, Iterable, Iterator, Optional, Sequence

from config import settings as _settings
from .exceptions import (
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
)
from .placeholders import translate_placeholders
from .scripts import split_sql_statements

#: Allowed values ​​of ``DB_BACKEND'' environment parameter. From E.2 both backends are supported.
BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRESQL = "postgresql"
SUPPORTED_BACKENDS = (BACKEND_SQLITE, BACKEND_POSTGRESQL)
ALL_BACKENDS = (BACKEND_SQLITE, BACKEND_POSTGRESQL)

ENV_DB_BACKEND = "DB_BACKEND"
#: PostgreSQL connection address (libpq style: ``host=… port=… dbname=… user=… password=…``).
ENV_PG_DSN = "PG_DSN"

#: Other common names for PostgreSQL (mapped to the same backend in Phase E).
BACKEND_ALIASES = {
    "postgres": BACKEND_POSTGRESQL,
    "pg": BACKEND_POSTGRESQL,
}


def resolve_backend(value: Optional[str] = None) -> str:
    """
    Specifies the backend from the ``DB_BACKEND'' parameter or environment variable.

    As of E.2 both values are allowed: ``sqlite'' and ``postgresql'' (with alternate names
    ``postgres''/``pg''). Empty value ⇒ default ``sqlite''.

    Note: Specifying backend **alone** means "backend selected", not "connection possible";
    For postgresql, missing ``PG_DSN'' in:func:`connect` with ``ConfigurationError''
    declared (not a silent fallback to sqlite).

    Raises:
        UnsupportedBackendError: Unknown value for ``DB_BACKEND''.
    """
    raw = value if value is not None else os.getenv(ENV_DB_BACKEND, "")
    backend = (raw or BACKEND_SQLITE).strip().lower()
    backend = BACKEND_ALIASES.get(backend, backend)
    if backend in SUPPORTED_BACKENDS:
        return backend
    raise UnsupportedBackendError(
        f"backend ناشناخته «{backend}» برای {ENV_DB_BACKEND} "
        f"(مقادیر مجاز: {', '.join(ALL_BACKENDS)}).",
        backend=backend)


def resolve_dsn(backend: str, dsn: Optional[str] = None,
                *, env: Optional[dict] = None) -> Optional[str]:
    """
    Specifies the final connection address for the backend — **explicit, no fallback**.

    Args:
        backend: The return value of:func:`resolve_backend`.
        dsn: explicit address (first priority); ``None'' ⇒ from ``PG_DSN''.
        env: alternative environment mapping (for testing).

    Returns:
        For ``postgresql'': non-empty DSN string. For ``sqlite'': ``None''
        (sqlite does not have an address; the path to the file comes separately with ``path'').

    Raises:
        UnsupportedBackendError: Unsupported backend.
        ConfigurationError: ``postgresql'' with no address.
    """
    if backend == BACKEND_SQLITE:
        return None
    if backend != BACKEND_POSTGRESQL:
        raise UnsupportedBackendError(
            f"backend «{backend}» مسیر اتصالی ندارد "
            f"(مقادیر مجاز: {', '.join(ALL_BACKENDS)}).", backend=backend)
    target = (dsn if dsn is not None else dsn_from_env(env)).strip()
    if not target:
        raise ConfigurationError(
            f"backend=postgresql انتخاب شده ولی نشانی اتصال خالی است؛ "
            f"{ENV_PG_DSN} را ست کنید (مثال: host=127.0.0.1 port=5432 dbname=printguard "
            f"user=printguard password=…) یا backend را به {BACKEND_SQLITE} برگردانید.",
            backend=BACKEND_POSTGRESQL)
    return target


def dsn_from_env(env: Optional[dict] = None) -> str:
    """
    Reads the PostgreSQL connection address from ``PG_DSN'' (empty ⇒ empty string).

    Args:
        env: alternative mapping (default ``os.environ'') — for testing.
    """
    source = os.environ if env is None else env
    return (source.get(ENV_PG_DSN, "") or "").strip()


def default_db_path() -> str:
    """
    It reads the default database path **at call time** from ``config.settings''.

    Why it matters: Monkeypatch if the path is bound during import
    ``config.settings.DB_PATH'' (tests and tools template) becomes invalid. Here every
    The load reads the current value. The default value of ``logs.db'' is relative and at the moment
    The connection is interpreted relative to the current CWD — the same behavior as the previous code.
    """
    return getattr(_settings, "DB_PATH", "logs.db")


# ─────────────────────────────────────────────────────────────────────
# Translation of driver error to shared family (E.4.4)
# ─────────────────────────────────────────────────────────────────────
#: Mapping SQLite messages to common class. SQLite does not have an SQLSTATE code, so you have to
#: is message oriented; This is the only place where we rely on the text of the message and the document itself
#: done Order is important: more specific clauses come first.
_SQLITE_MESSAGE_RULES = (
    ("duplicate column name", ColumnAlreadyExists),
    ("no such table", UndefinedTable),
    ("no such column", UndefinedColumn),
)

#: SQLSTATE prefixes that map to the shared class (PG side).
_PG_SQLSTATE_RULES = (
    ("23", UniqueViolation),        # The whole family of integrity (Decision E.4.4)
    ("42P01", UndefinedTable),
    ("42703", UndefinedColumn),
    ("42701", ColumnAlreadyExists),
)


#: JSON key must be "plain identifier" (without periods/quotes/spaces). This SQL text helper
#: makes, so input validation is the security boundary here.
_JSON_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """
    It quotes the ID (column/table name) to insert into the SQL text.

    Standard SQL syntax: Double quotes, and any inner quotes are doubled. on each
    Two backends are valid (``core.db.schema'' also uses the same).
    """
    return '"' + str(name).replace('"', '""') + '"'


def _translate_error(exc: BaseException, backend: str) -> BaseException:
    """
    Translates the native driver error to the ``core.db.exceptions'' family.

    Contract:
      • Errors that are already ``DatabaseError'' are returned intact
        (re-translation prohibited);
      • In SQLite, mapping is message-oriented (there is no SQLSTATE code);
      • In PG, first ``sqlstate'' and then **psycopg exception family** are checked
        (because some errors - such as client error - have an empty sqlstate);
      • Untranslated errors are returned **intact** to preserve previous behavior
        (no error will be rewritten for no reason);
      • ``original'' is always kept and the driver message remains intact
        (A few consumers still read the text for user guidance).

    Args:
        exc: exception taken from the driver.
        backend: ``sqlite'' or ``postgresql''.

    Returns:
        common exception (or ``exc'' if it is outside the map).
    """
    if isinstance(exc, DatabaseError):
        return exc

    message = str(exc)

    if backend == BACKEND_SQLITE:
        try:
            import sqlite3
            is_integrity = isinstance(exc, sqlite3.IntegrityError)
            is_operational = isinstance(exc, sqlite3.OperationalError)
            is_db_error = isinstance(exc, sqlite3.DatabaseError)
        except ImportError:  # pragma: no cover - defensive
            return exc
        if not is_db_error:
            return exc
        if is_integrity:
            return UniqueViolation(message, backend=backend, original=exc)
        lowered = message.lower()
        if is_operational:
            for needle, cls in _SQLITE_MESSAGE_RULES:
                if needle in lowered:
                    return cls(message, backend=backend, original=exc)
            return DatabaseOperationalError(message, backend=backend, original=exc)
        return DatabaseError(message, backend=backend, original=exc)

    # ── PostgreSQL ──
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        for prefix, cls in _PG_SQLSTATE_RULES:
            if sqlstate.startswith(prefix):
                return cls(message, backend=backend, sqlstate=sqlstate, original=exc)
    try:
        import psycopg
        mapping = (
            (psycopg.errors.DuplicateColumn, ColumnAlreadyExists),
            (psycopg.errors.UndefinedTable, UndefinedTable),
            (psycopg.errors.UndefinedColumn, UndefinedColumn),
            (psycopg.IntegrityError, UniqueViolation),
            (psycopg.OperationalError, DatabaseOperationalError),
            (psycopg.Error, DatabaseError),
        )
    except ImportError:  # pragma: no cover - defensive
        return exc
    for cls, target in mapping:
        if isinstance(exc, cls):
            return target(message, backend=backend, sqlstate=sqlstate, original=exc)
    return exc


class ConnectionAdapter:
    """
    It creates a thin layer around SQLite so that the upstream code does not depend on the backend.

    Usage (same form as previous raw connection)::

        with db_connection(commit=True) as conn:
            conn.execute("INSERT INTO logs (...) VALUES (?, ?)", (a, b))
            row = conn.execute("SELECT ...", (x,)).fetchone()

    Compatibility:
      • ``fetchone``/``fetchall``/``fetchmany``/``description``/``rowcount``/
        ``lastrowid''/``close''/``iteration'' are forwarded on the adapter itself ⇒
        The code that used to write ``conn.execute(...).fetchone()'' works without modification.
      • ``with conn.execute(...) as cur:'' (common sqlite3 pattern) works, because
        ``__enter__'' on the adapter returns the same cursor.
      • ``iter(conn.execute(...))'' and ``for row in conn.execute(...)'' work together
        (``__iter__'' only makes sense when the cursor is active).

    Backend differences (phase E.2, all with measurement):
      • ``lastrowid`` ⇒ on PG ``NotImplementedError`` with solution ``RETURNING id``;
        The correct way is from E.4.3:meth:`insert_returning_id` (works on both backends);
      • ``executescript'' ⇒ From E.4.7 also works on PG: script to statement
        break and run each separately (browser: ``core/db/scripts.py'');
      • Errors ⇒ Translated to ``core.db.exceptions'' (E.4.4): ``UniqueViolation'',
        ``ColumnAlreadyExists'', ``UndefinedTable''/``UndefinedColumn'',
        ``DatabaseOperationalError'', and finally ``DatabaseError'';
      • ``row_factory'' ⇒ on ineffective PG (row form with ``psycopg.rows'' in E.5);
      • ``executemany'' ⇒ goes to PG from cursor (``psycopg.Connection'' does not have this method).
    """

    def __init__(self, conn, backend: str = BACKEND_SQLITE):
        # Explicit: Bypassing a connection with an unknown backend will cause an error when executing the query
        # (dumb message "translation of placeholder failed"). It's faster and clearer here.
        """Wrap a DB-API connection and remember which backend it uses."""
        if backend not in SUPPORTED_BACKENDS:
            raise UnsupportedBackendError(
                f"backend «{backend}» برای ConnectionAdapter پشتیبانی نمی‌شود "
                f"(مقادیر مجاز: {', '.join(SUPPORTED_BACKENDS)}).", backend=backend)
        self._conn = conn
        self.backend = backend
        self._closed = False
        self._last_cursor = None

    # Running a query
    def _sql(self, sql: str) -> str:
        """Prepares a query for the current backend (in SQLite: unchanged)."""
        if self.backend == BACKEND_SQLITE:
            return sql
        try:
            return translate_placeholders(sql, self.backend)
        except PlaceholderTranslationError:
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            raise PlaceholderTranslationError(
                f"ترجمه‌ی placeholder ناموفق بود: {exc}", backend=self.backend) from exc

    def execute(self, sql: str, params: Sequence[Any] = ()):
        """
        Running a query; Returns the raw cursor (with fetch* and context manager support).

        Driver error before upload, translate to ``core.db.exceptions'' family
        (E.4.4) so that the consumer never depends on ``sqlite3.*''/``psycopg.*''
        not be The placeholder translation exception (adapter's own error) remains intact.
        """
        try:
            cur = self._conn.execute(self._sql(sql), params)
        except DatabaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc, self.backend) from exc
        self._last_cursor = cur
        return cur

    def executemany(self, sql: str, params_list: Iterable[Sequence[Any]]):
        """
        Batch execution of a query.

        As before, it is **a single transaction**: commit/rollback is the calling task
        (or ``commit=True'' in ``db_connection'').

        backend diff (measured): ``sqlite3.Connection'' ``executemany'' method
        It has, but it doesn't have ``psycopg.Connection'' ⇒ on postgresql, the same thing through
        cursor is done. The transaction policy remains the same (no implicit commit).
        """
        try:
            if self.backend == BACKEND_SQLITE:
                cur = self._conn.executemany(self._sql(sql), params_list)
            else:
                cur = self._conn.cursor()
                cur.executemany(self._sql(sql), params_list)
        except DatabaseError:
            raise
        except Exception as exc:
            raise _translate_error(exc, self.backend) from exc
        self._last_cursor = cur
        return cur

    def executescript(self, script: str):
        """
        Execution of several commands in a row (equivalent to ``sqlite3.Connection.executescript'').

        Routes (E.4.7):
          • **SQLite:** Same as ``sqlite3.Connection.executescript'' — unchanged
            (gives the script intact to the driver; ``?'' has no meaning here).
          • **PostgreSQL:** script with:func:`core.db.scripts.split_sql_statements`
            It is broken into statements and each one is executed separately with ``execute''.
            Why not an ``execute'' multi-command? Because psycopg only accept simple protocol
            does: ``execute("A; B", (1,))`` ⇒
            ``cannot insert multiple commands into a prepared statement``
            (measured). With separate execution, the error is attributed to the same statement
            will be

        Contract: As before, there is **no commit** here (the transaction is owned by the caller).
        The return value on SQLite is the same as the driver's cursor and on PG the last cursor
        statement (for empty/comment-only script: ``None'').

        Raises:
            ValueError: Invalid script (unwrapped string, dollar-quoting).
        """
        if self.backend == BACKEND_SQLITE:
            try:
                cur = self._conn.executescript(self._sql(script))
            except DatabaseError:
                raise
            except Exception as exc:
                raise _translate_error(exc, self.backend) from exc
            self._last_cursor = cur
            return cur

        cur = None
        for statement in split_sql_statements(script):
            cur = self.execute(statement)      # The error translation comes from this path
        self._last_cursor = cur
        return cur

    # Insert + ID (E.4.3)
    def json_extract(self, column: str, key: str, *, as_number: bool = False) -> str:
        """
        SQL fragment to read a key from a JSON column — independent of the backend (E.4.9).

        Why it's needed: ``json_extract(col, '$.key')'' is a SQLite function (extension
        JSON1) and does not exist on PostgreSQL —
        ``function json_extract(text, unknown) does not exist'' (measured,
        E.4.6/E.4.9). PostgreSQL equivalent is ``->>'' operator.

        **type** difference covered by ``as_number'':
          • SQLite: ``json_extract'' returns a JSON value of its own type
            (number ⇒ INTEGER/REAL, string ⇒ TEXT); This makes the comparison
            ``=?'' works with a Python number;
          • PostgreSQL: ``->>'' always returns **text**, so it should be used for numerical comparison
            Cast explicitly. ``as_number=True'' on PG adds the same cast
            (``::numeric``: both integer and decimal) and nothing extra on SQLite
            It doesn't, because the type is already correct — that is, SQLite is byte-for-byte the same
            It stays today.

        The second point specific to this project: the cast to ``jsonb'' remains in the fragment, because
        **All** JSON columns of the project on PG are textual **except** ``logs.details'' which are from
        migration ``0004'' (phase E.6.4) has type ``jsonb''. For a text column, cast
        Required (otherwise PG says "operator does not exist: text ->> unknown" —
        measured, E.4.9) and is cost-free for the ``jsonb'' column (``x::jsonb'' on
        ``jsonb'' is the value itself; Measured in E.6.4: both writing a number
        return). Bad data consequence difference: on ``jsonb'' validation at **time
        Write** falls (``22P02'' is where the value is written), and on the text column
        At **read time** — that is, a malformed row breaks the entire aggregate query.

        Small tip (registered, no effect on current data): If the JSON value is a decimal number
        Yes, ``CAST(... AS INTEGER)'' in SQLite leads to zero and in PG it rounds
        does The page counters in this project are always correct
        (``json.dumps'' on ``int''), so it makes no practical difference.

        Args:
            column: column name (quoted).
            key: key in JSON (without ``$.``).
            as_number: for numerical comparisons on PG.

        Returns:
            The SQL fragment is ready to be inserted in the query.

        Raises:
            ValueError: if ``key'' is not a simple identifier (security boundary: this creates SQL text).
        """
        if not isinstance(key, str) or not _JSON_KEY_RE.match(key):
            raise ValueError(
                f"کلید JSON نامعتبر است: {key!r} (فقط شناسهٔ ساده مجاز است)")
        col = quote_ident(column)
        if self.backend == BACKEND_POSTGRESQL:
            # The JSON column of this project on PG is of **TEXT** type (migration ``0001''):
            # ``sa.Text'') and the ``->>'' operator is only defined on ``json''/``jsonb'';
            # Without cast, PG says "operator does not exist: text ->> unknown"
            # (measured, E.4.9). So it is explicitly cast.
            jsonb = f"{col}::jsonb"
            fragment = f"({jsonb})->>'{key}'"
            return f"({fragment})::numeric" if as_number else fragment
        return f"json_extract({col}, '$.{key}')"


    def insert_returning_id(self, sql: str, params: Sequence[Any] = ()) -> Optional[int]:
        """
        Executes an ``INSERT'' and returns the ID of the inserted row.

        Why separate method? ``lastrowid'' is a SQLite concept and does not exist on PostgreSQL
        (Measured: neither ``psycopg.Connection'' nor ``psycopg.Cursor'' as such
        They do not have a feature). The standard PG alternative is ``INSERT... RETURNING id''.
        This method hides the difference **somewhere** so that the domain code does not have two parallel versions:

          • SQLite: ``execute(sql, params)'' then ``cursor.lastrowid'';
          • PostgreSQL: ``execute(sql.rstrip().rstrip(';') + " RETURNING id")``
            then ``fetchone()[0]''.

        Contract:
          • No commit is done here (like ``execute''); Transaction in hand
            is call (or ``commit=True'' in ``db_connection'');
          • ``None'' when the insert contains **no rows** (``rowcount == 0''). this
            It is important because ``sqlite3'' does not touch ``lastrowid'' in that case
            It leaves (the value of the previous entry) and silently gives the wrong id;
          • ``ValueError'' with clear message if SQL already has ``RETURNING''
            we give — otherwise we would get a syntax error on PG and a mistake on SQLite id;
          • ``lastrowid'' on PG still gives ``NotImplementedError'' (E.2): This
            The method gives the right path, but does not silence the wrong path.

        Raises:
            ValueError: if ``sql'' already has ``RETURNING''.
            NotImplementedError: Never — This method works on both backends.
        """
        if "returning" in sql.lower():
            raise ValueError(
                "insert_returning_id خودش «RETURNING id» را اضافه می‌کند؛ "
                "SQL ورودی نباید RETURNING داشته باشد.")

        if self.backend == BACKEND_SQLITE:
            cur = self.execute(sql, params)
            if cur.rowcount == 0:
                return None
            return cur.lastrowid

        trimmed = sql.rstrip().rstrip(";").rstrip()
        cur = self.execute(f"{trimmed} RETURNING id", params)
        row = cur.fetchone()
        return None if row is None else int(row[0])

    # Transaction/Connection Management
    def commit(self) -> None:
        """Commit the current transaction."""

        self._conn.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""

        self._conn.rollback()

    def close(self) -> None:
        """Close the underlying database connection."""

        self._conn.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        """Has ``close()'' been called? (test sweep; does not affect the application API)"""
        return self._closed

    @property
    def raw_connection(self):
        """Bottom raw connection — for search/test and temporary bridges."""
        return self._conn

    def cursor(self):
        """
        Raw cursor for places where direct cursor return is required.

        The same driver cursor is returned on both backends: ``sqlite3.Cursor''
        or ``psycopg.Cursor''. The code that works on this cursor should only be from
        Common APIs (``execute``/``fetchone``/``fetchall``/``rowcount``/
        ``description'') — not ``lastrowid'' (not present on PG).
        """
        return self._conn.cursor()

    # ─── passthrough accesses (current code on raw connection uses these) ───
    def fetchone(self):
        """Return the next row of the last query, or None."""

        return self._require_cursor("fetchone").fetchone()

    def fetchall(self):
        """Return all remaining rows of the last query."""

        return self._require_cursor("fetchall").fetchall()

    def fetchmany(self, size: Optional[int] = None):
        """Return up to size rows of the last query."""

        cur = self._require_cursor("fetchmany")
        return cur.fetchmany() if size is None else cur.fetchmany(size)

    @property
    def lastrowid(self):
        """
        ID of the last insert — **SQLite only**.

        PostgreSQL doesn't have this concept (psycopg doesn't have ``Cursor.lastrowid'' either —
        measured). Instead of a dumb AttributeError, we give an explicit error with a correct solution.
        From E.4.3 the **implemented** solution is::meth:`insert_returning_id` (on both
        backend) and the only live consumption of ``lastrowid'' in ``core/database.py''
        (``create_user``) moved to same (``docs/postgresql-strictness.md`` clause 9).
        This feature is only kept for residual code to be leaked, not muted
        make a mistake

        Raises:
            NotImplementedError: if backend is ``postgresql''.
        """
        if self.backend != BACKEND_SQLITE:
            raise NotImplementedError(
                "lastrowid روی PostgreSQL وجود ندارد؛ از "
                "«INSERT … RETURNING id» + fetchone()[0] استفاده کنید "
                "(الزام E.4 — docs/postgresql-strictness.md بند ۹).")
        return self._require_cursor("lastrowid").lastrowid

    # ─── sqlite3 style compatibility on PG path (E.2.3) ──────────────────
    @property
    def row_factory(self):
        """
        sqlite3-style ``row_factory'' — passed on SQLite, null on PG.

        On PostgreSQL, the row shape is determined by ``psycopg.rows'' and in phase E.5
        (``dict_row'') is decided; Until then, setting this feature is not an error
        gives and has no effect (it reads ``None'') until the code he wrote for sqlite3
        So that he doesn't suddenly break on PG and his behavior is not assumed to be wrong.
        """
        if self.backend == BACKEND_SQLITE:
            return getattr(self._conn, "row_factory", None)
        return None

    @row_factory.setter
    def row_factory(self, value):
        """Set the row factory on the underlying connection."""

        if self.backend == BACKEND_SQLITE:
            self._conn.row_factory = value
        # postgresql: Intentionally ineffective (documented in docstring above)

    @property
    def description(self):
        """Column descriptions of the last query."""

        cur = self._last_cursor
        return None if cur is None else cur.description

    @property
    def rowcount(self):
        """Number of rows affected by the last statement."""

        cur = self._last_cursor
        return -1 if cur is None else cur.rowcount

    def _require_cursor(self, what: str):
        """Return the last cursor, raising an error when no query has been executed."""
        cur = self._last_cursor
        if cur is None:
            raise self._programming_error(
                f"no active cursor: execute() را قبل از {what} صدا بزنید")
        return cur

    def _programming_error(self, message: str) -> Exception:
        """
        "Incorrect use of API" error with the right type of backend.

        SQLite ⇒ ``sqlite3.ProgrammingError'' (unchanged from Phase D, up to
        existing ``except''s will work intact). PostgreSQL ⇒
        ``psycopg.ProgrammingError'' (same family of errors in the destination driver).
        """
        if self.backend == BACKEND_SQLITE:
            return sqlite3.ProgrammingError(message)
        try:
            import psycopg  # noqa: PLC0415 — Lazy, like connect_postgres
            return psycopg.ProgrammingError(message)
        except ImportError:  # pragma: no cover - defensive path
            return DatabaseConnectionError(message, backend=self.backend)

    def __iter__(self) -> Iterator[Any]:
        """Iterate over the rows of the last executed query."""
        return iter(self._require_cursor("iteration"))

    # ─── context manager ────────────────────────────────────────
    def __enter__(self) -> "ConnectionAdapter":
        """Enter the context manager and return this adapter."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Commit or rollback — with the **same** previous ``db_connection'' policy:
        ``db_connection'' also commits/rollback itself (both are idempotent),
        But if someone makes the adapter manually and an error occurs inside ``with'', here
        It will be rolled back.
        """
        if exc_type is None:
            self.commit()
        else:
            with contextlib.suppress(Exception):
                self.rollback()
        return False  # We do not suppress the error

    def __repr__(self) -> str:  # pragma: no cover - only for debugging
        """Return a short debug representation showing the backend and open/closed state."""
        state = "closed" if self._closed else "open"
        return f"<ConnectionAdapter backend={self.backend} {state}>"


def connect_sqlite(path: str, timeout: float = 30.0) -> ConnectionAdapter:
    """
    Creating a SQLite connection and wrapping it in the adapter.

    **Only ``sqlite3.connect'' is allowed in the application code** (phase D acceptance criteria).
    ``PRAGMA busy_timeout'' is set equal to ``timeout''; This is exactly the work
    which ``timeout'' parameter of sqlite3 also does (busy_timeout in C level),
    So it does not add new behavior or change anything.

    Raises:
        DatabaseConnectionError: If connection creation fails.
    """
    try:
        raw = sqlite3.connect(path, timeout=timeout)
    except Exception as exc:
        raise DatabaseConnectionError(
            f"اتصال به دیتابیس ممکن نشد (backend={BACKEND_SQLITE}, path={path}): {exc}",
            backend=BACKEND_SQLITE) from exc
    try:
        raw.execute(f"PRAGMA busy_timeout={int(float(timeout) * 1000)}")
        # SQLite does not enforce foreign keys unless asked per connection;
        # migration 0008 adds FK constraints that would otherwise never fire.
        raw.execute("PRAGMA foreign_keys=ON")
    except Exception:  # pragma: no cover - defensive
        # Phase E.4.4: intentional Exception (not sqlite3.Error) — this PRAGMA only one
        # It is an optimization and should not break the construction of the connection; Also at the end of E.4.4 none
        # ``except sqlite3.*'' does not stay in core/ and web/ (owner requirement).
        pass
    return ConnectionAdapter(raw, backend=BACKEND_SQLITE)


# ─────────────────────────────────────────────────────────────
# PostgreSQL (Phase E.2)
# ─────────────────────────────────────────────────────────────
#: lock session time to UTC (paragraph 7 of `docs/postgresql-strictness.md`): equivalent
#: `SET TIME ZONE 'UTC'`, but **before connecting** and at the libpq level ⇒ no transaction/command
#: Addition is not performed on the connection.
TZ_OPTIONS = "-c timezone=UTC"


def _tokenize_conninfo(dsn: str) -> list:
    """
    The conninfo string breaks the libpq style into ``key=value'' tokens.

    Why is it necessary: ​​values ​​like ``options='-c timezone=UTC -c a=1'' are spaced;
    ``str.split()'' will split them. This function returns single quotes (with escaping
    ``\\'``) understands. It does not return a token for a DSN of URI type (``postgresql://...'').
    (because it doesn't have ``=''/is invalid) and the call uses the path URI.
    """
    tokens, buf, i, n, in_quote = [], [], 0, len(dsn), False
    while i < n:
        ch = dsn[i]
        if in_quote:
            if ch == "\\" and i + 1 < n:
                buf.append(dsn[i:i + 2]); i += 2; continue
            if ch == "'":
                in_quote = False; i += 1; continue
            buf.append(ch); i += 1; continue
        if ch == "'":
            in_quote = True; i += 1; continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf)); buf = []
            i += 1; continue
        buf.append(ch); i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _conninfo_value(dsn: str, key: str) -> Optional[str]:
    """the value of a conninfo key (without quotes); No key ⇒ ``None''."""
    if "://" in dsn:  # URI: This parsing makes no sense
        return None
    for token in _tokenize_conninfo(dsn):
        k, sep, v = token.partition("=")
        if sep and k.strip().lower() == key.lower():
            return v.strip().strip("'\"") if v.strip().startswith(("'", '"')) else v.strip()
    return None


def timezone_options(dsn: str) -> str:
    """
    ``options'' parameter value to lock UTC — without losing existing settings.

    If the DSN itself has ``options'', it is appended to the end (in libpq
    Subsequent ``-c''s override previous ones ⇒ UTC is guaranteed) otherwise just UTC.
    """
    existing = _conninfo_value(dsn, "options")
    if not existing:
        return TZ_OPTIONS
    if "timezone=" in existing.lower().replace(" ", ""):
        return existing + " " + TZ_OPTIONS
    return existing + " " + TZ_OPTIONS


def _import_psycopg():
    """
    The driver imports psycopg 3 (not psycopg2).

    Raises:
        DatabaseConnectionError: if not installed — with clear installation message, because this error
            It is not "configuration" but "missing dependency".
    """
    try:
        import psycopg  # noqa: PLC0415 — intentional: lazy import so sqlite doesn't depend on psycopg
        return psycopg
    except ImportError as exc:
        raise DatabaseConnectionError(
            "درایور psycopg (نسخه‌ی ۳) نصب نیست؛ برای backend=postgresql لازم است. "
            "نصب: python -m pip install \"psycopg[binary]\"",
            backend=BACKEND_POSTGRESQL) from exc


def connect_postgres(dsn: Optional[str] = None, timeout: float = 30.0) -> ConnectionAdapter:
    """
    Creating a PostgreSQL connection and wrapping it in the adapter.

    Args:
        dsn: libpq style connection address; ``None'' ⇒ Read from ``PG_DSN''.
        timeout: connection timeout (seconds) ⇒ ``connect_timeout'' libpq (minimum 1 second).

    Binding convention: ``autocommit=False'' (default psycopg ⇒ any explicit commit, e.g
    SQLite), ``options=-c timezone=UTC'' (lock UTC at the session level), and
    ``connect_timeout'' is the same as adapter ``timeout''.

    Raises:
        ConfigurationError: Missing DSN (not in parameter, not in ``PG_DSN'').
        DatabaseConnectionError: Missing driver, or connection failure (server down, wrong password/DB).
    """
    target = resolve_dsn(BACKEND_POSTGRESQL, dsn)   # Empty ⇒ ConfigurationError
    psycopg = _import_psycopg()
    try:
        raw = psycopg.connect(
            target,
            connect_timeout=max(1, int(float(timeout))),
            autocommit=False,
            options=timezone_options(target),
        )
    except Exception as exc:
        raise DatabaseConnectionError(
            f"اتصال به PostgreSQL ممکن نشد (backend={BACKEND_POSTGRESQL}): "
            f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}",
            backend=BACKEND_POSTGRESQL) from exc
    return ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)


def connect(path: Optional[str] = None, *, timeout: float = 30.0,
            backend: Optional[str] = None) -> ConnectionAdapter:
    """
    Connection creation entry point: select backend + default path.

    Args:
        path: the explicit path of the database file **only for sqlite**; If it is ``None''
            :func:`default_db_path` (ie ``config.settings.DB_PATH``) right now
            is read For postgresql, the path of the file has no meaning and the address of the
            ``PG_DSN'' comes up (giving ``path'' with postgresql is a clear error).
        timeout: lockout timeout for sqlite / connection timeout for postgresql (seconds).
        backend: If it is ``None'', it is read from ``DB_BACKEND''.

    Raises:
        UnsupportedBackendError: unknown backend — no silent fallback.
        ConfigurationError: ``postgresql`` no DSN (from:func:`resolve_dsn`), or
            Giving ``path'' along with postgresql.
        DatabaseConnectionError: Failed to create connection.
    """
    resolved = resolve_backend(backend)
    if resolved == BACKEND_SQLITE:
        return connect_sqlite(path if path is not None else default_db_path(), timeout)
    if resolved == BACKEND_POSTGRESQL:
        if path is not None:
            raise ConfigurationError(
                f"مسیر فایل («{path}») فقط برای backend={BACKEND_SQLITE} معنا دارد؛ "
                f"برای {BACKEND_POSTGRESQL} نشانی را در {ENV_PG_DSN} بگذارید.",
                backend=resolved)
        return connect_postgres(None, timeout)
    raise UnsupportedBackendError(  # Defensive Guard: Conflicts with SUPPORTED_BACKENDS
        f"backend «{resolved}» مسیر اتصالی در connect() ندارد.", backend=resolved)


__all__ = [
    "ConnectionAdapter",
    "connect",
    "connect_sqlite",
    "connect_postgres",
    "default_db_path",
    "resolve_backend",
    "resolve_dsn",
    "dsn_from_env",
    "timezone_options",
    "TZ_OPTIONS",
    "BACKEND_ALIASES",
    "BACKEND_SQLITE",
    "BACKEND_POSTGRESQL",
    "SUPPORTED_BACKENDS",
    "ALL_BACKENDS",
    "ENV_DB_BACKEND",
    "ENV_PG_DSN",
]
