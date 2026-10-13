# -*- coding: utf-8 -*-
"""
تست‌های E.4.4 — ترجمه‌ی خطای درایور به خانواده‌ی مشترک ``core.db.exceptions``.

قاعده‌ای که این فایل قفل می‌کند:
  • ``ConnectionAdapter`` خطای درایور را پیش از بالا رفتن ترجمه می‌کند؛ کد برنامه
    (``core/`` و ``web/``) هیچ ``except sqlite3.*`` ندارد و نباید داشته باشد؛
  • نگاشت تصمیم‌شده (E.4.4):
      SQLite                                  PostgreSQL (SQLSTATE)
      ------------------------------------------------------------------
      UNIQUE/NOT NULL/CHECK/FK constraint  →  UniqueViolation   (23xxx)
      duplicate column name               →  ColumnAlreadyExists (42701)
      no such table                        →  UndefinedTable     (42P01)
      no such column                       →  UndefinedColumn    (42703)
      سایر OperationalError                →  DatabaseOperationalError (57xxx…)
      سایر DatabaseError                   →  DatabaseError
  • ``original`` و پیام درایور دست‌نخورده می‌مانند (چند مصرف‌کننده متن را
    برای راهنمایی کاربر می‌خوانند)؛
  • «قفل/دیسک» عمداً همچنان با متن یا SQLSTATE تشخیص داده می‌شود (یادداشت بدهی
    فنی برای فاز بعد).

بدون ``PG_DSN``، کلاس‌های PG با پیام روشن skip می‌شوند.
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
# A) مسیر SQLite (DB واقعی در پوشه‌ی موقت)
# ─────────────────────────────────────────────────────────────────────
class SqliteTranslationTests(unittest.TestCase):
    """نگاشت خطاهای واقعی SQLite (نه ساختگی)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "logs.db")
        self.conn = connect_sqlite(self.path)
        self.conn.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, n INTEGER)")
        self.conn.commit()
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.conn.close()
        finally:
            self.tmp.cleanup()

    def _expect(self, cls, sql, params=()):
        with self.assertRaises(cls) as ctx:
            self.conn.execute(sql, params)
        return ctx.exception

    def test_undefined_table(self):
        exc = self._expect(UndefinedTable, "SELECT * FROM nope")
        self.assertEqual(exc.backend, BACKEND_SQLITE)
        self.assertIn("no such table", str(exc))
        self.assertIsInstance(exc.original, sqlite3.OperationalError)
        self.assertIsNone(exc.sqlstate)

    def test_undefined_column(self):
        exc = self._expect(UndefinedColumn, "SELECT nope FROM t")
        self.assertIn("no such column", str(exc))

    def test_column_already_exists(self):
        self.conn.execute("ALTER TABLE t ADD COLUMN extra TEXT")
        exc = self._expect(ColumnAlreadyExists, "ALTER TABLE t ADD COLUMN extra TEXT")
        self.assertIn("duplicate column name", str(exc))

    def test_unique_violation_from_unique_constraint(self):
        self.conn.execute("INSERT INTO t (id, name) VALUES (1, 'a')")
        exc = self._expect(UniqueViolation, "INSERT INTO t (id, name) VALUES (1, 'a')")
        self.assertIsInstance(exc.original, sqlite3.IntegrityError)

    def test_unique_violation_from_not_null(self):
        """
        تصمیم E.4.4 (مالک): کل خانوادهٔ ``IntegrityError`` ⇒ ``UniqueViolation``.

        یعنی ردیف NOT NULL هم مثل UNIQUE «skip» می‌شود و رفتار امروزِ
        ``import_db`` روی هر دو backend یکی می‌ماند (ثبت بدهی فنی در E.4.11).
        """
        self._expect(UniqueViolation, "INSERT INTO t (id, name) VALUES (2, NULL)")

    def test_other_operational_errors_stay_operational(self):
        exc = self._expect(DatabaseOperationalError, "SELECT (((")
        self.assertIn("incomplete input", str(exc))

    def test_errors_are_not_translated_twice(self):
        original = UniqueViolation("خودم", backend="sqlite")
        self.assertIs(_translate_error(original, BACKEND_SQLITE), original)

    def test_non_database_errors_pass_through_untouched(self):
        boom = ValueError("not a db error")
        self.assertIs(_translate_error(boom, BACKEND_SQLITE), boom)
        self.assertIs(_translate_error(boom, BACKEND_POSTGRESQL), boom)

    def test_shared_errors_are_database_error_subclasses(self):
        for cls in (ColumnAlreadyExists, UndefinedTable, UndefinedColumn,
                    UniqueViolation, DatabaseOperationalError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, DatabaseError))


# ─────────────────────────────────────────────────────────────────────
# B) مسیر PostgreSQL (استاب + PG واقعی)
# ─────────────────────────────────────────────────────────────────────
class _StubPgError(Exception):
    """خطای ساختگی سبک psycopg: فقط ``sqlstate`` و پیام دارد."""

    def __init__(self, message, sqlstate=None):
        super().__init__(message)
        self.sqlstate = sqlstate


class PostgresTranslationStubTests(unittest.TestCase):
    """نگاشت بر پایه‌ی ``sqlstate`` — بدون سرور (لایهٔ تشخیص SQLSTATE)."""

    def _translate(self, sqlstate, message="خطای ساختگی"):
        return _translate_error(_StubPgError(message, sqlstate), BACKEND_POSTGRESQL)

    def test_sqlstate_mapping(self):
        for sqlstate, expected in (
            ("42P01", UndefinedTable),
            ("42703", UndefinedColumn),
            ("42701", ColumnAlreadyExists),
            ("23502", UniqueViolation),      # NOT NULL — خانوادهٔ integrity
            ("23505", UniqueViolation),      # UNIQUE
            ("23503", UniqueViolation),      # FOREIGN KEY
        ):
            with self.subTest(sqlstate=sqlstate):
                exc = self._translate(sqlstate)
                self.assertIsInstance(exc, expected)
                self.assertEqual(exc.sqlstate, sqlstate)
                self.assertEqual(exc.backend, BACKEND_POSTGRESQL)

    def test_unknown_sqlstate_passes_through(self):
        boom = _StubPgError("division by zero", "22012")
        self.assertIs(_translate_error(boom, BACKEND_POSTGRESQL), boom)


def _pg_target():
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
    """همان نگاشت، این بار روی PG ۱۶ واقعی (با rollback بین سنجش‌ها)."""

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self._cleanup)
        self.conn.execute(
            "CREATE TEMP TABLE exc_t (id int PRIMARY KEY, name text UNIQUE NOT NULL)")
        self.conn.commit()

    def _cleanup(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def _expect(self, cls, sql, params=()):
        try:
            with self.assertRaises(cls) as ctx:
                self.conn.execute(sql, params)
            return ctx.exception
        finally:
            self.conn.rollback()      # PG: پس از خطا تراکنش abort است

    def test_live_mapping(self):
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
        self.conn.execute("INSERT INTO exc_t (id, name) VALUES (1, 'a')")
        self.conn.commit()
        exc = self._expect(UniqueViolation, "INSERT INTO exc_t (id, name) VALUES (1, 'a')")
        self.assertEqual(exc.sqlstate, "23505")

    def test_statement_timeout_is_operational(self):
        self.conn.execute("SET statement_timeout = '30ms'")
        self.conn.commit()
        try:
            exc = self._expect(DatabaseOperationalError, "SELECT pg_sleep(0.2)")
            self.assertEqual(exc.sqlstate, "57014")
        finally:
            self.conn.execute("SET statement_timeout = 0")
            self.conn.commit()


# ─────────────────────────────────────────────────────────────────────
# C) قراردادِ کد برنامه: صفر except sqlite3.* و مهار درست خطاها
# ─────────────────────────────────────────────────────────────────────
class AppCodeContractTests(unittest.TestCase):
    """قیدهای E.4.4 روی خودِ کد (نه رفتار اجرا)."""

    def _sqlite_except_sites(self, *roots):
        """هر ``except`` که نوعش به ``sqlite3`` اشاره می‌کند (اسکن AST، نه grep)."""
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
        """الزام مالک: در ``core/`` و ``web/`` هیچ ``except sqlite3.*`` نماند."""
        self.assertEqual(self._sqlite_except_sites("core", "web"), [])

    def test_sqlite_only_tools_are_explicitly_exempt(self):
        """
        ابزارهای SQLite-only گروه B **دست‌نخورده** می‌مانند (تصمیم مالک): با
        ``sqlite3.connect`` خام کار می‌کنند، پس نوع خطایشان واقعاً sqlite3 است.
        این تست فقط مستند می‌کند که استثنا آگاهانه است، نه فراموش‌شده.
        """
        sites = self._sqlite_except_sites("tools")
        self.assertTrue(sites, "ابزارهای SQLite-only باید همچنان استثنا داشته باشند")
        allowed_files = {
            "tools/analyze_old_events.py", "tools/find_insert_or_replace.py",
            "tools/migrate_timestamps_to_utc.py", "tools/set_cutover_marker.py",
            "tools/verify_server_timezone.py",
            # E.7.2: چک‌لیست آمادگی cutover — سمت SQLite آن عمداً sqlite3 خام است
            # (`PRAGMA integrity_check` معادل PG ندارد و پیش از cutover باید فایل
            #  logs.db را مستقیم ببیند)؛ سمت PG در همین ابزار از psycopg می‌گذرد.
            "tools/check_cutover_readiness.py",
        }
        offenders = [s for s in sites if s.replace("\\", "/").split(":")[0] not in allowed_files]
        self.assertEqual(offenders, [])

    def test_duplicate_column_is_tolerated_but_other_errors_are_not(self):
        """
        فقط «ستون تکراری» قورت داده می‌شود: اگر داخل همان حلقه‌ی ALTER خطای
        دیگری بیاید (مثلاً ``UndefinedTable``)، باید **بالا برود**.
        """
        import core.database as database

        class _FakeConn:
            backend = BACKEND_SQLITE

            def __init__(self, *, fail_with):
                self.fail_with = fail_with

            def execute(self, sql, params=()):
                if "ALTER TABLE" in sql:
                    raise self.fail_with
                return mock.Mock()

            def commit(self):
                pass

            def cursor(self):
                return self

            def fetchone(self):
                return None

            def close(self):
                pass

        tolerant = _FakeConn(fail_with=ColumnAlreadyExists("duplicate column name"))
        with mock.patch.object(database, "core_db_get_connection", return_value=tolerant):
            database.init_db()          # نباید خطا بدهد

        strict = _FakeConn(fail_with=UndefinedTable("no such table: logs"))
        with mock.patch.object(database, "core_db_get_connection", return_value=strict):
            with self.assertRaises(UndefinedTable):
                database.init_db()

    def test_create_admin_uses_sqlstate_for_disk_full_hint(self):
        """
        راهنمای «دیسک پر» در ``create_admin`` دیگر فقط متنی نیست: SQLSTATE
        (روی PG: 53100) هم پذیرفته می‌شود. اینجا فقط منطق تشخیص را قفل می‌کنیم.
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
