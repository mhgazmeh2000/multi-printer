# -*- coding: utf-8 -*-
"""
تست‌های E.4.5 — DDL آگاه‌به-backend.

قاعده‌ای که این فایل قفل می‌کند:
  • روی **SQLite** توابع bootstrap همان‌طور که از قبل بودند جدول/ایندکس می‌سازند و
    PRAGMAهای زمان اجرا را ست می‌کنند (`busy_timeout`/`journal_mode`/`synchronous`)؛
  • روی **PostgreSQL** همین توابع **no-op**اند: هیچ DDL و هیچ PRAGMAی اجرا نمی‌شود،
    چون `Alembic` تنها منبع DDL است. فقط تأیید می‌کنند اسکیما موجود است و در نبود
    آن خطای روشن (RuntimeError با راه‌حل) می‌دهند — نه شکست بی‌صدا.

پوشش:
  A) SQLite (DB واقعی در پوشه‌ی موقت): ساخت جدول‌ها، idempotency، و اینکه PRAGMAها
     هنوز اجرا می‌شوند (با پروکسی اتصال، بدون سرور).
  B) PostgreSQL با اتصال ساختگی: صفر PRAGMA/DDL، خطای روشن در نبود جدول، بستن اتصال.
  C) PostgreSQL واقعی (با ``PG_DSN``): سه تابع bootstrap بدون خطا و اسکیمای Alembic
     کامل. بدون ``PG_DSN`` با پیام روشن skip می‌شود.
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
# استاب‌ها (بدون سرور)
# ─────────────────────────────────────────────────────────────────────
class _StubCursor:
    """cursor ساختگی: ردیف‌های از پیش تعیین‌شده را برمی‌گرداند."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = -1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _SqlRecordingConnection:
    """اتصال ساختگی سبک psycopg که هر SQL را ثبت می‌کند."""

    def __init__(self, rows=()):
        self.log = []
        self.rows = list(rows)
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        return _StubCursor(self.rows)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True

    # ── کمک‌کارهای تست ──
    def statements(self):
        return [sql for sql, _params in self.log]

    def ddl_or_pragma(self):
        """هر گزاره‌ای که DDL/PRAGMA است (روی PG باید همیشه خالی باشد)."""
        out = []
        for sql in self.statements():
            head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
            if head in ("PRAGMA", "CREATE", "ALTER", "DROP", "INSERT", "UPDATE", "DELETE", "GRANT"):
                out.append(sql.strip())
        return out


class _RecordingCursorProxy:
    def __init__(self, cur, log):
        self._cur = cur
        self._log = log

    def execute(self, sql, params=()):
        self._log.append(sql)
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _RecordingRawConnection:
    """پروکسی اتصال خام sqlite3 که SQLهای اجراشده را ثبت می‌کند."""

    def __init__(self, raw):
        self._raw = raw
        self.sql = []

    def cursor(self):
        return _RecordingCursorProxy(self._raw.cursor(), self.sql)

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return _RecordingCursorProxy(self._raw.execute(sql, params), self.sql)

    def __getattr__(self, name):
        return getattr(self._raw, name)


@contextmanager
def _fake_db_connection(conn):
    """جانشین context manager برای ماژول‌هایی که خودشان اتصال می‌سازند."""
    yield conn


def _pg_test_target():
    """``(dsn, None)`` اگر PG در دسترس باشد، وگرنه ``(None, دلیلِ skip)``."""
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
# A) SQLite — رفتار قبلی دست‌نخورده
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class SqliteBootstrapTests(unittest.TestCase):
    """توابع bootstrap روی SQLite همان کار همیشگی را می‌کنند (بدون رگرسیون)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _tables(self):
        with db_connection() as conn:
            self.assertEqual(conn.backend, BACKEND_SQLITE)
            return present_tables(conn)

    def test_init_db_creates_its_tables_and_is_idempotent(self):
        import core.database as database
        database.init_db()
        first = self._tables()
        for table in ("logs", "printer_counters", "toner_history", "sensor_readings", "users"):
            self.assertIn(table, first)
        database.init_db()          # اجرای دوم: نباید خطا بدهد و schema عوض نشود
        self.assertEqual(self._tables(), first)

    def test_yield_and_security_initializers_create_their_tables(self):
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
        """``schema_columns`` روی SQLite همان خروجی ``PRAGMA table_info`` است."""
        with db_connection() as conn:
            self.assertEqual(conn.backend, BACKEND_SQLITE)
            conn.execute("CREATE TABLE probe (b TEXT, a INTEGER, c TEXT)")
            self.assertEqual(schema_columns(conn, "probe"), ["b", "a", "c"])
            self.assertEqual(schema_columns(conn, "no_such_table"), [])

    def test_schema_columns_quotes_identifier(self):
        """نام جدول با فاصله/کوتیشن نقل‌قول‌شده می‌رود (نحو ممنوع است، نه تزریق)."""
        with db_connection() as conn:
            conn.execute('CREATE TABLE "weird name" (x TEXT)')
            self.assertEqual(schema_columns(conn, "weird name"), ["x"])
            self.assertEqual(schema_columns(conn, 'weird" name'), [])

    def test_sqlite_branch_still_runs_the_runtime_pragmas(self):
        """
        PRAGMAها (ردیف ۱۲) باید روی مسیر SQLite باقی بمانند؛ فقط مسیر PG از آن‌ها
        پاک می‌شود. اینجا با پروکسی اتصال، SQLهای اجراشده ثبت می‌شوند.
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
# B) PostgreSQL با اتصال ساختگی — صفر DDL، صفر PRAGMA
# ─────────────────────────────────────────────────────────────────────
class PostgresBootstrapNoDdlTests(unittest.TestCase):
    """روی PG هیچ DDL/PRAGMAی اجرا نمی‌شود؛ فقط تأیید (با اتصال ساختگی)."""

    def setUp(self):
        # schema حاضر (مثل دیتابیسی که alembic upgrade head روی آن اجرا شده)
        self.rows = [(t,) for t in APP_TABLES]
        self.stub = _SqlRecordingConnection(self.rows)
        self.adapter = ConnectionAdapter(self.stub, backend=BACKEND_POSTGRESQL)

    def test_init_db_is_a_noop_verification(self):
        import core.database as database
        with mock.patch.object(database, "core_db_get_connection", return_value=self.adapter):
            database.init_db()
        self.assertTrue(self.stub.statements(), "باید حداقل یک پرس‌وجوی تأیید اجرا شود")
        self.assertEqual(self.stub.ddl_or_pragma(), [],
                         "روی PG هیچ DDL/PRAGMA/نوشتنی نباید اجرا شود")
        self.assertTrue(self.stub.closed, "اتصال باید بسته شود (بدون نشتی)")

    def test_init_db_missing_schema_raises_with_actionable_message(self):
        import core.database as database
        empty = _SqlRecordingConnection(())          # دیتابیس بدون migration
        adapter = ConnectionAdapter(empty, backend=BACKEND_POSTGRESQL)
        with mock.patch.object(database, "core_db_get_connection", return_value=adapter):
            with self.assertRaises(RuntimeError) as ctx:
                database.init_db()
        message = str(ctx.exception)
        self.assertIn("alembic upgrade head", message)
        self.assertIn("logs", message)
        self.assertTrue(empty.closed, "حتی در خطا هم اتصال باید بسته شود")

    def test_yield_engine_and_security_audit_are_noops_on_pg(self):
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
        stub = _SqlRecordingConnection([("logs",), ("users",)])
        adapter = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertEqual(missing_tables(adapter, ("logs", "users")), [])
        with self.assertRaises(RuntimeError) as ctx:
            verify_postgres_tables(adapter, ("logs", "users", "meta"), context="probe")
        self.assertIn("meta", str(ctx.exception))
        self.assertNotIn("users", str(ctx.exception))
        self.assertIn("probe", str(ctx.exception))

    def test_schema_columns_uses_information_schema_on_pg(self):
        """روی PG فهرست ستون‌ها از ``information_schema`` می‌آید، نه PRAGMA."""
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
        adapter = ConnectionAdapter(_SqlRecordingConnection([("logs",)]),
                                    backend=BACKEND_POSTGRESQL)
        self.assertEqual(present_tables(adapter), {"logs"})
        sql, _params = adapter.raw_connection.log[-1]
        self.assertIn("information_schema.tables", sql)
        self.assertIn("current_schema()", sql)


# ─────────────────────────────────────────────────────────────────────
# C) PostgreSQL واقعی
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_TEST_DSN, _PG_SKIP_REASON or "")
class PostgresBootstrapLiveTests(unittest.TestCase):
    """سه تابع bootstrap روی PG واقعی: بدون خطای نحوی، idempotent، اسکیمای Alembic."""

    def setUp(self):
        if not _PG_TEST_DSN:      # نگهبان دوم
            self.skipTest(_PG_SKIP_REASON or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_TEST_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_bootstrap_functions_run_twice_without_error(self):
        import core.database as database
        import core.security_audit as audit
        import core.yield_engine as yield_engine
        for _ in range(2):
            database.init_db()
            audit.init_security_audit()
            yield_engine.ensure_yield_tables()

    def test_schema_columns_matches_the_alembic_schema_on_pg(self):
        """روی PG واقعی، ستون‌های ``logs`` با ترتیب اعلام می‌آیند و PRAGMA می‌شکند."""
        with db_connection() as conn:
            cols = schema_columns(conn, "logs")
            self.assertIn("id", cols)
            self.assertIn("timestamp", cols)
            self.assertEqual(cols[0], "id", "ستون اول باید id باشد (ترتیب اعلام)")
            self.assertEqual(schema_columns(conn, "table_that_does_not_exist"), [])
            with self.assertRaises(DatabaseError):
                conn.execute("PRAGMA table_info(logs)")

    def test_alembic_schema_is_complete_on_pg(self):
        with db_connection() as conn:
            self.assertEqual(conn.backend, BACKEND_POSTGRESQL)
            tables = present_tables(conn)
        self.assertEqual([t for t in APP_TABLES if t not in tables], [],
                         "اسکیمای Alembic روی PG ناقص است")
        self.assertIn(ALEMBIC_TABLE, tables)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
