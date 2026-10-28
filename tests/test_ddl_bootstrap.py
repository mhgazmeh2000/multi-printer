# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_ddl_bootstrap.py
# @brief:        Tests for backend-aware DDL bootstrapping (E.4.5).
# @details:      Responsibilities: Check SQLite and PostgreSQL schema creation paths.
#                Main API: _StubCursor, _SqlRecordingConnection, _RecordingCursorProxy,
#                _RecordingRawConnection, SqliteBootstrapTests, PostgresBootstrapNoDdlTests
#                Side effects: database access
# @dependencies: internal: core.database, core.db, core.db.connection, core.db.schema,
#                core.security_audit, core.yield_engine | external: psycopg, pytest
# @notes:        Can be run directly as a script. PostgreSQL-live tests skip automatically when no
#                server is reachable.
# =============================================================================

"""
E.4.5 tests — backend-aware DDL.

The rule that locks this file:
  • On **SQLite**, the bootstrap functions create tables/indexes as before and
    set runtime pragmas (`busy_timeout`/`journal_mode`/`synchronous`);
  • On **PostgreSQL** these functions are **no-op**: no DDL and no PRAGMA are executed.
    Because ``Alembic'' is the only source of DDL. They only confirm that the schema is available and it was not
    They give a clear error (RuntimeError with workaround) — not a silent failure.

Coverage:
  A) SQLite (real DB in temporary folder): creation of tables, idempotency, and PRAGMAs
     They still run (with proxy connection, no server).
  B) PostgreSQL with dummy connection: zero PRAGMA/DDL, clear error on no table, close connection.
  C) Real PostgreSQL (with ``PG_DSN''): three error-free bootstrap functions and Alembic schema
     complete Without ``PG_DSN'' it will be skipped with clear message.
"""
import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

import pytest

from core.db import (
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ConnectionAdapter,
    DatabaseError,
    ENV_PG_DSN,
    db_connection,
)
from core.db.schema import (
    ALEMBIC_TABLE,
    APP_TABLES,
    SECURITY_TABLES,
    YIELD_TABLES,
    missing_tables,
    present_tables,
    schema_columns,
    verify_postgres_tables,
)


# ─────────────────────────────────────────────────────────────────────
# Stubs (no server)
# ─────────────────────────────────────────────────────────────────────
class _StubCursor:
    """Dummy cursor: returns predefined rows."""

    def __init__(self, rows):
        """Initialize the instance."""
        self._rows = list(rows)
        self.rowcount = -1

    def fetchone(self):
        """Helper: fetchone."""
        return self._rows[0] if self._rows else None

    def fetchall(self):
        """Helper: fetchall."""
        return list(self._rows)

    def __iter__(self):
        """Iterate over the items."""
        return iter(self._rows)


class _SqlRecordingConnection:
    """psycopg style dummy connection that logs any SQL."""

    def __init__(self, rows=()):
        """Initialize the instance."""
        self.log = []
        self.rows = list(rows)
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def execute(self, sql, params=()):
        """Helper: execute."""
        self.log.append((sql, params))
        return _StubCursor(self.rows)

    def commit(self):
        """Helper: commit."""
        self.committed += 1

    def rollback(self):
        """Helper: rollback."""
        self.rolled_back += 1

    def close(self):
        """Helper: close."""
        self.closed = True

    # ── Help with test tasks ──
    def statements(self):
        """Helper: statements."""
        return [sql for sql, _params in self.log]

    def ddl_or_pragma(self):
        """Any statement that is DDL/PRAGMA (must always be empty on PG)."""
        out = []
        for sql in self.statements():
            head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
            if head in ("PRAGMA", "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "GRANT"):
                out.append(sql.strip())
        return out


class _RecordingCursorProxy:
    """Helper class: recording cursor proxy."""
    def __init__(self, cur, log):
        """Initialize the instance."""
        self._cur = cur
        self._log = log

    def execute(self, sql, params=()):
        """Helper: execute."""
        self._log.append(sql)
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        """Special method __getattr__."""
        return getattr(self._cur, name)


class _RecordingRawConnection:
    """A raw sqlite3 connection proxy that logs executed SQLs."""

    def __init__(self, raw):
        """Initialize the instance."""
        self._raw = raw
        self.sql = []

    def cursor(self):
        """Helper: cursor."""
        return _RecordingCursorProxy(self._raw.cursor(), self.sql)

    def execute(self, sql, params=()):
        """Helper: execute."""
        self.sql.append(sql)
        return _RecordingCursorProxy(self._raw.execute(sql, params), self.sql)

    def __getattr__(self, name):
        """Special method __getattr__."""
        return getattr(self._raw, name)


@contextmanager
def _fake_db_connection(conn):
    """Substitute context manager for modules that create connections themselves."""
    yield conn


def _pg_test_target():
    """``(dsn, None)'' if PG is available, otherwise ``(None, skip reason)''."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ تست‌های PG این فایل اجرا نمی‌شوند "
                      f"(اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ تست‌های PG رد می‌شوند")
    return dsn, None


_PG_TEST_DSN, _PG_SKIP_REASON = _pg_test_target()


# ─────────────────────────────────────────────────────────────────────
# A) SQLite — previous behavior intact
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: This test writes SQLite/schema specific to SQLite ⇒ below
# `DB_BACKEND=postgresql` is skipped with auto-skip (tests/conftest.py, docs/phase-e-plan.md §5.3).
@pytest.mark.sqlite_only
class SqliteBootstrapTests(unittest.TestCase):
    """The bootstrap functions on SQLite do the same as usual (without regression)."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        """Helper: restore."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _tables(self):
        """Helper: tables."""
        with db_connection() as conn:
            self.assertEqual(conn.backend, BACKEND_SQLITE)
            return present_tables(conn)

    def test_init_db_creates_its_tables_and_is_idempotent(self):
        """Verifies init db creates its tables and is idempotent."""
        import core.database as database
        database.init_db()
        first = self._tables()
        for table in ("logs", "printer_counters", "toner_history", "sensor_readings", "users"):
            self.assertIn(table, first)
        database.init_db()          # The second run: it should not give an error and the schema should not be changed
        self.assertEqual(self._tables(), first)

    def test_yield_and_security_initializers_create_their_tables(self):
        """Verifies yield and security initializers create their tables."""
        import core.security_audit as audit
        import core.yield_engine as yield_engine
        yield_engine.ensure_yield_tables()
        audit.init_security_audit()
        tables = self._tables()
        for table in YIELD_TABLES + SECURITY_TABLES:
            self.assertIn(table, tables)
        yield_engine.ensure_yield_tables()      # idempotent
        audit.init_security_audit()
        self.assertEqual(self._tables(), tables)

    def test_schema_columns_reads_declaration_order_on_sqlite(self):
        """``schema_columns'' on SQLite is the output of ``PRAGMA table_info''."""
        with db_connection() as conn:
            self.assertEqual(conn.backend, BACKEND_SQLITE)
            conn.execute("CREATE TABLE probe (b TEXT, a INTEGER, c TEXT)")
            self.assertEqual(schema_columns(conn, "probe"), ["b", "a", "c"])
            self.assertEqual(schema_columns(conn, "no_such_table"), [])

    def test_schema_columns_quotes_identifier(self):
        """The table name is enclosed in space/quotation (syntax forbidden, not injection)."""
        with db_connection() as conn:
            conn.execute('CREATE TABLE "weird name" (x TEXT)')
            self.assertEqual(schema_columns(conn, "weird name"), ["x"])
            self.assertEqual(schema_columns(conn, 'weird" name'), [])

    def test_sqlite_branch_still_runs_the_runtime_pragmas(self):
        """
        PRAGMAs (line 12) must remain on the SQLite path; Only the PG track of them
        will be cleared Here, with the connection proxy, executed SQLs are recorded.
        """
        import core.database as database
        from core.db.connection import connect_sqlite
        real = connect_sqlite("logs.db")
        proxy = _RecordingRawConnection(real.raw_connection)
        with mock.patch.object(database, "core_db_get_connection",
                               return_value=ConnectionAdapter(proxy, BACKEND_SQLITE)):
            database.init_db()
        joined = "\n".join(proxy.sql)
        self.assertIn("PRAGMA busy_timeout=30000", joined)
        self.assertIn("PRAGMA journal_mode=DELETE", joined)
        self.assertIn("PRAGMA synchronous=FULL", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS logs", joined)
        real.close()


# ─────────────────────────────────────────────────────────────────────
# B) PostgreSQL with dummy binding — zero DDL, zero PRAGMA
# ─────────────────────────────────────────────────────────────────────
class PostgresBootstrapNoDdlTests(unittest.TestCase):
    """No DDL/PRAGMA is executed on PG; Confirmation only (with dummy connection)."""

    def setUp(self):
        # current schema (like the database on which Alembic upgrade head is executed)
        """Prepare the fixtures that each test in this class uses."""
        self.rows = [(t,) for t in APP_TABLES]
        self.stub = _SqlRecordingConnection(self.rows)
        self.adapter = ConnectionAdapter(self.stub, backend=BACKEND_POSTGRESQL)

    def test_init_db_is_a_noop_verification(self):
        """Verifies init db is a noop verification."""
        import core.database as database
        with mock.patch.object(database, "core_db_get_connection", return_value=self.adapter):
            database.init_db()
        self.assertTrue(self.stub.statements(), "باید حداقل یک پرس‌وجوی تأیید اجرا شود")
        self.assertEqual(self.stub.ddl_or_pragma(), [],
                         "روی PG هیچ DDL/PRAGMA/نوشتنی نباید اجرا شود")
        self.assertTrue(self.stub.closed, "اتصال باید بسته شود (بدون نشتی)")

    def test_init_db_missing_schema_raises_with_actionable_message(self):
        """Verifies init db missing schema raises with actionable message."""
        import core.database as database
        empty = _SqlRecordingConnection(())          # Database without migration
        adapter = ConnectionAdapter(empty, backend=BACKEND_POSTGRESQL)
        with mock.patch.object(database, "core_db_get_connection", return_value=adapter):
            with self.assertRaises(RuntimeError) as ctx:
                database.init_db()
        message = str(ctx.exception)
        self.assertIn("alembic upgrade head", message)
        self.assertIn("logs", message)
        self.assertTrue(empty.closed, "حتی در خطا هم اتصال باید بسته شود")

    def test_yield_engine_and_security_audit_are_noops_on_pg(self):
        """Verifies yield engine and security audit are noops on pg."""
        import core.security_audit as audit
        import core.yield_engine as yield_engine
        with mock.patch.object(yield_engine, "db_connection",
                               lambda **kw: _fake_db_connection(self.adapter)):
            yield_engine.ensure_yield_tables()
        self.assertEqual(self.stub.ddl_or_pragma(), [])

        sec_stub = _SqlRecordingConnection([(t,) for t in SECURITY_TABLES])
        sec_adapter = ConnectionAdapter(sec_stub, backend=BACKEND_POSTGRESQL)
        with mock.patch.object(audit, "is_postgres", return_value=True), \
                mock.patch.object(audit, "_db_conn",
                                  lambda **kw: _fake_db_connection(sec_adapter)):
            audit.init_security_audit()
        self.assertEqual(sec_stub.ddl_or_pragma(), [])

    def test_verify_helper_reports_only_missing_tables(self):
        """Verifies verify helper reports only missing tables."""
        stub = _SqlRecordingConnection([("logs",), ("users",)])
        adapter = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertEqual(missing_tables(adapter, ("logs", "users")), [])
        with self.assertRaises(RuntimeError) as ctx:
            verify_postgres_tables(adapter, ("logs", "users", "meta"), context="probe")
        self.assertIn("meta", str(ctx.exception))
        self.assertNotIn("users", str(ctx.exception))
        self.assertIn("probe", str(ctx.exception))

    def test_schema_columns_uses_information_schema_on_pg(self):
        """On PG the list of columns comes from ``information_schema'', not PRAGMA."""
        stub = _SqlRecordingConnection([("id",), ("timestamp",), ("message",)])
        adapter = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertEqual(schema_columns(adapter, "logs"), ["id", "timestamp", "message"])
        sql, params = stub.log[-1]
        self.assertIn("information_schema.columns", sql)
        self.assertIn("ordinal_position", sql)
        self.assertIn("current_schema()", sql)
        self.assertNotIn("PRAGMA", sql)
        self.assertEqual(params, ("logs",))

    def test_present_tables_uses_information_schema_on_pg(self):
        """Verifies present tables uses information schema on pg."""
        adapter = ConnectionAdapter(_SqlRecordingConnection([("logs",)]),
                                    backend=BACKEND_POSTGRESQL)
        self.assertEqual(present_tables(adapter), {"logs"})
        sql, _params = adapter.raw_connection.log[-1]
        self.assertIn("information_schema.tables", sql)
        self.assertIn("current_schema()", sql)


# ─────────────────────────────────────────────────────────────────────
# C) Real PostgreSQL
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_TEST_DSN, _PG_SKIP_REASON or "")
class PostgresBootstrapLiveTests(unittest.TestCase):
    """Three bootstrap functions on real PG: no syntax error, idempotent, Alembic schema."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_TEST_DSN:      # second guard
            self.skipTest(_PG_SKIP_REASON or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_TEST_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_bootstrap_functions_run_twice_without_error(self):
        """Verifies bootstrap functions run twice without error."""
        import core.database as database
        import core.security_audit as audit
        import core.yield_engine as yield_engine
        for _ in range(2):
            database.init_db()
            audit.init_security_audit()
            yield_engine.ensure_yield_tables()

    def test_schema_columns_matches_the_alembic_schema_on_pg(self):
        """On a real PG, the ``logs'' columns are declared in order and PRAGMA breaks."""
        with db_connection() as conn:
            cols = schema_columns(conn, "logs")
            self.assertIn("id", cols)
            self.assertIn("timestamp", cols)
            self.assertEqual(cols[0], "id", "ستون اول باید id باشد (ترتیب اعلام)")
            self.assertEqual(schema_columns(conn, "table_that_does_not_exist"), [])
            with self.assertRaises(DatabaseError):
                conn.execute("PRAGMA table_info(logs)")

    def test_alembic_schema_is_complete_on_pg(self):
        """Verifies alembic schema is complete on pg."""
        with db_connection() as conn:
            self.assertEqual(conn.backend, BACKEND_POSTGRESQL)
            tables = present_tables(conn)
        self.assertEqual([t for t in APP_TABLES if t not in tables], [],
                         "اسکیمای Alembic روی PG ناقص است")
        self.assertIn(ALEMBIC_TABLE, tables)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
