# -*- coding: utf-8 -*-
"""
تست‌های adapter لایه‌ی داده (فاز D — کار D.1).

پوشش:
  A) ConnectionAdapter روی SQLite: execute/executemany/commit/rollback/close،
     context manager (موفق ⇒ commit، خطا ⇒ rollback)، cursor خام و پاس‌تروها،
     و «معادل‌بودن سیمی» با استفاده‌ی مستقیم از sqlite3 روی یک فایل دیتابیس واحد.
  B) تعیین backend از ``DB_BACKEND``: پیش‌فرض sqlite، هر دو مقدار مجاز (از E.2:
     sqlite و postgresql) و مقادیر ناشناخته ⇒ خطای صریح (بدون fallback بی‌صدا).
  C) ترجمه‌ی placeholder (``?`` → ``%s``) — امروز روی هیچ مسیر اجرایی نیست،
     ولی قرارداد فاز E را قفل می‌کند.
  D) سازگاری کد موجود: ``core.database.db_connection`` و
     ``core.security_audit._db_conn`` همان پوسته‌ی adapter هستند و افزودن
     ``add_event`` (که خودش اتصال می‌سازد) واقعاً در دیتابیس می‌نویسد.

نکته‌ی ایزولاسیون: مسیر دیتابیس در ``config.settings.DB_PATH`` نسبی است
(``logs.db``) و دو لایه‌ی adapter/core.database هر دو همان مقدار را import
می‌کنند؛ این تست‌ها با ``os.chdir`` جابه‌جا می‌شوند (مثل سایر تست‌های مخزن) تا
هرگز روی ``logs.db`` اصلی نوشته نشود.
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
    """DB موقت + ورود به دایرکتوری موقت (چون DB_PATH نسبی است)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def physical(self, sql=("SELECT id, name, n FROM t",)):
        """مقایسه‌ی فیزیکی: همان ردیف‌ها مستقیماً از sqlite3 (کنترل مستقل)."""
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
# A) رفتار ConnectionAdapter
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class ConnectionAdapterTests(_TempDbBase):

    def _adapter(self):
        return get_connection(timeout=5.0)

    def test_execute_insert_select_and_row_access(self):
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
            # دسترسی روی خود adapter (الگوی conn.execute(...).fetchone())
            row = conn.execute(_TRACE).fetchone()
            self.assertEqual(tuple(row), (1, "alpha", 1))
            self.assertEqual(len(conn.execute(_TRACE).fetchall()), 2)
            self.assertEqual([c[0] for c in conn.execute(_TRACE).description],
                             ["id", "name", "n"])
        finally:
            conn.close()
        self.assertEqual(self.physical(), [[(1, "alpha", 1), (2, "beta", 2)]])

    def test_executemany_is_single_transaction(self):
        """executemany باید مثل رفتار قبلی یک تراکنش واحد باشد (نه commit ضمنی)."""
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
        """قرارداد فاز D: __exit__ فقط commit/rollback می‌کند؛ close با فراخوان است."""
        with self._adapter() as conn:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("ok", 1))
        self.assertFalse(conn.closed)                       # عمداً: close در __exit__ نیست
        self.assertEqual(self.physical()[0], [(1, "ok", 1)])  # ولی کامیت شده
        conn.close()
        self.assertTrue(conn.closed)

    def test_adapter_exit_rolls_back_and_reraises_on_error(self):
        with self._adapter() as conn:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("ok", 1))
        with self.assertRaises(RuntimeError):
            with self._adapter() as conn:
                conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("boom", 2))
                raise RuntimeError("boom")
        self.assertEqual([r[1] for r in self.physical()[0]], ["ok"])

    def test_db_connection_closes_even_when_body_raises(self):
        """پوسته‌ی سازگاری (db_connection) باید در همه حال اتصال را ببندد."""
        import core.database as database
        with self.assertRaises(RuntimeError):
            with database.db_connection() as conn:
                conn.execute(_DDL)
                raise RuntimeError("boom")
        self.assertTrue(conn.closed)

    def test_cursor_and_raw_connection_are_available(self):
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            cur = conn.cursor()                      # cursor خام (مطابق امضای فاز D)
            cur.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("via-cursor", 7))
            conn.commit()
            self.assertEqual(conn.raw_connection.execute(_TRACE).fetchall(),
                             [(1, "via-cursor", 7)])
        finally:
            conn.close()

    def test_execute_with_default_and_empty_params(self):
        conn = self._adapter()
        try:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("empty", 0))
            conn.execute("UPDATE t SET n = n + 1")   # بدون پارامتر (پیش‌فرض ())
            conn.commit()
            self.assertEqual(conn.execute(_TRACE).fetchall(), [(1, "empty", 1)])
        finally:
            conn.close()

    def test_sqlite_error_is_translated_to_shared_type(self):
        """
        E.4.4 — خطای درایور از adapter با **نوع مشترک** بالا می‌آید (نه ``sqlite3.*``).

        این تست قبلاً «انتشار دست‌نخوردهٔ sqlite3.OperationalError» را قفل می‌کرد؛
        با تصمیم E.4.4 آن قرارداد عوض شد: مصرف‌کننده (core/ و web/) دیگر هیچ
        ``except sqlite3.*`` ندارد و باید نوع مشترک بگیرد.
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
        """پارامتر commit روی get_connection فقط سازگاری امضاست و commit نمی‌کند."""
        conn = get_connection(timeout=5.0, commit=True)
        try:
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("uncommitted", 1))
        finally:
            conn.close()   # بستن بدون commit ⇒ باید دور ریخته شود
        self.assertEqual(self.physical()[0], [])


# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class WireEquivalenceTests(_TempDbBase):
    """
    «دورگیر بودن» adapter را سخت‌گیرانه قفل می‌کند:
    ادعا ⇒ adapter و منطق قبلی (sqlite3 خام + همان سیاست تراکنش) روی کافیِ
    یکسان، ردیف‌های یکسان (و همان نوع/ترتیب ستون‌ها) تولید می‌کنند. اگر
    روزی کسی دسته‌بندی تراکنش یا پارامتری‌سازی را عوض کند، این تست می‌شکند.
    """

    @contextmanager
    def _raw_db_connection(self, db_path, timeout=10.0, commit=False):
        """پیاده‌سازی قبلی core.database.db_connection (عیناً، برای مقایسه)."""
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
        full = os.path.abspath("logs.db")

        @contextmanager
        def raw_facade():
            with self._raw_db_connection(full, timeout=10.0, commit=True) as c:
                yield c

        statements = [
            (_DDL, ()),
            ("INSERT INTO t (name, n) VALUES (?, ?)", ("a", 1)),
            ("INSERT INTO t (name, n) VALUES (?, ?)", ("b", 2)),
            ("INSERT INTO t (name, n) VALUES (?, ?)", ("c", 3)),
            ("UPDATE t SET n = ? WHERE name = ?", (20, "c")),
        ]

        # ── مسیر A: adapter (دقیقاً همان چیزی که core.database الان صدا می‌زند)
        from core.database import db_connection as compat_db_connection
        with compat_db_connection(commit=True) as conn:
            for sql, params in statements:
                conn.execute(sql, params)
            adapter_rows = [tuple(r) for r in conn.execute(_TRACE)]
            adapter_desc = [c[0] for c in conn.execute(_TRACE).description]
            adapter_types = [type(v).__name__ for v in adapter_rows[0]]

        # ── مسیر B: منطق قبلی روی همان دیتابیس (بازخوانی، نه بازنویسی)
        with raw_facade() as conn:
            raw_rows = [tuple(r) for r in conn.execute(_TRACE)]
            raw_desc = [c[0] for c in conn.execute(_TRACE).description]
            raw_types = [type(v).__name__ for v in raw_rows[0]]

        self.assertEqual(adapter_rows, raw_rows)
        self.assertEqual(adapter_desc, raw_desc)
        self.assertEqual(adapter_types, raw_types)
        self.assertEqual(adapter_rows, [(1, "a", 1), (2, "b", 2), (3, "c", 20)])

    def test_commit_flag_semantics_match(self):
        """commit=False ⇒ دور ریخته می‌شود؛ commit=True ⇒ ذخیره می‌شود (مثل قبل)."""
        from core.database import db_connection as compat_db_connection
        with compat_db_connection() as conn:                    # commit=False
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("gone", 1))
        self.assertEqual(self.physical()[0], [])

        with compat_db_connection(commit=True) as conn:
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("kept", 2))
        self.assertEqual([r[1] for r in self.physical()[0]], ["kept"])


class _StubCursor:
    """cursor ساختگی psycopg: فقط SQL/پارامترها را ثبت می‌کند."""

    def __init__(self, log):
        self._log = log
        self.rowcount = -1

    def executemany(self, sql, params_list):
        self._log.append(("executemany", sql, list(params_list)))
        self.rowcount = len(list(params_list))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class _StubConnection:
    """
    اتصال ساختگی سبک psycopg (بدون سرور): برای قفل‌کردن مسیر SQL عبوری از adapter.

    عمداً ``executemany`` **ندارد** — مثل خودِ ``psycopg.Connection``؛ اگر کسی
    دوباره در adapter آن را صدا بزند، همین استاب مثل PG واقعی می‌شکند.
    """

    def __init__(self):
        self.log = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def execute(self, sql, params=()):
        self.log.append(("execute", sql, params))
        return _StubCursor(self.log)

    def cursor(self):
        return _StubCursor(self.log)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


# ─────────────────────────────────────────────────────────────────────
# B) انتخاب backend
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class BackendSelectionTests(unittest.TestCase):

    def test_default_backend_is_sqlite(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_DB_BACKEND, None)
            self.assertEqual(resolve_backend(), BACKEND_SQLITE)
            self.assertEqual(resolve_backend(""), BACKEND_SQLITE)
            self.assertEqual(resolve_backend("  SQLite  "), BACKEND_SQLITE)

    def test_env_variable_is_honored(self):
        with mock.patch.dict(os.environ, {ENV_DB_BACKEND: "sqlite"}):
            self.assertEqual(resolve_backend(), BACKEND_SQLITE)

    def test_postgresql_is_supported_from_phase_e(self):
        """از E.2: postgresql (و نام‌های جایگزینش) backend معتبر است."""
        for value in ("postgresql", "postgres", "pg", "PostgreSQL", "  pg  "):
            self.assertEqual(resolve_backend(value), BACKEND_POSTGRESQL)

    def test_unknown_backend_raises(self):
        with self.assertRaises(UnsupportedBackendError):
            resolve_backend("mysql")
        with mock.patch.dict(os.environ, {ENV_DB_BACKEND: "oracle"}):
            with self.assertRaises(UnsupportedBackendError):
                resolve_backend()

    def test_backend_constants_and_marker_registry_are_consistent(self):
        self.assertEqual(SUPPORTED_BACKENDS, (BACKEND_SQLITE, BACKEND_POSTGRESQL))
        self.assertEqual(set(ALL_BACKENDS), {BACKEND_SQLITE, BACKEND_POSTGRESQL})
        self.assertEqual(set(PLACEHOLDER_STYLE), set(ALL_BACKENDS))
        # پکیج core.db باید همه‌ی نام‌ها را بازصادر کند (کد فاز E روی همین‌ها می‌نشیند)
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
        از E.2: ``DB_BACKEND=postgresql`` بدون ``PG_DSN`` ⇒ ``ConfigurationError``
        (نه fallback بی‌صدا به sqlite، نه اتصال حدسی به سرور پیش‌فرض).
        """
        env = {ENV_DB_BACKEND: "postgresql"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop(ENV_PG_DSN, None)
            with self.assertRaises(ConfigurationError) as ctx:
                get_connection(timeout=1.0)
            self.assertEqual(ctx.exception.backend, BACKEND_POSTGRESQL)
            self.assertIn(ENV_PG_DSN, str(ctx.exception))

    def test_connection_error_is_wrapped(self):
        from core.db import DatabaseConnectionError
        with mock.patch("core.db.connection.sqlite3.connect", side_effect=sqlite3.OperationalError("no")):
            with self.assertRaises(DatabaseConnectionError) as ctx:
                get_connection(timeout=1.0)
            self.assertEqual(ctx.exception.backend, BACKEND_SQLITE)


# ─────────────────────────────────────────────────────────────────────
# B2) قرارداد backend دوگانه — E.2.3 و E.2.4 (بدون نیاز به سرور)
# ─────────────────────────────────────────────────────────────────────
class PostgresSpecificGuardTests(unittest.TestCase):
    """متدهایی که روی SQLite معنا دارند و روی PG باید رفتار روشن داشته باشند."""

    def _pg(self, raw=None):
        return ConnectionAdapter(raw or _StubConnection(), backend=BACKEND_POSTGRESQL)

    def test_lastrowid_on_postgresql_raises_with_actionable_message(self):
        conn = self._pg()
        with self.assertRaises(NotImplementedError) as ctx:
            _ = conn.lastrowid
        msg = str(ctx.exception)
        self.assertIn("RETURNING", msg)
        self.assertIn("E.4", msg)

    def test_lastrowid_on_sqlite_still_forwards(self):
        """روی SQLite هیچ تغییری نکرده است (قرارداد فاز D)."""
        conn = ConnectionAdapter(_StubConnection(), backend=BACKEND_SQLITE)
        conn._last_cursor = mock.Mock(lastrowid=42)
        self.assertEqual(conn.lastrowid, 42)

    def test_executescript_on_postgresql_splits_and_executes(self):
        """
        E.4.7 — روی PG اسکریپت به statement می‌شکند و هر کدام جدا اجرا می‌شود
        (psycopg فقط در پروتکل ساده چنددستوری را قبول می‌کند: با پارامترِ غیرخالی
        «cannot insert multiple commands into a prepared statement» می‌دهد).
        """
        raw = _StubConnection()
        conn = ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)
        conn.executescript("CREATE TABLE a (x INT);\n-- کامنت\nCREATE TABLE b (y INT);")
        self.assertEqual([sql for _op, sql, _p in raw.log],
                         ["CREATE TABLE a (x INT)", "-- کامنت\nCREATE TABLE b (y INT)"])
        # ترجمه‌ی placeholder روی هر statement هم اعمال می‌شود
        raw2 = _StubConnection()
        conn2 = ConnectionAdapter(raw2, backend=BACKEND_POSTGRESQL)
        conn2.executescript("SELECT '50%' WHERE a = ?; SELECT 2;")
        self.assertEqual(raw2.log[0][1], "SELECT '50%%' WHERE a = %s")

    def test_executescript_rejects_dollar_quoted_scripts(self):
        conn = self._pg()
        with self.assertRaises(ValueError) as ctx:
            conn.executescript("CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;")
        self.assertIn("dollar", str(ctx.exception))

    def test_row_factory_is_noop_on_postgresql_and_forwarded_on_sqlite(self):
        pg = self._pg()
        pg.row_factory = sqlite3.Row          # نباید خطا بدهد
        self.assertIsNone(pg.row_factory)     # و نباید اثری داشته باشد

        sqlite_raw = _StubConnection()
        sq = ConnectionAdapter(sqlite_raw, backend=BACKEND_SQLITE)
        sq.row_factory = sqlite3.Row
        self.assertIs(sq.row_factory, sqlite3.Row)

    def test_no_active_cursor_error_type_matches_backend(self):
        import psycopg
        with self.assertRaises(sqlite3.ProgrammingError):
            ConnectionAdapter(_StubConnection(), backend=BACKEND_SQLITE).fetchone()
        with self.assertRaises(psycopg.ProgrammingError):
            self._pg().fetchone()

    def test_cursor_returns_raw_driver_cursor(self):
        raw = _StubConnection()
        self.assertIsInstance(self._pg(raw).cursor(), _StubCursor)


class ResolveDsnTests(unittest.TestCase):
    """E.2.4 — تعیین صریح نشانی اتصال (پارامتر ⇒ PG_DSN ⇒ ConfigurationError)."""

    def test_explicit_dsn_wins(self):
        from core.db import resolve_dsn
        with mock.patch.dict(os.environ, {ENV_PG_DSN: "host=env-db"}):
            self.assertEqual(resolve_dsn(BACKEND_POSTGRESQL, "host=explicit"), "host=explicit")

    def test_env_dsn_used_when_no_explicit(self):
        from core.db import resolve_dsn
        with mock.patch.dict(os.environ, {ENV_PG_DSN: "  host=env-db  "}):
            self.assertEqual(resolve_dsn(BACKEND_POSTGRESQL), "host=env-db")

    def test_missing_dsn_raises_configuration_error(self):
        from core.db import resolve_dsn
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_PG_DSN, None)
            with self.assertRaises(ConfigurationError) as ctx:
                resolve_dsn(BACKEND_POSTGRESQL)
            self.assertIn(ENV_PG_DSN, str(ctx.exception))
            self.assertEqual(ctx.exception.backend, BACKEND_POSTGRESQL)

    def test_sqlite_has_no_dsn_and_unknown_backend_raises(self):
        from core.db import resolve_dsn
        self.assertIsNone(resolve_dsn(BACKEND_SQLITE))
        with self.assertRaises(UnsupportedBackendError):
            resolve_dsn("mysql")

    def test_connect_without_dsn_raises_before_touching_psycopg(self):
        """حتی اگر psycopg نصب باشد، بدون DSN هیچ اتصالی تلاش نمی‌شود."""
        import core.db as pkg
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_PG_DSN, None)
            with self.assertRaises(ConfigurationError):
                pkg.connect(backend=BACKEND_POSTGRESQL)

    def test_adapter_rejects_unknown_backend_early(self):
        with self.assertRaises(UnsupportedBackendError):
            ConnectionAdapter(_StubConnection(), backend="mysql")
        with self.assertRaises(UnsupportedBackendError):
            ConnectionAdapter(_StubConnection(), backend="postgres")  # نام جایگزین این‌جا معتبر نیست


# ─────────────────────────────────────────────────────────────────────
# C) ترجمه‌ی placeholder (قرارداد فاز E)
# ─────────────────────────────────────────────────────────────────────
class PlaceholderTranslationTests(unittest.TestCase):

    def test_sqlite_is_identity(self):
        sql = "SELECT * FROM t WHERE a = ? AND b = ?"
        self.assertIs(translate_placeholders(sql, BACKEND_SQLITE), sql)

    def test_postgresql_translation(self):
        self.assertEqual(
            translate_placeholders("SELECT * FROM t WHERE a = ? AND b = ?",
                                   BACKEND_POSTGRESQL),
            "SELECT * FROM t WHERE a = %s AND b = %s")

    def test_translation_skips_strings_identifiers_and_comments(self):
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
        self.assertEqual(
            translate_placeholders("SELECT 'it''s ?' WHERE a = ?", BACKEND_POSTGRESQL),
            "SELECT 'it''s ?' WHERE a = %s")

    def test_unknown_backend_and_bad_input_raise(self):
        from core.db import PlaceholderTranslationError
        with self.assertRaises(PlaceholderTranslationError):
            translate_placeholders("SELECT ?", "mysql")
        with self.assertRaises(PlaceholderTranslationError):
            translate_placeholders(None, BACKEND_POSTGRESQL)

    # ── E.2.2: escape درصد برای psycopg ──────────────────────────────
    def test_percent_escape_default_is_per_backend(self):
        """پیش‌فرض: postgresql ⇒ escape، sqlite ⇒ دست‌نخورده (بدون هیچ تغییری)."""
        sql = "SELECT 'نرخ 50%' || ?"
        self.assertEqual(
            translate_placeholders(sql, BACKEND_POSTGRESQL),
            "SELECT 'نرخ 50%%' || %s")
        self.assertEqual(translate_placeholders(sql, BACKEND_SQLITE), sql)

    def test_percent_escape_covers_strings_comments_and_operators(self):
        from core.db import escape_percent_signs
        sql = "SELECT 'a%b', n % 2 -- 10% comment\n/* 5% */ WHERE x LIKE '%y%' AND z = ?"
        out = translate_placeholders(sql, BACKEND_POSTGRESQL)
        # هیچ `%` تنها نمی‌ماند (همه زوج شده‌اند) و تنها placeholder یک `?` بود
        self.assertNotIn("%", out.replace("%%", "").replace("%s", ""))
        self.assertEqual(out.count("%s"), 1)
        self.assertEqual(out.count("%%"), 6)
        self.assertEqual(out, escape_percent_signs(sql).replace("?", "%s"))

    def test_escape_happens_before_placeholder_insertion(self):
        """ترتیب مهم است: `%s`های تازه‌ساخته‌شده نباید دوباره escape شوند."""
        out = translate_placeholders("SELECT '100%' || ? || '50%'", BACKEND_POSTGRESQL)
        self.assertEqual(out, "SELECT '100%%' || %s || '50%%'")

    def test_escape_can_be_disabled_or_rejected_for_sqlite(self):
        from core.db import PlaceholderTranslationError
        self.assertEqual(
            translate_placeholders("SELECT '100%' || ?", BACKEND_POSTGRESQL,
                                   escape_percent=False),
            "SELECT '100%' || %s")
        with self.assertRaises(PlaceholderTranslationError):
            translate_placeholders("SELECT '100%' || ?", BACKEND_SQLITE,
                                   escape_percent=True)

    def test_sqlite_path_is_untouched_even_with_percent(self):
        """adb: مسیر SQLite هیچ‌وقت escape نمی‌شود (رفتار sqlite3 تغییری نمی‌کند)."""
        sql = "SELECT '100%' FROM t WHERE a = ?"
        self.assertIs(translate_placeholders(sql, BACKEND_SQLITE), sql)


class AdapterSqlPathTests(unittest.TestCase):
    """
    مسیر SQL عبوری از adapter: sqlite دست‌نخورده، postgresql ترجمه‌شده.

    با اتصال ساختگی انجام می‌شود تا این قرارداد بدون نیاز به سرور PG قفل شود
    (آزمون واقعی روی PG در کلاس PGContractTests با گارد در دسترس بودن).
    """

    def test_postgresql_translates_before_sending(self):
        raw = _StubConnection()
        conn = ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)
        conn.execute("SELECT '100%' FROM t WHERE a = ? AND b = ?", (1, 2))
        self.assertEqual(raw.log[-1][1], "SELECT '100%%' FROM t WHERE a = %s AND b = %s")
        self.assertEqual(raw.log[-1][2], (1, 2))

    def test_postgresql_executemany_uses_cursor(self):
        """psycopg.Connection متد executemany ندارد ⇒ adapter باید از cursor برود."""
        raw = _StubConnection()
        conn = ConnectionAdapter(raw, backend=BACKEND_POSTGRESQL)
        conn.executemany("INSERT INTO t (a) VALUES (?)", [(1,), (2,)])
        self.assertEqual(raw.log[-1][0], "executemany")
        self.assertEqual(raw.log[-1][1], "INSERT INTO t (a) VALUES (%s)")
        self.assertEqual(raw.log[-1][2], [(1,), (2,)])

    def test_sqlite_path_sends_sql_untouched(self):
        raw = _StubConnection()   # صرفاً ثبت‌کننده؛ رفتار sqlite3 در تست‌های بالاست
        conn = ConnectionAdapter(raw, backend=BACKEND_SQLITE)
        conn.execute("SELECT '100%' FROM t WHERE a = ?", (1,))
        self.assertEqual(raw.log[-1][1], "SELECT '100%' FROM t WHERE a = ?")

    def test_unexpected_translation_failure_is_wrapped(self):
        """هر خطای غیرمنتظره‌ی ترجمه به PlaceholderTranslationError بسته می‌شود."""
        from core.db import PlaceholderTranslationError
        conn = ConnectionAdapter(_StubConnection(), backend=BACKEND_POSTGRESQL)
        with mock.patch("core.db.connection.translate_placeholders",
                        side_effect=ValueError("boom")):
            with self.assertRaises(PlaceholderTranslationError):
                conn.execute("SELECT 1")

    def test_app_code_uses_no_raw_cursor(self):
        """
        E.4.6 — کد برنامه (``core/`` و ``web/``) نباید ``cursor()`` خودش را بگیرد.

        دلیل: ``cursor()`` **خام** است و SQLش از ترجمه‌ی placeholder و رفتار مشترک
        adapter عبور نمی‌کند؛ روی PG یعنی SQL باید دو-سبکی نوشته شود (منبع باگ).
        استثنا: خودِ ``core/db/connection.py`` که پیاده‌سازی adapter است.
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
    """E.4.7 — شکستن اسکریپت: ``;`` داخل رشته/کامنت جداکننده نیست."""

    def _split(self, script):
        from core.db.scripts import split_sql_statements
        return split_sql_statements(script)

    def test_simple_script(self):
        self.assertEqual(self._split("CREATE TABLE a (x INT); CREATE TABLE b (y INT);"),
                         ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"])

    def test_semicolon_inside_string_literal(self):
        self.assertEqual(self._split("INSERT INTO t VALUES ('a;b'); SELECT 1;"),
                         ["INSERT INTO t VALUES ('a;b')", "SELECT 1"])

    def test_escaped_quotes(self):
        self.assertEqual(self._split("SELECT 'it''s; fine';"),
                         ["SELECT 'it''s; fine'"])

    def test_quoted_identifier(self):
        self.assertEqual(self._split('CREATE TABLE "we;ird" (x INT);'),
                         ['CREATE TABLE "we;ird" (x INT)'])

    def test_comments_are_not_separators(self):
        self.assertEqual(self._split("SELECT 1; /* a; b */ SELECT 2;"),
                         ["SELECT 1", "/* a; b */ SELECT 2"])
        self.assertEqual(self._split("SELECT 1; -- کامنت با ; داخل\nSELECT 2;"),
                         ["SELECT 1", "-- کامنت با ; داخل\nSELECT 2"])

    def test_trailing_and_empty_statements(self):
        self.assertEqual(self._split("SELECT 1"), ["SELECT 1"])
        self.assertEqual(self._split("  ;;\n;  "), [])
        self.assertEqual(self._split("-- فقط کامنت\n"), [])

    def test_invalid_scripts_raise_clear_errors(self):
        with self.assertRaises(ValueError):
            self._split("SELECT 'unterminated")
        with self.assertRaises(ValueError):
            self._split("SELECT 1 /* unterminated")
        with self.assertRaises(ValueError):
            self._split("DO $$ BEGIN END $$;")
        with self.assertRaises(ValueError):
            self._split(None)


class _ReturningStubCursor(_StubCursor):
    """cursor استاب که یک ردیف برمی‌گرداند (برای مسیر ``RETURNING id``)."""

    def __init__(self, log, row):
        super().__init__(log)
        self._row = row

    def fetchone(self):
        return self._row


class _ReturningStubConnection(_StubConnection):
    """اتصال استاب سبک psycopg با ``fetchone`` قابل‌تنظیم."""

    def __init__(self, row=None):
        super().__init__()
        self._row = row

    def execute(self, sql, params=()):
        self.log.append(("execute", sql, params))
        return _ReturningStubCursor(self.log, self._row)


# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class InsertReturningIdTests(_TempDbBase):
    """
    E.4.3 — «INSERT + شناسه‌ی ردیف» با یک امضای مشترک روی هر دو backend.

    ``lastrowid`` مفهومِ SQLite است و روی PG وجود ندارد؛ قرارداد درست PG
    ``INSERT … RETURNING id`` است. این کلاس مسیر SQLite را روی یک DB واقعی و مسیر
    PG را با استاب سبک psycopg (بدون سرور) قفل می‌کند؛ PG واقعی در
    ``PostgresContractTests.test_insert_returning_id_on_real_postgres``.
    """

    def _sqlite(self):
        return get_connection(timeout=5.0)

    # ── مسیر SQLite (DB واقعی؛ بازخوانی مستقل با sqlite3) ────────
    def test_sqlite_returns_autoincrement_id(self):
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
        ثبت یک تفاوت واقعی: ``sqlite3`` وقتی هیچ ردیفی درج نشود ``lastrowid`` را
        دست‌نخورده (مقدار درج قبلی) می‌گذارد ⇒ خواندن خام آن شناسه‌ی اشتباه می‌داد.
        قرارداد ``insert_returning_id`` در این حالت ``None`` است.
        """
        conn = self._sqlite()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
            self.assertEqual(
                conn.insert_returning_id("INSERT INTO t (name) VALUES (?)", ("a",)), 1)
            stale = conn.execute("INSERT INTO t (name) SELECT ? WHERE 0", ("z",)).lastrowid
            self.assertEqual(stale, 1)      # اثبات اینکه مشکل واقعی است
            self.assertIsNone(
                conn.insert_returning_id("INSERT INTO t (name) SELECT ? WHERE 0", ("z",)))
            conn.commit()
            self.assertEqual(self.physical(("SELECT count(*) FROM t",)), [[(1,)]])
        finally:
            conn.close()

    def test_sqlite_params_default_to_empty(self):
        conn = self._sqlite()
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
            self.assertEqual(conn.insert_returning_id("INSERT INTO t (name) VALUES ('x')"), 1)
        finally:
            conn.close()

    # ── مسیر PostgreSQL (استاب سبک psycopg، بدون سرور) ───────────
    def test_postgresql_appends_returning_and_returns_id(self):
        stub = _ReturningStubConnection(row=(7,))
        conn = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertEqual(
            conn.insert_returning_id("INSERT INTO t (name) VALUES (?)", ("a",)), 7)
        self.assertEqual(
            stub.log, [("execute", "INSERT INTO t (name) VALUES (%s) RETURNING id", ("a",))])

    def test_postgresql_strips_trailing_semicolon_and_whitespace(self):
        stub = _ReturningStubConnection(row=(1,))
        conn = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        conn.insert_returning_id("INSERT INTO t (name)\nVALUES (?);\t \n", ("a",))
        self.assertEqual(len(stub.log), 1)
        self.assertTrue(stub.log[0][1].endswith("VALUES (%s) RETURNING id"), stub.log[0][1])
        self.assertNotIn(";", stub.log[0][1].splitlines()[-1])

    def test_postgresql_returns_none_when_no_row(self):
        stub = _ReturningStubConnection(row=None)
        conn = ConnectionAdapter(stub, backend=BACKEND_POSTGRESQL)
        self.assertIsNone(
            conn.insert_returning_id("INSERT INTO t (name) SELECT 'x' WHERE false"))

    def test_existing_returning_is_rejected_before_executing(self):
        for backend in (BACKEND_SQLITE, BACKEND_POSTGRESQL):
            with self.subTest(backend=backend):
                stub = _ReturningStubConnection()
                conn = ConnectionAdapter(stub, backend=backend)
                with self.assertRaises(ValueError):
                    conn.insert_returning_id(
                        "INSERT INTO t (name) VALUES (?) RETURNING id", ("a",))
                self.assertEqual(stub.log, [])   # هیچ SQLی اجرا نشد


def _pg_test_target():
    """
    نشانی PG برای تست‌های یکپارچه: ``PG_DSN`` + یک probe فقط-خواندنی.

    Returns:
        ``(dsn, None)`` اگر در دسترس باشد، وگرنه ``(None, دلیلِ skip)`` — دلیل
        عمداً روشن است تا «چرا این تست‌ها اجرا نشدند» در خروجی pytest دیده شود.
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
    همان قرارداد adapter، این بار روی **PostgreSQL واقعی** (E.2.5).

    اصول:
      • هیچ جدول برنامه‌ای لمس نمی‌شود: هر تست فقط ``CREATE TEMP TABLE`` می‌سازد
        (در پایان session خودش می‌رود) و در پایان rollback می‌کند؛
      • بدون ``PG_DSN`` یا با سرور خاموش، کلاس با پیام روشن skip می‌شود (بدون خطا).
    """

    def setUp(self):
        if not _PG_TEST_DSN:  # نگهبان دوم: اگر کسی skipUnless را برداشت
            self.skipTest(_PG_SKIP_REASON or "PG در دسترس نیست")
        self.conn = connect_postgres(_PG_TEST_DSN, timeout=5.0)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def _temp_table(self):
        self.conn.execute("CREATE TEMP TABLE t_pg (id INTEGER, name TEXT, n INTEGER)")
        self.conn.commit()   # DDL در PG تراکنشی است ⇒ برای ماندگاری در همین session

    # ── اتصال ────────────────────────────────────────────────────
    def test_connection_contract(self):
        self.assertEqual(self.conn.backend, BACKEND_POSTGRESQL)
        self.assertFalse(self.conn.raw_connection.autocommit)
        self.assertEqual(self.conn.execute("SHOW timezone").fetchone()[0], "UTC")
        version = self.conn.execute("SHOW server_version").fetchone()[0]
        major_version = int(version.split(".")[0])
        self.assertGreaterEqual(major_version, 16, version)
        self.conn.rollback()

    # ── همان قرارداد SQLite، این بار روی PG ──────────────────────
    def test_execute_insert_select_with_question_marks(self):
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
        self._temp_table()
        self.conn.executemany("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)",
                              [(1, "a", 1), (2, "b", 2), (3, "c", 3)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM t_pg").fetchone()[0], 3)
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM t_pg").fetchone()[0], 0)

    def test_commit_persists_and_rollback_discards(self):
        self._temp_table()
        self.conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)", (1, "keep", 1))
        self.conn.commit()
        self.conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)", (2, "drop", 2))
        self.conn.rollback()
        rows = self.conn.execute("SELECT id FROM t_pg ORDER BY id").fetchall()
        self.assertEqual(rows, [(1,)])

    def test_percent_literals_work_with_and_without_params(self):
        self.assertEqual(self.conn.execute("SELECT '100%'").fetchone()[0], "100%")
        self.assertEqual(self.conn.execute("SELECT 'نرخ 50%', ?", (7,)).fetchone(),
                         ("نرخ 50%", 7))
        self.assertTrue(self.conn.execute("SELECT ? LIKE '%alp%'", ("alpha",)).fetchone()[0])
        self.conn.rollback()

    def test_context_manager_commits(self):
        self._temp_table()
        with self.conn as conn:                      # __exit__ ⇒ commit
            conn.execute("INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?)", (1, "ctx", 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM t_pg").fetchone()[0], 1)

    # ── تفاوت‌های مستند PG ───────────────────────────────────────
    def test_lastrowid_raises_and_returning_is_the_way(self):
        self._temp_table()
        with self.assertRaises(NotImplementedError):
            _ = self.conn.lastrowid
        new_id = self.conn.execute(
            "INSERT INTO t_pg (id, name, n) VALUES (?, ?, ?) RETURNING id",
            (5, "ret", 5)).fetchone()[0]
        self.assertEqual(new_id, 5)

    def test_cursor_is_raw_so_sql_uses_driver_style(self):
        """
        ``cursor()`` **خام** است ⇒ ترجمه‌ی placeholder روی آن اعمال نمی‌شود و SQL
        باید با سبک خودِ درایور نوشته شود (روی PG: ``%s``).

        این یک «یافتهٔ اندازه‌گیری‌شده» است، نه سلیقه: adapter فقط روی
        ``execute/executemany/executescript`` خودش ترجمه می‌کند. پس ``cursor()`` برای
        ابزار/تست باقی می‌ماند، ولی **کد برنامه از آن استفاده نمی‌کند**: مصرف‌کننده‌های
        قدیمی (``web/routes/stats.py``، ``web/routes/metrics.py``، ``init_db``) در
        E.4.6 به ``conn.execute`` منتقل شدند و
        ``test_app_code_uses_no_raw_cursor`` جلوی بازگشتشان را می‌گیرد.
        """
        self._temp_table()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO t_pg (id, name, n) VALUES (%s, %s, %s)", (1, "cur", 1))
        self.conn.commit()
        self.assertEqual(cur.execute("SELECT count(*) FROM t_pg").fetchone()[0], 1)
        # و ثابت می‌کنیم سبک qmark روی cursor خام کار **نمی‌کند** (قرارداد صریح):
        with self.assertRaises(Exception):
            cur.execute("SELECT count(*) FROM t_pg WHERE id = ?", (1,))

    def test_executescript_runs_on_real_postgres(self):
        """E.4.7 روی PG واقعی: اسکریپت دو‌دستوری واقعاً اجرا می‌شود."""
        self.conn.executescript(
            "CREATE TEMP TABLE scr_a (x INT);\nCREATE TEMP TABLE scr_b (y INT);")
        self.conn.execute("INSERT INTO scr_a (x) VALUES (1)")
        self.conn.execute("INSERT INTO scr_b (y) VALUES (2)")
        self.assertEqual(self.conn.execute("SELECT x FROM scr_a").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT y FROM scr_b").fetchone()[0], 2)
        self.conn.rollback()

    def test_lastrowid_is_the_only_remaining_sqlite_only_feature(self):
        """تنها جلوه‌ی SQLite-only باقی‌مانده ``lastrowid`` است (راه‌حل: E.4.3)."""
        with self.assertRaises(NotImplementedError):
            _ = self.conn.lastrowid

    def test_insert_returning_id_on_real_postgres(self):
        """
        E.4.3 روی PG واقعی: شناسه از ``RETURNING id`` می‌آید، idها پیوسته‌اند و
        SQL چندخطی/نقطه‌ویرگول‌دار هم کار می‌کند (بدون ``lastrowid``).
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
        # صفر ردیف ⇒ None (نه شناسهٔ درج قبلی)
        self.assertIsNone(
            self.conn.insert_returning_id("INSERT INTO t_ids (name) SELECT 'x' WHERE false"))
        self.conn.rollback()


# ─────────────────────────────────────────────────────────────────────
# D) سازگاری کد موجود
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class ExistingCodeCompatibilityTests(_TempDbBase):

    def test_core_db_public_api_works_standalone(self):
        """API عمومی پکیج: core.db.db_connection باید مستقلاً قابل استفاده باشد."""
        with db_connection(commit=True) as conn:
            self.assertIsInstance(conn, ConnectionAdapter)
            conn.execute(_DDL)
            conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("pkg", 1))
        self.assertTrue(conn.closed)
        self.assertEqual([r[1] for r in self.physical()[0]], ["pkg"])

    def test_core_database_db_connection_is_adapter(self):
        import core.database as database
        with database.db_connection() as conn:
            self.assertIsInstance(conn, ConnectionAdapter)
            self.assertEqual(conn.backend, BACKEND_SQLITE)

    def test_security_audit_db_conn_is_adapter_and_writes(self):
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
        """مسیر کامل production: init_db → add_event (که خودش اتصال می‌سازد)."""
        import core.database as database
        database.init_db()
        database.add_event("192.0.2.99", "PRINT",
                           {"pages": 3, "message": "adapter-compat",
                            "timestamp": "2026-10-03T10:00:00"})
        rows = database.get_log("192.0.2.99")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pages"], 3)
        self.assertEqual(rows[0]["type"], "PRINT")
        # بازخوانی فیزیکی مستقل (بدون adapter) برای اطمینان از واقعی‌بودن نوشتن
        raw = sqlite3.connect("logs.db")
        try:
            types = [r[0] for r in raw.execute(
                "SELECT type FROM logs WHERE printer_ip = ?", ("192.0.2.99",))]
        finally:
            raw.close()
        self.assertEqual(types, ["PRINT"])

    def test_default_timeout_is_thirty_seconds(self):
        """امضای core.db.db_connection: پیش‌فرض ۳۰s (فاز D) در برابر ۱۰s قدیمی."""
        import inspect
        from core.db import db_connection as core_conn
        import core.database as database
        self.assertEqual(inspect.signature(core_conn).parameters["timeout"].default, 30.0)
        self.assertEqual(inspect.signature(database.db_connection).parameters["timeout"].default, 10.0)

    def test_create_user_uses_insert_returning_id(self):
        """
        E.4.3 — تنها مصرف زنده‌ی ``lastrowid`` (``core.database.create_user``) به
        ``insert_returning_id`` منتقل شد؛ این تست جلوی بازگشت خاموش را می‌گیرد
        (روی PG، ``lastrowid`` وجود ندارد ⇒ ``create_user`` بی‌صدا None می‌داد).
        """
        import inspect
        import core.database as database
        src = inspect.getsource(database.create_user)
        self.assertIn("insert_returning_id", src)
        # کامنت‌ها ممکن است نام lastrowid را توضیح دهند؛ معیار، کدِ بی‌کامنت است:
        code_only = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        self.assertNotIn("lastrowid", code_only)

        database.init_db()
        user = database.create_user("e43_regression", "e43.regression@example.com", "h")
        self.assertIsNotNone(user)
        self.assertIsInstance(user["id"], int)
        self.assertGreater(user["id"], 0)
        self.assertEqual(database.get_user_by_id(user["id"])["username"], "e43_regression")
        # مسیر ردِ نام کاربری نامعتبر (پیش از هر اتصالی) دست‌نخورده مانده است
        self.assertIsNone(database.create_user("bad name!", "x@example.com", "h"))

    def test_no_silent_fallback_on_error(self):
        """اگر ساخت اتصال شکست بخورد، خطای صریح می‌آید (نه fallback به backend دیگر)."""
        import core.database as database
        from core.db import DatabaseConnectionError
        with mock.patch("core.db.connection.sqlite3.connect", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(DatabaseConnectionError):
                with database.db_connection() as conn:
                    conn.execute("SELECT 1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
