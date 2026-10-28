# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_db_adapter.py
# @brief:        Tests for the data-layer connection adapter (phase D).
# @details:      Responsibilities: Check wire equivalence and backend selection.
#                Main API: _TempDbBase, ConnectionAdapterTests, WireEquivalenceTests, _StubCursor,
#                _StubConnection, BackendSelectionTests
#                Side effects: database access
# @dependencies: internal: core.database, core.db, core.db.scripts, core.security_audit |
#                external: psycopg, pytest
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Data layer adapter tests (Phase D — Task D.1).

Coverage:
  A) ConnectionAdapter on SQLite: execute/executemany/commit/rollback/close,
     context manager (success ⇒ commit, error ⇒ rollback), raw cursor and passwords,
     and "wired equivalence" using sqlite3 directly on a single database file.
  B) Specify backend from ``DB_BACKEND'': sqlite default, both values allowed (from E.2:
     sqlite and postgresql) and unknown values ⇒ explicit error (no silent fallback).
  C) placeholder translation (``?`` → ``%s``) — today it is not on any execution path,
     But the contract locks phase E.
  D) Compatibility of existing code: ``core.database.db_connection'' and
     ``core.security_audit._db_conn'' are the same as adapter shell and add
     ``add_event'' (which creates the connection itself) actually writes to the database.

Isolation note: The database path in ``config.settings.DB_PATH'' is relative
(``logs.db'') and two layers of adapter/core.database both import the same value
they do; These tests are moved with ``os.chdir'' (like other repository tests) to
Never write to the main ``logs.db''.
"""
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

import pytest

from core.db import (
    ALL_BACKENDS,
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_PG_DSN,
    UndefinedTable,
    SUPPORTED_BACKENDS,
    ConfigurationError,
    ConnectionAdapter,
    ENV_DB_BACKEND,
    PLACEHOLDER_STYLE,
    UnsupportedBackendError,
    connect_postgres,
    db_connection,
    get_connection,
    resolve_backend,
    translate_placeholders,
)

_DDL = "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)"
_TRACE = "SELECT id, name, n FROM t ORDER BY id"


class _TempDbBase(unittest.TestCase):
    """Temporary DB + entry to temporary directory (because DB_PATH is relative)."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def physical(self, sql=("SELECT id, name, n FROM t",)):
        """Physical comparison: same rows directly from sqlite3 (independent control)."""
        raw = sqlite3.connect("logs.db")
        try:
            out = []
            for q in sql:
                try:
                    out.append(raw.execute(q).fetchall())
                except sqlite3.OperationalError as exc:
                    out.append(f"ERR:{exc}")
            return out
        finally:
            raw.close()


# ─────────────────────────────────────────────────────────────────────
# A) ConnectionAdapter behavior
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class ConnectionAdapterTests(_TempDbBase):

    """Tests for connection adapter."""
    def _adapter(self):
        """Helper: adapter."""
        return get_connection(timeout=5.0)

    def test_execute_insert_select_and_row_access(self):
        """Verifies execute insert select and row access."""
        conn = self._adapter()
        try:
            self.assertIsInstance(conn, ConnectionAdapter)
            self.assertEqual(conn.backend, BACKEND_SQLITE)
            conn.execute(_DDL)
            cur = conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("alpha", 1))
            self.assertEqual(cur.lastrowid, 1)
            cur2 = conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("beta", 2))
            self.assertEqual(cur2.rowcount, 1)
            conn.commit()
            # Access on the adapter itself (pattern conn.execute(...).fetchone())
            row = conn.execute(_TRACE).fetchone()
            self.assertEqual(tuple(row), (1, "alpha", 1))
            self.assertEqual(len(conn.execute(_TRACE).fetchall()), 2)
            self.assertEqual([c[0] for c in conn.execute(_TRACE).description],
                             ["id", "name", "n"])
        finally:
            conn.close()
        self.assertEqual(self.physical(), [[(1, "alpha", 1), (2, "beta", 2)]])

    def test_executemany_is_single_transaction(self):
        """executemany should behave like the previous behavior of a single transaction (not implicit commit)."""
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("pre", 0))
            conn.executemany("INSERT INTO t (name, n) VALUES (?, ?)",
                             [("a", 1), ("b", 2), ("c", 3)])
            conn.commit()
        finally:
            conn.close()
        rows = self.physical()[0]
        self.assertEqual([r[1] for r in rows], ["pre", "a", "b", "c"])

    def test_rollback_discards_uncommitted_work(self):
        """Verifies rollback discards uncommitted work."""
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            conn.commit()
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("doomed", 9))
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(self.physical()[0], [])

    def test_adapter_exit_commits_without_closing(self):
        """Phase D contract: __exit__ only commit/rollback; close is called."""
        with self._adapter() as conn:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("ok", 1))
        self.assertFalse(conn.closed)                       # Intentionally: close is not in __exit__
        self.assertEqual(self.physical()[0], [(1, "ok", 1)])  # But it is committed
        conn.close()
        self.assertTrue(conn.closed)

    def test_adapter_exit_rolls_back_and_reraises_on_error(self):
        """Verifies adapter exit rolls back and reraises on error."""
        with self._adapter() as conn:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("ok", 1))
        with self.assertRaises(RuntimeError):
            with self._adapter() as conn:
                conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("boom", 2))
                raise RuntimeError("boom")
        self.assertEqual([r[1] for r in self.physical()[0]], ["ok"])

    def test_db_connection_closes_even_when_body_raises(self):
        """The compatibility shell (db_connection) should always close the connection."""
        import core.database as database
        with self.assertRaises(RuntimeError):
            with database.db_connection() as conn:
                conn.execute(_DDL)
                raise RuntimeError("boom")
        self.assertTrue(conn.closed)

    def test_cursor_and_raw_connection_are_available(self):
        """Verifies cursor and raw connection are available."""
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            cur = conn.cursor()                      # raw cursor (according to phase D signature)
            cur.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("via-cursor", 7))
            conn.commit()
            self.assertEqual(conn.raw_connection.execute(_TRACE).fetchall(),
                             [(1, "via-cursor", 7)])
        finally:
            conn.close()

    def test_execute_with_default_and_empty_params(self):
        """Verifies execute with default and empty params."""
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("empty", 0))
            conn.execute("UPDATE t SET n = n + 1")   # no parameters (default())
            conn.commit()
            self.assertEqual(conn.execute(_TRACE).fetchall(), [(1, "empty", 1)])
        finally:
            conn.close()

    def test_sqlite_error_is_translated_to_shared_type(self):
        """
        E.4.4 — Driver error is raised from adapter with **common type** (not ``sqlite3.*'').

        This test used to lock "release intact sqlite3.OperationalError";
        With decision E.4.4 that contract was changed: consumer (core/ and web/) no more
        It does not have ``except sqlite3.*'' and must take common type.
        """
        conn = self._adapter()
        try:
            with self.assertRaises(UndefinedTable) as ctx:
                conn.execute("SELECT * FROM table_that_does_not_exist")
            self.assertIsInstance(ctx.exception.original, sqlite3.OperationalError)
            self.assertEqual(ctx.exception.backend, BACKEND_SQLITE)
        finally:
            conn.close()

    def test_iteration_and_fetchmany_passthrough(self):
        """Verifies iteration and fetchmany passthrough."""
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            conn.executemany("INSERT INTO t (name, n) VALUES (?, ?)",
                             [("r1", 1), ("r2", 2), ("r3", 3)])
            conn.commit()
            rows = [tuple(r) for r in conn.execute(_TRACE)]     # __iter__
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(conn.execute(_TRACE).fetchmany(2)), 2)
        finally:
            conn.close()

    def test_get_connection_does_not_commit_implicitly(self):
        """The commit parameter on get_connection is only signature compatibility and does not commit."""
        conn = get_connection(timeout=5.0, commit=True)
        try:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("uncommitted", 1))
        finally:
            conn.close()   # Close without commit ⇒ must be discarded
        self.assertEqual(self.physical()[0], [])


# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class WireEquivalenceTests(_TempDbBase):
    """
    "Remoteness" hard locks the adapter:
    Assertion ⇒ adapter and previous logic (raw sqlite3 + the same transaction policy) on sufficient
    The same will produce the same rows (and the same type/order of columns). If
    If one day someone changes the transaction category or parameterization, this test will break.
    """

    @contextmanager
    def _raw_db_connection(self, db_path, timeout=10.0, commit=False):
        """Previous implementation of core.database.db_connection (same, for comparison)."""
        conn = sqlite3.connect(db_path, timeout=timeout)
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

    def test_adapter_matches_previous_wire_behavior(self):
        """Verifies adapter matches previous wire behavior."""
        full = os.path.abspath("logs.db")

        @contextmanager
        def raw_facade():
            """Helper: raw facade."""
            with self._raw_db_connection(full, timeout=10.0, commit=True) as c:
                yield c

        statements = [
            (_DDL, ()),
            ("INSERT INTO t (name, n) VALUES (?, ?)", ("a", 1)),
            ("INSERT INTO t (name, n) VALUES (?, ?)", ("b", 2)),
            ("INSERT INTO t (name, n) VALUES (?, ?)", ("c", 3)),
            ("UPDATE t SET n = ? WHERE name = ?", (20, "c")),
        ]

        # ── path A: adapter (exactly what core.database calls now)
        from core.database import db_connection as compat_db_connection
        with compat_db_connection(commit=True) as conn:
            for sql, params in statements:
                conn.execute(sql, params)
            adapter_rows = [tuple(r) for r in conn.execute(_TRACE)]
            adapter_desc = [c[0] for c in conn.execute(_TRACE).description]
            adapter_types = [type(v).__name__ for v in adapter_rows[0]]

        # ── Path B: previous logic on the same database (reading, not rewriting)
        with raw_facade() as conn:
            raw_rows = [tuple(r) for r in conn.execute(_TRACE)]
            raw_desc = [c[0] for c in conn.execute(_TRACE).description]
            raw_types = [type(v).__name__ for v in raw_rows[0]]

        self.assertEqual(adapter_rows, raw_rows)
        self.assertEqual(adapter_desc, raw_desc)
        self.assertEqual(adapter_types, raw_types)
        self.assertEqual(adapter_rows, [(1, "a", 1), (2, "b", 2), (3, "c", 20)])

    def test_commit_flag_semantics_match(self):
        """commit=False ⇒ discarded; commit=True ⇒ saved (as before)."""
        from core.database import db_connection as compat_db_connection
        with compat_db_connection() as conn:                    # commit=False
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("gone", 1))
        self.assertEqual(self.physical()[0], [])

        with compat_db_connection(commit=True) as conn:
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("kept", 2))
        self.assertEqual([r[1] for r in self.physical()[0]], ["kept"])


class _StubCursor:
    """psycopg dummy cursor: records only SQL/parameters."""

    def __init__(self, log):
        """Initialize the instance."""
        self._log = log
        self.rowcount = -1

    def executemany(self, sql, params_list):
        """Helper: executemany."""
        self._log.append(("executemany", sql, list(params_list)))
        self.rowcount = len(list(params_list))
        return self

    def fetchone(self):
        """Helper: fetchone."""
        return None

    def fetchall(self):
        """Helper: fetchall."""
        return []

    def __iter__(self):
        """Iterate over the items."""
        return iter(())


class _StubConnection:
    """
    psycopg-style dummy connection (serverless): to lock the SQL path passing through the adapter.

    Intentionally **doesn't have ``executemany'' — like ``psycopg.Connection'' itself; If someone
    Call it again in the adapter, the stub breaks like a real PG.
    """

    def __init__(self):
        """Initialize the instance."""
        self.log = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def execute(self, sql, params=()):
        """Helper: execute."""
        self.log.append(("execute", sql, params))
        return _StubCursor(self.log)

    def cursor(self):
        """Helper: cursor."""
        return _StubCursor(self.log)

    def commit(self):
        """Helper: commit."""
        self.committed += 1

    def rollback(self):
        """Helper: rollback."""
        self.rolled_back += 1

    def close(self):
        """Helper: close."""
        self.closed = True


# ─────────────────────────────────────────────────────────────────────
# B) Select backend
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class BackendSelectionTests(unittest.TestCase):

    """Tests for backend selection."""
    def test_default_backend_is_sqlite(self):
        """Verifies default backend is sqlite."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_DB_BACKEND, None)
            self.assertEqual(resolve_backend(), BACKEND_SQLITE)
            self.assertEqual(resolve_backend(""), BACKEND_SQLITE)
            self.assertEqual(resolve_backend("  SQLite  "), BACKEND_SQLITE)

    def test_env_variable_is_honored(self):
        """Verifies env variable is honored."""
        with mock.patch.dict(os.environ, {ENV_DB_BACKEND: "sqlite"}):
            self.assertEqual(resolve_backend(), BACKEND_SQLITE)

    def test_postgresql_is_supported_from_phase_e(self):
        """From E.2: postgresql (and its aliases) is a valid backend."""
        for value in ("postgresql", "postgres", "pg", "PostgreSQL", "  pg  "):
            self.assertEqual(resolve_backend(value), BACKEND_POSTGRESQL)

    def test_unknown_backend_raises(self):
        """Verifies unknown backend raises."""
        with self.assertRaises(UnsupportedBackendError):
            resolve_backend("mysql")
        with mock.patch.dict(os.environ, {ENV_DB_BACKEND: "oracle"}):
            with self.assertRaises(UnsupportedBackendError):
                resolve_backend()

    def test_backend_constants_and_marker_registry_are_consistent(self):
        """Verifies backend constants and marker registry are consistent."""
        self.assertEqual(SUPPORTED_BACKENDS, (BACKEND_SQLITE, BACKEND_POSTGRESQL))
        self.assertEqual(set(ALL_BACKENDS), {BACKEND_SQLITE, BACKEND_POSTGRESQL})
        self.assertEqual(set(PLACEHOLDER_STYLE), set(ALL_BACKENDS))
        # The core.db package should export all names (phase E code sits on these)
        import core.db as pkg
        for name in ("ConnectionAdapter", "get_connection", "db_connection",
                     "resolve_backend", "translate_placeholders",
                     "UnsupportedBackendError", "DatabaseConnectionError",
                     "PlaceholderTranslationError", "DatabaseError",
                     "ConfigurationError", "connect_postgres", "dsn_from_env",
                     "timezone_options", "ENV_PG_DSN", "TZ_OPTIONS"):
            self.assertTrue(hasattr(pkg, name), name)

    def test_get_connection_requires_dsn_for_postgresql_via_env(self):
        """
        From E.2: ``DB_BACKEND=postgresql`` no ``PG_DSN`` ⇒ ``ConfigurationError``
        (no silent fallback to sqlite, no guess connection to default server).
        """
        env = {ENV_DB_BACKEND: "postgresql"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop(ENV_PG_DSN, None)
            with self.assertRaises(ConfigurationError) as ctx:
                get_connection(timeout=1.0)
            self.assertEqual(ctx.exception.backend, BACKEND_POSTGRESQL)
            self.assertIn(ENV_PG_DSN, str(ctx.exception))

    def test_connection_error_is_wrapped(self):
        """Verifies connection error is wrapped."""
        from core.db import DatabaseConnectionError
        with mock.patch("core.db.connection.sqlite3.connect", side_effect=sqlite3.OperationalError("no")):
            with self.assertRaises(DatabaseConnectionError) as ctx:
                get_connection(timeout=1.0)
            self.assertEqual(ctx.exception.backend, BACKEND_SQLITE)


# ─────────────────────────────────────────────────────────────────────
# B2) Dual backend contract — E.2.3 and E.2.4 (no server required)
# ─────────────────────────────────────────────────────────────────────
class PostgresSpecificGuardTests(unittest.TestCase):
    """Methods that make sense on SQLite and on PG must have explicit behavior."""

    def _pg(self, raw=None):
        """Helper: pg."""
        return ConnectionAdapter(raw or _StubConnection(), backend=BACKEND_POSTGRESQL)

    def test_lastrowid_on_postgresql_raises_with_actionable_message(self):
        """Verifies lastrowid on postgresql raises with actionable message."""
        conn = self._pg()
        with self.assertRaises(NotImplementedError) as ctx:
            _ = conn.lastrowid
        msg = str(ctx.exception)
        self.assertIn("RETURNING", msg)
        self.assertIn("E.4", msg)

    def test_lastrowid_on_sqlite_still_forwards(self):
        """SQLite has not changed (phase D contract)."""
        conn = ConnectionAdapter(_StubConnection(), backend=BACKEND_SQLITE)
        conn._last_cursor = mock.Mock(lastrowid=42)
        self.assertEqual(conn.lastrowid, 42)

    def test_executescript_on_postgresql_splits_and_executes(self):
        """
        E.4.7 — On PG, the script is broken into statements and each one is executed separately
        (psycopg only accepts multi-commands in the simple protocol: with a non-empty parameter
        gives "cannot insert multiple commands into a prepared statement").
        """
        raw = _StubConnection()
        conn = ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)
        conn.executescript("CREATE TABLE a (x INT);\n-- کامنت\nCREATE TABLE b (y INT);")
        self.assertEqual([sql for _op, sql, _p in raw.log],
                         ["CREATE TABLE a (x INT)", "-- کامنت\nCREATE TABLE b (y INT)"])
        # Placeholder translation is applied to every statement
        raw2 = _StubConnection()
        conn2 = ConnectionAdapter(raw2, backend=BACKEND_POSTGRESQL)
        conn2.executescript("SELECT '50%' WHERE a = ?; SELECT 2;")
        self.assertEqual(raw2.log[0][1], "SELECT '50%%' WHERE a = %s")

    def test_executescript_rejects_dollar_quoted_scripts(self):
        """Verifies executescript rejects dollar quoted scripts."""
        conn = self._pg()
        with self.assertRaises(ValueError) as ctx:
            conn.executescript("CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;")
        self.assertIn("dollar", str(ctx.exception))

    def test_row_factory_is_noop_on_postgresql_and_forwarded_on_sqlite(self):
        """Verifies row factory is noop on postgresql and forwarded on sqlite."""
        pg = self._pg()
        pg.row_factory = sqlite3.Row          # It should not give an error
        self.assertIsNone(pg.row_factory)     # And it should have no effect

        sqlite_raw = _StubConnection()
        sq = ConnectionAdapter(sqlite_raw, backend=BACKEND_SQLITE)
        sq.row_factory = sqlite3.Row
        self.assertIs(sq.row_factory, sqlite3.Row)

    def test_no_active_cursor_error_type_matches_backend(self):
        """Verifies no active cursor error type matches backend."""
        import psycopg
        with self.assertRaises(sqlite3.ProgrammingError):
            ConnectionAdapter(_StubConnection(), backend=BACKEND_SQLITE).fetchone()
        with self.assertRaises(psycopg.ProgrammingError):
            self._pg().fetchone()

    def test_cursor_returns_raw_driver_cursor(self):
        """Verifies cursor returns raw driver cursor."""
        raw = _StubConnection()
        self.assertIsInstance(self._pg(raw).cursor(), _StubCursor)


class ResolveDsnTests(unittest.TestCase):
    """E.2.4 — Explicit determination of connection address (parameter ⇒ PG_DSN ⇒ ConfigurationError)."""

    def test_explicit_dsn_wins(self):
        """Verifies explicit dsn wins."""
        from core.db import resolve_dsn
        with mock.patch.dict(os.environ, {ENV_PG_DSN: "host=env-db"}):
            self.assertEqual(resolve_dsn(BACKEND_POSTGRESQL, "host=explicit"), "host=explicit")

    def test_env_dsn_used_when_no_explicit(self):
        """Verifies env dsn used when no explicit."""
        from core.db import resolve_dsn
        with mock.patch.dict(os.environ, {ENV_PG_DSN: "  host=env-db  "}):
            self.assertEqual(resolve_dsn(BACKEND_POSTGRESQL), "host=env-db")

    def test_missing_dsn_raises_configuration_error(self):
        """Verifies missing dsn raises configuration error."""
        from core.db import resolve_dsn
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_PG_DSN, None)
            with self.assertRaises(ConfigurationError) as ctx:
                resolve_dsn(BACKEND_POSTGRESQL)
            self.assertIn(ENV_PG_DSN, str(ctx.exception))
            self.assertEqual(ctx.exception.backend, BACKEND_POSTGRESQL)

    def test_sqlite_has_no_dsn_and_unknown_backend_raises(self):
        """Verifies sqlite has no dsn and unknown backend raises."""
        from core.db import resolve_dsn
        self.assertIsNone(resolve_dsn(BACKEND_SQLITE))
        with self.assertRaises(UnsupportedBackendError):
            resolve_dsn("mysql")

    def test_connect_without_dsn_raises_before_touching_psycopg(self):
        """Even if psycopg is installed, no connection will be attempted without a DSN."""
        import core.db as pkg
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_PG_DSN, None)
            with self.assertRaises(ConfigurationError):
                pkg.connect(backend=BACKEND_POSTGRESQL)

    def test_adapter_rejects_unknown_backend_early(self):
        """Verifies adapter rejects unknown backend early."""
        with self.assertRaises(UnsupportedBackendError):
            ConnectionAdapter(_StubConnection(), backend="mysql")
        with self.assertRaises(UnsupportedBackendError):
            ConnectionAdapter(_StubConnection(), backend="postgres")  # The alternate name is not valid here


# ─────────────────────────────────────────────────────────────────────
# C) placeholder translation (phase E contract)
# ─────────────────────────────────────────────────────────────────────
class PlaceholderTranslationTests(unittest.TestCase):

    """Tests for placeholder translation."""
    def test_sqlite_is_identity(self):
        """Verifies sqlite is identity."""
        sql = "SELECT * FROM t WHERE a = ? AND b = ?"
        self.assertIs(translate_placeholders(sql, BACKEND_SQLITE), sql)

    def test_postgresql_translation(self):
        """Verifies postgresql translation."""
        self.assertEqual(
            translate_placeholders("SELECT * FROM t WHERE a = ? AND b = ?",
                                   BACKEND_POSTGRESQL),
            "SELECT * FROM t WHERE a = %s AND b = %s")

    def test_translation_skips_strings_identifiers_and_comments(self):
        """Verifies translation skips strings identifiers and comments."""
        sql = ('SELECT "col?", \'lit?\' FROM t -- comment ?\n'
               '/* block ? */ WHERE a = ?  /* tail ? */')
        out = translate_placeholders(sql, BACKEND_POSTGRESQL)
        self.assertIn('"col?"', out)
        self.assertIn("'lit?'", out)
        self.assertIn("-- comment ?", out)
        self.assertIn("/* block ? */", out)
        self.assertIn("-- comment ?\n", out)
        self.assertTrue(out.endswith("/* tail ? */"))
        self.assertIn("WHERE a = %s", out)
        self.assertEqual(out.count("%s"), 1)

    def test_escaped_quotes_inside_strings(self):
        """Verifies escaped quotes inside strings."""
        self.assertEqual(
            translate_placeholders("SELECT 'it''s ?' WHERE a = ?", BACKEND_POSTGRESQL),
            "SELECT 'it''s ?' WHERE a = %s")

    def test_unknown_backend_and_bad_input_raise(self):
        """Verifies unknown backend and bad input raise."""
        from core.db import PlaceholderTranslationError
        with self.assertRaises(PlaceholderTranslationError):
            translate_placeholders("SELECT ?", "mysql")
        with self.assertRaises(PlaceholderTranslationError):
            translate_placeholders(None, BACKEND_POSTGRESQL)

    # ── E.2.2: escape percentage for psycopg
    def test_percent_escape_default_is_per_backend(self):
        """Default: postgresql ⇒ escape, sqlite ⇒ intact (no changes)."""
        sql = "SELECT 'نرخ 50%' || ?"
        self.assertEqual(
            translate_placeholders(sql, BACKEND_POSTGRESQL),
            "SELECT 'نرخ 50%%' || %s")
        self.assertEqual(translate_placeholders(sql, BACKEND_SQLITE), sql)

    def test_percent_escape_covers_strings_comments_and_operators(self):
        """Verifies percent escape covers strings comments and operators."""
        from core.db import escape_percent_signs
        sql = "SELECT 'a%b', n % 2 -- 10% comment\n/* 5% */ WHERE x LIKE '%y%' AND z = ?"
        out = translate_placeholders(sql, BACKEND_POSTGRESQL)
        # No `%` is left alone (all are paired) and the only placeholder was a `?`
        self.assertNotIn("%", out.replace("%%", "").replace("%s", ""))
        self.assertEqual(out.count("%s"), 1)
        self.assertEqual(out.count("%%"), 6)
        self.assertEqual(out, escape_percent_signs(sql).replace("?", "%s"))

    def test_escape_happens_before_placeholder_insertion(self):
        """The order is important: newly created `%s's must not be re-escaped."""
        out = translate_placeholders("SELECT '100%' || ? || '50%'", BACKEND_POSTGRESQL)
        self.assertEqual(out, "SELECT '100%%' || %s || '50%%'")

    def test_escape_can_be_disabled_or_rejected_for_sqlite(self):
        """Verifies escape can be disabled or rejected for sqlite."""
        from core.db import PlaceholderTranslationError
        self.assertEqual(
            translate_placeholders("SELECT '100%' || ?", BACKEND_POSTGRESQL,
                                   escape_percent=False),
            "SELECT '100%' || %s")
        with self.assertRaises(PlaceholderTranslationError):
            translate_placeholders("SELECT '100%' || ?", BACKEND_SQLITE,
                                   escape_percent=True)

    def test_sqlite_path_is_untouched_even_with_percent(self):
        """adb: SQLite path is never escaped (the behavior of sqlite3 does not change)."""
        sql = "SELECT '100%' FROM t WHERE a = ?"
        self.assertIs(translate_placeholders(sql, BACKEND_SQLITE), sql)


class AdapterSqlPathTests(unittest.TestCase):
    """
    SQL path passing through adapter: sqlite intact, translated postgresql.

    It is done with a dummy connection so that the contract is locked without the need for a PG server
    (Actual test on PG in PGContractTests class with availability guard).
    """

    def test_postgresql_translates_before_sending(self):
        """Verifies postgresql translates before sending."""
        raw = _StubConnection()
        conn = ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)
        conn.execute("SELECT '100%' FROM t WHERE a = ? AND b = ?", (1, 2))
        self.assertEqual(raw.log[-1][1], "SELECT '100%%' FROM t WHERE a = %s AND b = %s")
        self.assertEqual(raw.log[-1][2], (1, 2))

    def test_postgresql_executemany_uses_cursor(self):
        """psycopg.Connection does not have executemany method ⇒ adapter must go from cursor."""
        raw = _StubConnection()
        conn = ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)
        conn.executemany("INSERT INTO t (a) VALUES (?)", [(1,), (2,)])
        self.assertEqual(raw.log[-1][0], "executemany")
        self.assertEqual(raw.log[-1][1], "INSERT INTO t (a) VALUES (%s)")
        self.assertEqual(raw.log[-1][2], [(1,), (2,)])

    def test_sqlite_path_sends_sql_untouched(self):
        """Verifies sqlite path sends sql untouched."""
        raw = _StubConnection()   # Only the registrar; The behavior of sqlite3 in tests is high
        conn = ConnectionAdapter(raw, backend=BACKEND_SQLITE)
        conn.execute("SELECT '100%' FROM t WHERE a = ?", (1,))
        self.assertEqual(raw.log[-1][1], "SELECT '100%' FROM t WHERE a = ?")

    def test_unexpected_translation_failure_is_wrapped(self):
        """Any unexpected translation errors are closed with PlaceholderTranslationError."""
        from core.db import PlaceholderTranslationError
        conn = ConnectionAdapter(_StubConnection(), backend=BACKEND_POSTGRESQL)
        with mock.patch("core.db.connection.translate_placeholders",
                        side_effect=ValueError("boom")):
            with self.assertRaises(PlaceholderTranslationError):
                conn.execute("SELECT 1")

    def test_app_code_uses_no_raw_cursor(self):
        """
        E.4.6 — Application code (``core/`` and ``web/``) must not take its own ``cursor()''.

        Reason: ``cursor()'' is **raw** and its SQL is from placeholder translation and common behavior
        adapter does not pass; On PG, that is, SQL must be written bi-style (bug source).
        Exception: ``core/db/connection.py'' itself, which is the adapter implementation.
        """
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for base in ("core", "web"):
            for path in sorted((root / base).rglob("*.py")):
                if path.name == "connection.py" and path.parent.name == "db":
                    continue
                for lineno, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), start=1):
                    code = line.split("#", 1)[0]
                    if re.search(r"\.cursor\s*\(", code):
                        offenders.append(f"{path.relative_to(root)}:{lineno}")
        self.assertEqual(offenders, [], "cursor خام در کد برنامه پیدا شد")


class SplitSqlStatementsTests(unittest.TestCase):
    """E.4.7 — Script breaking: ``;'' is not a string/comment separator."""

    def _split(self, script):
        """Helper: split."""
        from core.db.scripts import split_sql_statements
        return split_sql_statements(script)

    def test_simple_script(self):
        """Verifies simple script."""
        self.assertEqual(self._split("CREATE TABLE a (x INT); CREATE TABLE b (y INT);"),
                         ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"])

    def test_semicolon_inside_string_literal(self):
        """Verifies semicolon inside string literal."""
        self.assertEqual(self._split("INSERT INTO t VALUES ('a;b'); SELECT 1;"),
                         ["INSERT INTO t VALUES ('a;b')", "SELECT 1"])

    def test_escaped_quotes(self):
        """Verifies escaped quotes."""
        self.assertEqual(self._split("SELECT 'it''s; fine';"),
                         ["SELECT 'it''s; fine'"])

    def test_quoted_identifier(self):
        """Verifies quoted identifier."""
        self.assertEqual(self._split('CREATE TABLE "we;ird" (x INT);'),
                         ['CREATE TABLE "we;ird" (x INT)'])

    def test_comments_are_not_separators(self):
        """Verifies comments are not separators."""
        self.assertEqual(self._split("SELECT 1; /* a; b */ SELECT 2;"),
                         ["SELECT 1", "/* a; b */ SELECT 2"])
        self.assertEqual(self._split("SELECT 1; -- کامنت با ; داخل\nSELECT 2;"),
                         ["SELECT 1", "-- کامنت با ; داخل\nSELECT 2"])

    def test_trailing_and_empty_statements(self):
        """Verifies trailing and empty statements."""
        self.assertEqual(self._split("SELECT 1"), ["SELECT 1"])
        self.assertEqual(self._split("  ;;\n;  "), [])
        self.assertEqual(self._split("-- فقط کامنت\n"), [])

    def test_invalid_scripts_raise_clear_errors(self):
        """Verifies invalid scripts raise clear errors."""
        with self.assertRaises(ValueError):
            self._split("SELECT 'unterminated")
        with self.assertRaises(ValueError):
            self._split("SELECT 1 /* unterminated")
        with self.assertRaises(ValueError):
            self._split("DO $$ BEGIN END $$;")
        with self.assertRaises(ValueError):
            self._split(None)


class _ReturningStubCursor(_StubCursor):
    """cursor Stub that returns a row (for ``RETURNING id'' path)."""

    def __init__(self, log, row):
        """Initialize the instance."""
        super().__init__(log)
        self._row = row

    def fetchone(self):
        """Helper: fetchone."""
        return self._row


class _ReturningStubConnection(_StubConnection):
    """psycopg style stub connection with configurable ``fetchone''."""

    def __init__(self, row=None):
        """Initialize the instance."""
        super().__init__()
        self._row = row

    def execute(self, sql, params=()):
        """Helper: execute."""
        self.log.append(("execute", sql, params))
        return _ReturningStubCursor(self.log, self._row)


# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class InsertReturningIdTests(_TempDbBase):
    """
    E.4.3 — "INSERT + row ID" with a common signature on both backends.

    ``lastrowid'' is a SQLite concept and does not exist on PG; PG correct contract
    ``INSERT... RETURNING id'' is This SQLite class path on a real DB and path
    Locks PG with psycopg style stub (serverless); Real PG in
    ``PostgresContractTests.test_insert_returning_id_on_real_postgres``.
    """

    def _sqlite(self):
        """Helper: sqlite."""
        return get_connection(timeout=5.0)

    # ── Path to SQLite (real DB; independent read with sqlite3) ─────────
    def test_sqlite_returns_autoincrement_id(self):
        """Verifies sqlite returns autoincrement id."""
        conn = self._sqlite()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
            self.assertEqual(
                conn.insert_returning_id("INSERT INTO t (name) VALUES (?)", ("a",)), 1)
            self.assertEqual(
                conn.insert_returning_id("INSERT INTO t (name) VALUES (?)", ("b",)), 2)
            conn.commit()
            self.assertEqual(self.physical(("SELECT id, name FROM t",)),
                             [[(1, "a"), (2, "b")]])
        finally:
            conn.close()

    def test_sqlite_zero_row_insert_returns_none_not_stale_lastrowid(self):
        """
        Logging a real difference: ``sqlite3'' calls ``lastrowid'' when no rows are inserted
        It leaves intact (the value of the previous insert) ⇒ reading it raw would have given the wrong ID.
        The ``insert_returning_id'' convention is ``None'' in this case.
        """
        conn = self._sqlite()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
            self.assertEqual(
                conn.insert_returning_id("INSERT INTO t (name) VALUES (?)", ("a",)), 1)
            stale = conn.execute("INSERT INTO t (name) SELECT ? WHERE 0", ("z",)).lastrowid
            self.assertEqual(stale, 1)      # Prove that the problem is real
            self.assertIsNone(
                conn.insert_returning_id("INSERT INTO t (name) SELECT ? WHERE 0", ("z",)))
            conn.commit()
            self.assertEqual(self.physical(("SELECT count(*) FROM t",)), [[(1,)]])
        finally:
            conn.close()

    def test_sqlite_params_default_to_empty(self):
        """Verifies sqlite params default to empty."""
        conn = self._sqlite()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
            self.assertEqual(conn.insert_returning_id("INSERT INTO t (name) VALUES ('x')"), 1)
        finally:
            conn.close()

    # ── PostgreSQL path (psycopg style stub, no server) ────────────
    def test_postgresql_appends_returning_and_returns_id(self):
        """Verifies postgresql appends returning and returns id."""
        stub = _ReturningStubConnection(row=(7,))
        conn = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertEqual(
            conn.insert_returning_id("INSERT INTO t (name) VALUES (?)", ("a",)), 7)
        self.assertEqual(
            stub.log, [("execute", "INSERT INTO t (name) VALUES (%s) RETURNING id", ("a",))])

    def test_postgresql_strips_trailing_semicolon_and_whitespace(self):
        """Verifies postgresql strips trailing semicolon and whitespace."""
        stub = _ReturningStubConnection(row=(1,))
        conn = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        conn.insert_returning_id("INSERT INTO t (name)\nVALUES (?);\t \n", ("a",))
        self.assertEqual(len(stub.log), 1)
        self.assertTrue(stub.log[0][1].endswith("VALUES (%s) RETURNING id"), stub.log[0][1])
        self.assertNotIn(";", stub.log[0][1].splitlines()[-1])

    def test_postgresql_returns_none_when_no_row(self):
        """Verifies postgresql returns none when no row."""
        stub = _ReturningStubConnection(row=None)
        conn = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertIsNone(
            conn.insert_returning_id("INSERT INTO t (name) SELECT 'x' WHERE false"))

    def test_existing_returning_is_rejected_before_executing(self):
        """Verifies existing returning is rejected before executing."""
        for backend in (BACKEND_SQLITE, BACKEND_POSTGRESQL):
            with self.subTest(backend=backend):
                stub = _ReturningStubConnection()
                conn = ConnectionAdapter(stub, backend=backend)
                with self.assertRaises(ValueError):
                    conn.insert_returning_id(
                        "INSERT INTO t (name) VALUES (?) RETURNING id", ("a",))
                self.assertEqual(stub.log, [])   # No SQL was executed


def _pg_test_target():
    """
    PG address for integrated tests: ``PG_DSN'' + a read-only probe.

    Returns:
        ``(dsn, None)'' if available, else ``(None, skip reason)'' — reason
        It is intentionally left on so that "why these tests failed" can be seen in the pytest output.
    """
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ تست‌های یکپارچه‌ی PG اجرا نمی‌شوند "
                      f"(اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ تست‌های یکپارچه رد می‌شوند")
    return dsn, None


_PG_TEST_DSN, _PG_SKIP_REASON = _pg_test_target()


@unittest.skipUnless(_PG_TEST_DSN, _PG_SKIP_REASON or "")
class PostgresContractTests(unittest.TestCase):
    """
    Same adapter contract, this time on **real PostgreSQL** (E.2.5).

    Principles:
      • No application tables are touched: each test just ``CREATE TEMP TABLE''
        (it leaves at the end of the session) and rolls back at the end;
      • Without ``PG_DSN'' or with the server off, the class is skipped with a clear message (no error).
    """

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_TEST_DSN:  # Second Guard: If someone picked up skipUnless
            self.skipTest(_PG_SKIP_REASON or "PG در دسترس نیست")
        self.conn = connect_postgres(_PG_TEST_DSN, timeout=5.0)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        """Helper: cleanup."""
        try:
            self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def _temp_table(self):
        """Helper: temp table."""
        self.conn.execute("CREATE TEMP TABLE t_pg (id INTEGER, name TEXT, n INTEGER)")
        self.conn.commit()   # DDL in PG is transactional ⇒ to persist in the same session

    # Connection
    def test_connection_contract(self):
        """Verifies connection contract."""
        self.assertEqual(self.conn.backend, BACKEND_POSTGRESQL)
        self.assertFalse(self.conn.raw_connection.autocommit)
        self.assertEqual(self.conn.execute("SHOW timezone").fetchone()[0], "UTC")
        version = self.conn.execute("SHOW server_version").fetchone()[0]
        major_version = int(version.split(".")[0])
        self.assertGreaterEqual(major_version, 16, version)
        self.conn.rollback()

    # ── Same SQLite contract, this time on PG ─────────────────────────
    def test_execute_insert_select_with_question_marks(self):
        """Verifies execute insert select with question marks."""
        self._temp_table()
        cur = self.conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)",
                                (1, "alpha", 10))
        self.assertEqual(cur.rowcount, 1)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT id, name, n FROM t_pg").fetchone(),
                         (1, "alpha", 10))
        self.assertEqual([c[0] for c in self.conn.execute(
            "SELECT id, name, n FROM t_pg").description], ["id", "name", "n"])

    def test_executemany_is_single_transaction(self):
        """Verifies executemany is single transaction."""
        self._temp_table()
        self.conn.executemany("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)",
                              [(1, "a", 1), (2, "b", 2), (3, "c", 3)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM t_pg").fetchone()[0], 3)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM t_pg").fetchone()[0], 0)

    def test_commit_persists_and_rollback_discards(self):
        """Verifies commit persists and rollback discards."""
        self._temp_table()
        self.conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)", (1, "keep", 1))
        self.conn.commit()
        self.conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)", (2, "drop", 2))
        self.conn.rollback()
        rows = self.conn.execute("SELECT id FROM t_pg ORDER BY id").fetchall()
        self.assertEqual(rows, [(1,)])

    def test_percent_literals_work_with_and_without_params(self):
        """Verifies percent literals work with and without params."""
        self.assertEqual(self.conn.execute("SELECT '100%'").fetchone()[0], "100%")
        self.assertEqual(self.conn.execute("SELECT 'نرخ 50%', ?", (7,)).fetchone(),
                         ("نرخ 50%", 7))
        self.assertTrue(self.conn.execute("SELECT ? LIKE '%alp%'", ("alpha",)).fetchone()[0])
        self.conn.rollback()

    def test_context_manager_commits(self):
        """Verifies context manager commits."""
        self._temp_table()
        with self.conn as conn:                      # __exit__ ⇒ commit
            conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)", (1, "ctx", 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM t_pg").fetchone()[0], 1)

    # ── Documentary PG differences ──────────────────────────────────────────
    def test_lastrowid_raises_and_returning_is_the_way(self):
        """Verifies lastrowid raises and returning is the way."""
        self._temp_table()
        with self.assertRaises(NotImplementedError):
            _ = self.conn.lastrowid
        new_id = self.conn.execute(
            "INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?) RETURNING id",
            (5, "ret", 5)).fetchone()[0]
        self.assertEqual(new_id, 5)

    def test_cursor_is_raw_so_sql_uses_driver_style(self):
        """
        ``cursor()'' is **raw** ⇒ placeholder translation is not applied to it and SQL
        Must be written in the driver's own style (on PG: ``%s'').

        This is a "measured finding", not a taste: adapter only
        ``execute/executemany/executescript'' translates itself. So ``cursor()'' for
        The tool/test remains, but **app code doesn't use it**: consumers
        old(``web/routes/stats.py``, ``web/routes/metrics.py``, ``init_db``) at
        E.4.6 were moved to ``conn.execute'' and
        ``test_app_code_uses_no_raw_cursor'' prevents them from returning.
        """
        self._temp_table()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO t_pg (id, name, n) VALUES (%s, %s, %s)", (1, "cur", 1))
        self.conn.commit()
        self.assertEqual(cur.execute("SELECT count(*) FROM t_pg").fetchone()[0], 1)
        # And we prove that the qmark style **doesn't work** on the raw cursor (explicit contract):
        with self.assertRaises(Exception):
            cur.execute("SELECT count(*) FROM t_pg WHERE id = ?", (1,))

    def test_executescript_runs_on_real_postgres(self):
        """E.4.7 On the real PG: The two-command script actually runs."""
        self.conn.executescript(
            "CREATE TEMP TABLE scr_a (x INT);\nCREATE TEMP TABLE scr_b (y INT);")
        self.conn.execute("INSERT INTO scr_a (x) VALUES (1)")
        self.conn.execute("INSERT INTO scr_b (y) VALUES (2)")
        self.assertEqual(self.conn.execute("SELECT x FROM scr_a").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT y FROM scr_b").fetchone()[0], 2)
        self.conn.rollback()

    def test_lastrowid_is_the_only_remaining_sqlite_only_feature(self):
        """The only remaining SQLite-only effect is ``lastrowid'' (solution: E.4.3)."""
        with self.assertRaises(NotImplementedError):
            _ = self.conn.lastrowid

    def test_insert_returning_id_on_real_postgres(self):
        """
        E.4.3 On real PG: ID comes from ``RETURNING id'', ids are concatenated and
        Multiline/semicolon SQL also works (without ``lastrowid'').
        """
        self.conn.execute(
            "CREATE TEMP TABLE t_ids (id BIGINT GENERATED BY DEFAULT AS IDENTITY, name TEXT)")
        self.conn.commit()
        first = self.conn.insert_returning_id("INSERT INTO t_ids (name) VALUES (?)", ("a",))
        second = self.conn.insert_returning_id(
            "INSERT INTO t_ids (name)\nVALUES (?);\n", ("b",))
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.conn.execute("SELECT id, name FROM t_ids ORDER BY id").fetchall(),
                         [(1, "a"), (2, "b")])
        # Zero row ⇒ None (not the ID of the previous insert)
        self.assertIsNone(
            self.conn.insert_returning_id("INSERT INTO t_ids (name) SELECT 'x' WHERE false"))
        self.conn.rollback()


# ─────────────────────────────────────────────────────────────────────
# D) compatibility of existing code
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class ExistingCodeCompatibilityTests(_TempDbBase):

    """Tests for existing code compatibility."""
    def test_core_db_public_api_works_standalone(self):
        """Package public API: core.db.db_connection should be able to be used independently."""
        with db_connection(commit=True) as conn:
            self.assertIsInstance(conn, ConnectionAdapter)
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("pkg", 1))
        self.assertTrue(conn.closed)
        self.assertEqual([r[1] for r in self.physical()[0]], ["pkg"])

    def test_core_database_db_connection_is_adapter(self):
        """Verifies core database db connection is adapter."""
        import core.database as database
        with database.db_connection() as conn:
            self.assertIsInstance(conn, ConnectionAdapter)
            self.assertEqual(conn.backend, BACKEND_SQLITE)

    def test_security_audit_db_conn_is_adapter_and_writes(self):
        """Verifies security audit db conn is adapter and writes."""
        from core.security_audit import _db_conn, init_security_audit
        init_security_audit()
        with _db_conn(commit=True) as conn:
            self.assertIsInstance(conn, ConnectionAdapter)
            conn.execute(
                "INSERT INTO security_events (timestamp, event_type, severity, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("2026-10-03T00:00:00", "test_event", "info", "2026-10-03T00:00:00"))
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT event_type FROM security_events WHERE event_type = ?",
                ("test_event",)).fetchone()
            self.assertEqual(row[0], "test_event")

    def test_add_event_persists_through_adapter(self):
        """Full production path: init_db → add_event (which creates the connection itself)."""
        import core.database as database
        database.init_db()
        database.add_event("192.0.2.99", "PRINT",
                           {"pages": 3, "message": "adapter-compat",
                            "timestamp": "2026-10-03T10:00:00"})
        rows = database.get_log("192.0.2.99")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pages"], 3)
        self.assertEqual(rows[0]["type"], "PRINT")
        # Independent physical readout (no adapter) to ensure real writing
        raw = sqlite3.connect("logs.db")
        try:
            types = [r[0] for r in raw.execute(
                "SELECT type FROM logs WHERE printer_ip = ?", ("192.0.2.99",))]
        finally:
            raw.close()
        self.assertEqual(types, ["PRINT"])

    def test_default_timeout_is_thirty_seconds(self):
        """signature core.db.db_connection: default 30s (phase D) vs old 10s."""
        import inspect
        from core.db import db_connection as core_conn
        import core.database as database
        self.assertEqual(inspect.signature(core_conn).parameters["timeout"].default, 30.0)
        self.assertEqual(inspect.signature(database.db_connection).parameters["timeout"].default, 10.0)

    def test_create_user_uses_insert_returning_id(self):
        """
        E.4.3 — The only live use of ``lastrowid'' (``core.database.create_user'') to
        ``insert_returning_id'' passed; This test prevents the shutdown return
        (On PG, ``lastrowid'' does not exist ⇒ ``create_user'' silently returns None).
        """
        import inspect
        import core.database as database
        src = inspect.getsource(database.create_user)
        self.assertIn("insert_returning_id", src)
        # Comments may explain lastrowid's name; The benchmark is uncommented code:
        code_only = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        self.assertNotIn("lastrowid", code_only)

        database.init_db()
        user = database.create_user("e43_regression", "e43.regression@example.com", "h")
        self.assertIsNotNone(user)
        self.assertIsInstance(user["id"], int)
        self.assertGreater(user["id"], 0)
        self.assertEqual(database.get_user_by_id(user["id"])["username"], "e43_regression")
        # The invalid username rejection path (before any connection) is left intact
        self.assertIsNone(database.create_user("bad name!", "x@example.com", "h"))

    def test_no_silent_fallback_on_error(self):
        """If the connection fails, an explicit error is thrown (no fallback to another backend)."""
        import core.database as database
        from core.db import DatabaseConnectionError
        with mock.patch("core.db.connection.sqlite3.connect", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(DatabaseConnectionError):
                with database.db_connection() as conn:
                    conn.execute("SELECT 1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
