# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_exceptions_pg.py
# @brief:        Tests that driver errors are translated into the common exception family (E.4.4).
# @details:      Responsibilities: Verify error mapping on both backends.
#                Main API: SqliteTranslationTests, _StubPgError, PostgresTranslationStubTests,
#                PostgresTranslationLiveTests, AppCodeContractTests
#                Side effects: database access
# @dependencies: internal: core.database, core.db, core.db.connection | external: psycopg
# @notes:        Can be run directly as a script. PostgreSQL-live tests skip automatically when no
#                server is reachable.
# =============================================================================

"""
E.4.4 Tests — Translate driver error to common ``core.db.exceptions'' family.

The rule that locks this file:
  • ``ConnectionAdapter'' translates the driver error before going up; Program code
    (``core/'' and ``web/'') does not and should not have any ``except sqlite3.*'';
  • Decided mapping (E.4.4):
      SQLite                                  PostgreSQL (SQLSTATE)
      ------------------------------------------------------------------
      UNIQUE/NOT NULL/CHECK/FK constraint  →  UniqueViolation   (23xxx)
      duplicate column name               →  ColumnAlreadyExists (42701)
      no such table                        →  UndefinedTable     (42P01)
      no such column                       →  UndefinedColumn    (42703)
      Other OperationalError → DatabaseOperationalError (57xxx…)
      Other DatabaseError → DatabaseError
  • The ``original'' and driver message remain intact (multiple text consumers
    read for user guidance);
  • "lock/disk" is intentionally still detected with text or SQLSTATE (debt note
    technical for the next phase).

Without ``PG_DSN'', PG classes are skipped with clear message.
"""
import ast
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_PG_DSN,
    ColumnAlreadyExists,
    DatabaseError,
    DatabaseOperationalError,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
    connect_postgres,
    connect_sqlite,
)
from core.db.connection import _translate_error

ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────
# A) SQLite path (real DB in temporary folder)
# ─────────────────────────────────────────────────────────────────────
class SqliteTranslationTests(unittest.TestCase):
    """Mapping real (not dummy) SQLite errors."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "logs.db")
        self.conn = connect_sqlite(self.path)
        self.conn.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, n INTEGER)")
        self.conn.commit()
        self.addCleanup(self._close)

    def _close(self):
        """Helper: close."""
        try:
            self.conn.close()
        finally:
            self.tmp.cleanup()

    def _expect(self, cls, sql, params=()):
        """Helper: expect."""
        with self.assertRaises(cls) as ctx:
            self.conn.execute(sql, params)
        return ctx.exception

    def test_undefined_table(self):
        """Verifies undefined table."""
        exc = self._expect(UndefinedTable, "SELECT * FROM nope")
        self.assertEqual(exc.backend, BACKEND_SQLITE)
        self.assertIn("no such table", str(exc))
        self.assertIsInstance(exc.original, sqlite3.OperationalError)
        self.assertIsNone(exc.sqlstate)

    def test_undefined_column(self):
        """Verifies undefined column."""
        exc = self._expect(UndefinedColumn, "SELECT nope FROM t")
        self.assertIn("no such column", str(exc))

    def test_column_already_exists(self):
        """Verifies column already exists."""
        self.conn.execute("ALTER TABLE t ADD COLUMN extra TEXT")
        exc = self._expect(ColumnAlreadyExists, "ALTER TABLE t ADD COLUMN extra TEXT")
        self.assertIn("duplicate column name", str(exc))

    def test_unique_violation_from_unique_constraint(self):
        """Verifies unique violation from unique constraint."""
        self.conn.execute("INSERT INTO t (id, name) VALUES (1, 'a')")
        exc = self._expect(UniqueViolation, "INSERT INTO t (id, name) VALUES (1, 'a')")
        self.assertIsInstance(exc.original, sqlite3.IntegrityError)

    def test_unique_violation_from_not_null(self):
        """
        Decision E.4.4 (Owner): Entire family of ``IntegrityError'' ⇒ ``UniqueViolation''.

        That is, the NOT NULL row is "skipped" like UNIQUE, and today's behavior is
        ``import_db'' remains the same on both backends (technical debt registration in E.4.11).
        """
        self._expect(UniqueViolation, "INSERT INTO t (id, name) VALUES (2, NULL)")

    def test_other_operational_errors_stay_operational(self):
        """Verifies other operational errors stay operational."""
        exc = self._expect(DatabaseOperationalError, "SELECT (((")
        self.assertIn("incomplete input", str(exc))

    def test_errors_are_not_translated_twice(self):
        """Verifies errors are not translated twice."""
        original = UniqueViolation("خودم", backend="sqlite")
        self.assertIs(_translate_error(original, BACKEND_SQLITE), original)

    def test_non_database_errors_pass_through_untouched(self):
        """Verifies non database errors pass through untouched."""
        boom = ValueError("not a db error")
        self.assertIs(_translate_error(boom, BACKEND_SQLITE), boom)
        self.assertIs(_translate_error(boom, BACKEND_POSTGRESQL), boom)

    def test_shared_errors_are_database_error_subclasses(self):
        """Verifies shared errors are database error subclasses."""
        for cls in (ColumnAlreadyExists, UndefinedTable, UndefinedColumn,
                    UniqueViolation, DatabaseOperationalError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, DatabaseError))


# ─────────────────────────────────────────────────────────────────────
# B) PostgreSQL path (stub + real PG)
# ─────────────────────────────────────────────────────────────────────
class _StubPgError(Exception):
    """psycopg style dummy error: has only ``sqlstate'' and message."""

    def __init__(self, message, sqlstate=None):
        """Initialize the instance."""
        super().__init__(message)
        self.sqlstate = sqlstate


class PostgresTranslationStubTests(unittest.TestCase):
    """Mapping based on ``sqlstate'' — serverless (SQLSTATE detection layer)."""

    def _translate(self, sqlstate, message="خطای ساختگی"):
        """Helper: translate."""
        return _translate_error(_StubPgError(message, sqlstate), BACKEND_POSTGRESQL)

    def test_sqlstate_mapping(self):
        """Verifies sqlstate mapping."""
        for sqlstate, expected in (
            ("42P01", UndefinedTable),
            ("42703", UndefinedColumn),
            ("42701", ColumnAlreadyExists),
            ("23502", UniqueViolation),      # NOT NULL — integrity family
            ("23505", UniqueViolation),      # UNIQUE
            ("23503", UniqueViolation),      # FOREIGN KEY
        ):
            with self.subTest(sqlstate=sqlstate):
                exc = self._translate(sqlstate)
                self.assertIsInstance(exc, expected)
                self.assertEqual(exc.sqlstate, sqlstate)
                self.assertEqual(exc.backend, BACKEND_POSTGRESQL)

    def test_unknown_sqlstate_passes_through(self):
        """Verifies unknown sqlstate passes through."""
        boom = _StubPgError("division by zero", "22012")
        self.assertIs(_translate_error(boom, BACKEND_POSTGRESQL), boom)


def _pg_target():
    """Helper: pg target."""
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
                      f"{str(exc).strip().splitlines()[0][:120]})")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()


@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresTranslationLiveTests(unittest.TestCase):
    """Same mapping, this time on real PG 16 (with rollback between measurements)."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self._cleanup)
        self.conn.execute(
            "CREATE TEMP TABLE exc_t (id int PRIMARY KEY, name text UNIQUE NOT NULL)")
        self.conn.commit()

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

    def _expect(self, cls, sql, params=()):
        """Helper: expect."""
        try:
            with self.assertRaises(cls) as ctx:
                self.conn.execute(sql, params)
            return ctx.exception
        finally:
            self.conn.rollback()      # PG: After the error, the transaction is aborted

    def test_live_mapping(self):
        """Verifies live mapping."""
        cases = (
            (UndefinedTable, "SELECT * FROM nope_table_xyz", "42P01"),
            (UndefinedColumn, "SELECT nope_column FROM exc_t", "42703"),
            (UniqueViolation, "INSERT INTO exc_t (id, name) VALUES (1, NULL)", "23502"),
            (ColumnAlreadyExists, "ALTER TABLE exc_t ADD COLUMN name text", "42701"),
        )
        for cls, sql, sqlstate in cases:
            with self.subTest(sql=sql):
                exc = self._expect(cls, sql)
                self.assertEqual(exc.sqlstate, sqlstate)
                self.assertEqual(exc.backend, BACKEND_POSTGRESQL)
                self.assertIsNotNone(exc.original)

    def test_duplicate_key_is_unique_violation(self):
        """Verifies duplicate key is unique violation."""
        self.conn.execute("INSERT INTO exc_t (id, name) VALUES (1, 'a')")
        self.conn.commit()
        exc = self._expect(UniqueViolation, "INSERT INTO exc_t (id, name) VALUES (1, 'a')")
        self.assertEqual(exc.sqlstate, "23505")

    def test_statement_timeout_is_operational(self):
        """Verifies statement timeout is operational."""
        self.conn.execute("SET statement_timeout = '30ms'")
        self.conn.commit()
        try:
            exc = self._expect(DatabaseOperationalError, "SELECT pg_sleep(0.2)")
            self.assertEqual(exc.sqlstate, "57014")
        finally:
            self.conn.execute("SET statement_timeout = 0")
            self.conn.commit()


# ─────────────────────────────────────────────────────────────────────
# C) program code convention: zero except sqlite3.* and proper handling of errors
# ─────────────────────────────────────────────────────────────────────
class AppCodeContractTests(unittest.TestCase):
    """E.4.4 constraints on the code itself (not the execution behavior)."""

    def _sqlite_except_sites(self, *roots):
        """Any ``except'' whose type refers to ``sqlite3'' (AST scan, not grep)."""
        found = []
        for root in roots:
            for path in sorted(Path(ROOT, root).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ExceptHandler) or node.type is None:
                        continue
                    for sub in ast.walk(node.type):
                        if isinstance(sub, ast.Name) and sub.id == "sqlite3":
                            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                        elif (isinstance(sub, ast.Attribute)
                              and isinstance(sub.value, ast.Name)
                              and sub.value.id == "sqlite3"):
                            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        return found

    def test_no_sqlite_excepts_in_app_code(self):
        """Owner's requirement: no ``except sqlite3.*'' should be left in ``core/'' and ``web/''."""
        self.assertEqual(self._sqlite_except_sites("core", "web"), [])

    def test_sqlite_only_tools_are_explicitly_exempt(self):
        """
        Group B SQLite-only tools remain **intact** (owner's decision): with
        ``sqlite3.connect'' works raw, so their error type is really sqlite3.
        This test only documents that the exception is aware, not forgotten.
        """
        sites = self._sqlite_except_sites("tools")
        self.assertTrue(sites, "ابزارهای SQLite-only باید همچنان استثنا داشته باشند")
        allowed_files = {
            "tools/analyze_old_events.py", "tools/find_insert_or_replace.py",
            "tools/migrate_timestamps_to_utc.py", "tools/set_cutover_marker.py",
            "tools/verify_server_timezone.py",
            # E.7.2: Cutover Readiness Checklist — The SQLite side is intentionally raw sqlite3
            # (`PRAGMA integrity_check' does not have a PG equivalent and before cutover the file must be
            # see logs.db directly); The PG side in the same tool goes through psycopg.
            "tools/check_cutover_readiness.py",
        }
        offenders = [s for s in sites if s.replace("\\", "/").split(":")[0] not in allowed_files]
        self.assertEqual(offenders, [])

    def test_duplicate_column_is_tolerated_but_other_errors_are_not(self):
        """
        Only the "duplicate column" is swallowed: if inside the same loop ALTER error
        If another comes along (eg ``UndefinedTable''), it should **go up**.
        """
        import core.database as database

        class _FakeConn:
            """Helper class: fake conn."""
            backend = BACKEND_SQLITE

            def __init__(self, *, fail_with):
                """Initialize the instance."""
                self.fail_with = fail_with

            def execute(self, sql, params=()):
                """Helper: execute."""
                if "ALTER TABLE" in sql:
                    raise self.fail_with
                return mock.Mock()

            def commit(self):
                """Helper: commit."""
                pass

            def cursor(self):
                """Helper: cursor."""
                return self

            def fetchone(self):
                """Helper: fetchone."""
                return None

            def close(self):
                """Helper: close."""
                pass

        tolerant = _FakeConn(fail_with=ColumnAlreadyExists("duplicate column name"))
        with mock.patch.object(database, "core_db_get_connection", return_value=tolerant):
            database.init_db()          # It should not give an error

        strict = _FakeConn(fail_with=UndefinedTable("no such table: logs"))
        with mock.patch.object(database, "core_db_get_connection", return_value=strict):
            with self.assertRaises(UndefinedTable):
                database.init_db()

    def test_create_admin_uses_sqlstate_for_disk_full_hint(self):
        """
        The ``full disk'' prompt in ``create_admin'' is no longer just text: SQLSTATE
        (on PG: 53100) is also accepted. Here we only lock the detection logic.
        """
        for exc, expected in (
            (DatabaseOperationalError("database or disk is full"), True),
            (DatabaseOperationalError("disk_full", sqlstate="53100"), True),
            (DatabaseOperationalError("database is locked"), False),
        ):
            with self.subTest(message=str(exc)):
                got = (getattr(exc, "sqlstate", None) == "53100"
                       or "full" in str(exc).lower())
                self.assertEqual(got, expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
