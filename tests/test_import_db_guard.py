# -*- coding: utf-8 -*-
"""
تست‌های E.4.8 — import روی PostgreSQL: SAVEPOINT هر ردیف + گارد یکپارچه + بولی.

چهار قاعده‌ای که این فایل قفل می‌کند
------------------------------------
۱) **SAVEPOINT هر ردیف (فقط روی PG).** روی PostgreSQL اولین خطای درج، کل
   تراکنش را abort می‌کند (``25P02``) و ردیف‌های سالمِ همان دسته هم از دست
   می‌روند؛ سنجش E.4.4 نشان داد یک دسته‌ی سه‌ردیفی با یک تکراری به
   ``rows_now=0`` می‌رسد. پس هر ردیف در ``SAVEPOINT sp_row`` خودش درج می‌شود و
   خطا با ``ROLLBACK TO SAVEPOINT sp_row`` به همان ردیف محدود می‌ماند.
   روی SQLite **هیچ SAVEPOINT‌ای باز نمی‌شود** (رفتار امروز، بایت‌به‌بایت).

۲) **گارد یکپارچه‌ی endpoint.** تصمیم مالک (E.4.8): به‌جای چند گارد پراکنده،
   ``_reject_import_on_postgres()`` به‌عنوان نخستین کار ``confirm_import`` همه‌ی
   مسیرهای import روی PG را با یک ``UnsupportedFeatureError`` روشن می‌بندد
   (TD-007 + TD-009 + ردیف ۱۱ strictness که هر سه در E.6 رفع می‌شوند).
   روی SQLite گارد **فعال نیست** و رفتار امروز دست‌نخورده است.
   هیچ تست ON CONFLICT در این فایل نیست — آن کار E.6 است.

۳) **نرمال‌سازی بولی.** ``yield_samples.accepted`` روی PG نوع ``BOOLEAN`` دارد
   (migration 0002، E.4.1) و ``int`` را با خطای نوع رد می‌کند؛ مقدار مبدأ
   (فایل SQLite، ``0/1``) پیش از درج به ``bool`` تبدیل می‌شود و ``NULL``
   دست‌نخورده می‌ماند.

۴) **مسیر ``analyze`` مسدود نمی‌شود** (فقط فایل مبدأ را می‌خواند و مبدأ همیشه
   SQLite است) — گارد فقط روی endpoint اجرای import است.

بدون ``PG_DSN``، کلاس‌های PGِ زنده با پیام روشن skip می‌شوند (بقیه اجرا می‌شوند).
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from core.db import (
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_PG_DSN,
    DatabaseError,
    UniqueViolation,
    UnsupportedFeatureError,
    connect_postgres,
    connect_sqlite,
)
from core.db.schema import BOOLEAN_COLUMNS, boolean_columns_of, schema_columns
from web.routes import import_db

ROOT = Path(__file__).resolve().parent.parent
# تصمیم E.6.1: پیام گارد باید «تصمیم» را اعلام کند، نه «مهلت» — این چهار نشانه
# همان چیزی است که یک مدیر سیستم برای فهمیدن علت و مسیر جایگزین لازم دارد.
GUARD_MARKERS = ("E.6.1", "TD-007", "TD-009", "ردیف ۱۱")


def _pg_target():
    """``(dsn, None)`` اگر PG در دسترس باشد، وگرنه ``(None, دلیلِ skip)``."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ تست‌های PGِ زنده‌ی این فایل اجرا "
                      f"نمی‌شوند (اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ تست‌های PG رد می‌شوند")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()


# ─────────────────────────────────────────────────────────────────────
# ابزار تست
# ─────────────────────────────────────────────────────────────────────
class _Rows:
    """نتیجهٔ ساختگی: هم ``fetchall`` دارد هم ``fetchone`` (مثل cursor درایور)."""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _RecordingAdapter:
    """
    پوشش نازک روی یک اتصال واقعی (adapter) که SQLها را ثبت می‌کند.

    برای تست «روی SQLite هیچ SAVEPOINT‌ای باز نمی‌شود» — بدون دست‌زدن به
    helper‌های خصوصی ماژول.
    """

    def __init__(self, conn):
        self._conn = conn
        self.sql = []

    @property
    def backend(self):
        return self._conn.backend

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return self._conn.execute(sql, params)


class _ScriptedAdapter:
    """
    اتصال ساختگی PG که برای هر الگوی SQL ردیف‌های مشخصی برمی‌گرداند
    (بدون سرور). ``fail_by_needle`` می‌تواند استثنای مشخصی روی الگوی داده‌شده بیندازد.
    """

    backend = BACKEND_POSTGRESQL

    def __init__(self, rows_by_needle=None, fail_by_needle=None):
        self.rows_by_needle = dict(rows_by_needle or {})
        self.fail_by_needle = dict(fail_by_needle or {})
        self.log = []           # (sql, params)

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        for needle, exc in self.fail_by_needle.items():
            if needle in sql:
                raise exc
        for needle, rows in self.rows_by_needle.items():
            if needle in sql:
                return _Rows(rows)
        return _Rows([])

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    # ── کمک‌کارها ──
    def statements(self):
        return [sql for sql, _params in self.log]


def _unique_violation(message="duplicate key value violates unique constraint"):
    return UniqueViolation(message, backend=BACKEND_POSTGRESQL, sqlstate="23505")


def _make_source_db(tmpdir, table="printer_counters", ddl=None, rows=()):
    """فایل SQLite مبدأ (همان چیزی که import آپلود می‌کند) را می‌سازد."""
    path = os.path.join(tmpdir, "source.db")
    raw = sqlite3.connect(path)
    try:
        raw.execute(ddl or f"CREATE TABLE {table} (ip TEXT PRIMARY KEY, print_total INTEGER)")
        for row in rows:
            raw.execute(
                f"INSERT INTO {table} VALUES ({','.join(['?'] * len(row))})", row)
        raw.commit()
    finally:
        raw.close()
    return path


def _new_admin_user(username):
    """یک ادمین در دیتابیس فعال (SQLite موقت یا PG) می‌سازد."""
    from models import User
    return User.create(username=username, email=f"{username}@test.io",
                       password="secret123", role="admin", is_verified=True)


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ─────────────────────────────────────────────────────────────────────
# A) SQLite — رفتار امروز دست‌نخورده
# ─────────────────────────────────────────────────────────────────────
class SqliteImportBehaviourTests(unittest.TestCase):
    """مسیر SQLite نه SAVEPOINT می‌گیرد و نه بولی را عوض می‌کند."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_replace_mode_still_works_on_sqlite(self):
        """حالت replace روی SQLite همان کار همیشگی را می‌کند (بدون گارد)."""
        target = connect_sqlite(os.path.join(self.tmp.name, "target.db"))
        self.addCleanup(target.close)
        target.execute("CREATE TABLE printer_counters (ip TEXT PRIMARY KEY, print_total INTEGER)")
        target.execute("INSERT INTO printer_counters (ip, print_total) VALUES (?, ?)",
                       ("203.0.113.10", 100))
        target.commit()

        source = connect_sqlite(_make_source_db(
            self.tmp.name, rows=[("203.0.113.10", 555), ("203.0.113.11", 7)]))
        self.addCleanup(source.close)

        result = import_db._import_table(source, target, "printer_counters", {}, "replace")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)

        got = dict(target.execute(
            "SELECT ip, print_total FROM printer_counters").fetchall())
        self.assertEqual(got["203.0.113.10"], 555, "ردیف قبلی باید جایگزین شده باشد")
        self.assertEqual(got["203.0.113.11"], 7)

    def test_sqlite_never_opens_savepoints(self):
        """روی SQLite هیچ ``SAVEPOINT``‌ای باز نمی‌شود (رفتار E.4.8 فقط PG است)."""
        target = connect_sqlite(os.path.join(self.tmp.name, "target2.db"))
        self.addCleanup(target.close)
        target.execute("CREATE TABLE printer_counters (ip TEXT PRIMARY KEY, print_total INTEGER)")
        recording = _RecordingAdapter(target)

        source = connect_sqlite(_make_source_db(
            self.tmp.name, table="printer_counters",
            rows=[("203.0.113.20", 1), ("203.0.113.21", 2)]))
        self.addCleanup(source.close)

        result = import_db._import_table(
            source, recording, "printer_counters", {}, "skip")
        self.assertEqual(result["imported"], 2)
        savepoints = [s for s in recording.sql if "SAVEPOINT" in s.upper()]
        self.assertEqual(savepoints, [], f"روی SQLite نباید SAVEPOINT باشد: {savepoints}")


# ─────────────────────────────────────────────────────────────────────
# B) PostgreSQL با اتصال ساختگی (بدون سرور) — SAVEPOINT
# ─────────────────────────────────────────────────────────────────────
class PostgresSavepointStubTests(unittest.TestCase):
    """ترتیب SAVEPOINT/RELEASE و محدودماندن خطا به یک ردیف، روی مسیر PG."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = connect_sqlite(_make_source_db(
            self.tmp.name, rows=[("203.0.113.30", 3), ("203.0.113.31", 4)]))
        self.addCleanup(self.source.close)
        # مقصد PG ساختگی: فهرست ستون‌هایش از information_schema می‌آید، پس
        # همان هِلپر cross-backend جواب می‌دهد (هِلپر E.4.8 همین را برای E.6 آماده کرد).
        self._columns_patch = mock.patch.object(
            import_db, "_columns",
            side_effect=lambda conn, table: schema_columns(conn, table))
        self._columns_patch.start()
        self.addCleanup(self._columns_patch.stop)

    def test_plain_insert_uses_one_savepoint_per_row_on_pg(self):
        """روی PG هر ردیف: ``SAVEPOINT`` ⇒ ``INSERT`` ⇒ ``RELEASE`` (به همان ترتیب)."""
        target = _ScriptedAdapter(rows_by_needle={
            "information_schema.columns": [("ip",), ("print_total",)],
            "SELECT 1 FROM": [],                      # dedupe: ردیف وجود ندارد
        })
        result = import_db._import_table(self.source, target, "printer_counters", {}, "skip")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)

        sqls = target.statements()
        self.assertEqual([s for s in sqls if "INSERT OR REPLACE" in s.upper()], [])
        inserts = [i for i, s in enumerate(sqls) if s.upper().startswith("INSERT INTO")]
        self.assertEqual(len(inserts), 2, sqls)
        for i in inserts:
            self.assertEqual(sqls[i - 1].upper(), "SAVEPOINT SP_ROW", sqls)
            self.assertIn("RELEASE SAVEPOINT SP_ROW", sqls[i + 1].upper(), sqls)
        self.assertEqual(len([s for s in sqls if "SAVEPOINT" in s.upper()]), 4)

    def test_duplicate_row_does_not_stop_the_batch_on_pg(self):
        """ردیف تکراری فقط SAVEPOINT خودش را برمی‌گرداند؛ ردیف‌های بعدی می‌روند."""
        target = _ScriptedAdapter(
            rows_by_needle={"information_schema.columns": [("ip",), ("print_total",)],
                            "SELECT 1 FROM": []},
            fail_by_needle={"INSERT INTO": _unique_violation()})
        result = import_db._import_table(self.source, target, "printer_counters", {}, "skip")
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped"], 2, "هر دو ردیف تکراری‌اند ⇒ هر دو skip")

        sqls = [s.upper() for s in target.statements()]
        self.assertEqual(len([s for s in sqls if s == "ROLLBACK TO SAVEPOINT SP_ROW"]), 2)
        # هیچ خطایی از حلقه بیرون نزده و هر SAVEPOINT بسته شده است.
        self.assertEqual(len([s for s in sqls if s == "SAVEPOINT SP_ROW"]),
                         len([s for s in sqls if s.startswith("RELEASE SAVEPOINT")]))


# ─────────────────────────────────────────────────────────────────────
# C) گارد یکپارچه (بدون DB)
# ─────────────────────────────────────────────────────────────────────
class ImportGuardUnitTests(unittest.TestCase):
    """``_reject_import_on_postgres`` فقط روی PG بالا می‌آید و پیامش راهنما دارد."""

    def test_guard_raises_on_postgresql(self):
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql"}):
            with self.assertRaises(UnsupportedFeatureError) as ctx:
                import_db._reject_import_on_postgres()
        message = str(ctx.exception)
        for marker in GUARD_MARKERS:
            self.assertIn(marker, message, f"پیام گارد باید «{marker}» را داشته باشد")
        self.assertEqual(ctx.exception.backend, BACKEND_POSTGRESQL)
        self.assertIsInstance(ctx.exception, DatabaseError)
        self.assertIn("SQLite", message, "پیام باید مسیر سالم را هم روشن کند")

    def test_guard_message_states_retirement_not_a_deadline(self):
        """
        پیام گارد پس از تصمیم E.6.1 باید «بازنشستگی» و مسیرهای سالم را بگوید.

        چرا این تست هست: تا پیش از E.6.1 پیام «تا E.6 پشتیبانی نمی‌شود» بود — یک
        مهلت، که بعد از E.6 هم می‌توانست درست به‌نظر برسد و کاربر را معطل کند.
        """
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql"}):
            with self.assertRaises(UnsupportedFeatureError) as ctx:
                import_db._reject_import_on_postgres()
        message = str(ctx.exception)
        self.assertIn("بازنشستگی", message, "پیام باید بگوید این مسیر **ترجمه نمی‌شود**")
        self.assertIn("analyze", message, "پیام باید بگوید پیش‌نمایش فایل کار می‌کند")
        self.assertNotIn("تا E.6 ", message, "مهلتِ منقضی‌شده نباید در پیام بماند")

    def test_guard_is_silent_on_sqlite(self):
        for value in ("sqlite", "", "postgres", "pg"):     # دو نام جایگزین PG هم تست می‌شوند
            with self.subTest(DB_BACKEND=value):
                with mock.patch.dict(os.environ, {"DB_BACKEND": value}):
                    if value in ("postgres", "pg"):
                        with self.assertRaises(UnsupportedFeatureError):
                            import_db._reject_import_on_postgres()
                    else:
                        self.assertIsNone(import_db._reject_import_on_postgres())

    def test_guard_is_the_first_statement_of_confirm_import(self):
        """گارد باید **پیش از** هر بررسی فایل/نوشتنی اجرا شود (قید مالک)."""
        import ast
        tree = ast.parse((ROOT / "web" / "routes" / "import_db.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "confirm_import")
        first = fn.body[0]
        self.assertIsInstance(first, ast.Try, "نخستین دستور باید بلوک گارد باشد")
        called = [n.func.id for n in ast.walk(first)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("_reject_import_on_postgres", called)

    def test_guard_does_not_touch_the_analyze_endpoint(self):
        """``analyze`` فقط مبدأ را می‌خواند ⇒ نباید گارد داشته باشد."""
        source = (ROOT / "web" / "routes" / "import_db.py").read_text(encoding="utf-8")
        analyze = source.split("def analyze_db()", 1)[1].split("@bp.route", 1)[0]
        self.assertNotIn("_reject_import_on_postgres", analyze)


# ─────────────────────────────────────────────────────────────────────
# C۲) مبدأ همیشه SQLite است — قفل backend در connect_to (تصمیم E.6.1)
# ─────────────────────────────────────────────────────────────────────
class SqliteSourceUnderPostgresEnvTests(unittest.TestCase):
    """
    ``analyze`` نباید به ``DB_BACKEND`` نگاه کند: آنچه باز می‌شود یک **فایل** است.

    پیش از E.6.1، ``connect_to`` بک‌اند را از محیط می‌خواند و زیر
    ``DB_BACKEND=postgresql`` خودِ ``connect()`` با ``ConfigurationError``
    («path همراه postgresql») رد می‌شد ⇒ endpoint با ۵۰۰ «خطا در تحلیل دیتابیس»
    برمی‌گشت. این کلاس همان را بدون نیاز به PGِ زنده می‌آزماید — و چون
    ``PG_DSN`` را خالی می‌گذارد، هر بازگشتِ قفل، تست را **loud** می‌شکند.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql", ENV_PG_DSN: ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_connect_to_opens_the_sqlite_file_under_postgres_env(self):
        from core.db import connect_to
        path = _make_source_db(self.tmp.name, table="logs", ddl=(
            "CREATE TABLE logs (printer_ip TEXT, timestamp TEXT, type TEXT, message TEXT)"),
            rows=[("10.9.9.20", "2026-10-04T00:00:00.000000Z", "PRINT", "x")])
        conn = connect_to(path, timeout=5.0)
        self.addCleanup(conn.close)
        self.assertEqual(conn.backend, BACKEND_SQLITE,
                         "فایل SQLite باید با adapter SQLite باز شود، فارغ از DB_BACKEND")
        self.assertIn("logs", import_db._tables(conn))
        self.assertEqual(import_db._table_count(conn, "logs"), 1)
        rng = import_db._table_range(conn, "logs", "timestamp")
        self.assertEqual(rng["start"], "2026-10-04T00:00:00.000000Z")
        self.assertEqual([p["ip"] for p in import_db._table_printers(conn, "logs", "printer_ip")],
                         ["10.9.9.20"])

    def test_the_same_call_without_the_pin_would_still_fail(self):
        """شاهد معکوس: connect(path) بدون قفل backend همان ConfigurationError را می‌دهد."""
        from core.db import ConfigurationError, connect
        path = _make_source_db(self.tmp.name)
        with self.assertRaises(ConfigurationError):
            connect(path, timeout=5.0)


class DiscardUploadTests(unittest.TestCase):
    """``_discard_upload`` فایل ردشده را پاک و کلید session را برداشته می‌کند (E.6.1)."""

    def test_removes_file_and_session_key(self):
        from flask import Flask, session
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = _make_source_db(tmp.name)
        app = Flask(__name__)
        app.secret_key = "test-only"              # session برای این helper لازم است
        with app.test_request_context("/"):
            session["import_temp_path"] = path
            import_db._discard_upload()
            self.assertFalse(os.path.exists(path), "آپلودِ ردشده نباید روی دیسک بماند")
            self.assertNotIn("import_temp_path", dict(session))

    def test_missing_file_is_not_an_error(self):
        from flask import Flask, session
        app = Flask(__name__)
        app.secret_key = "test-only"
        with app.test_request_context("/"):
            session["import_temp_path"] = "/nonexistent/temp_import_gone.db"
            self.assertIsNone(import_db._discard_upload())   # بی‌صدا و بی‌خطا


# ─────────────────────────────────────────────────────────────────────
# D) نرمال‌سازی بولی (بدون DB)
# ─────────────────────────────────────────────────────────────────────
class BooleanCoercionTests(unittest.TestCase):
    """``yield_samples.accepted`` روی PG ``bool`` می‌خواهد؛ روی SQLite همان 0/1."""

    def test_schema_boolean_list_matches_migration_0002(self):
        """فهرست ``BOOLEAN_COLUMNS`` نباید از migration 0002 واگرا شود."""
        spec = importlib.util.spec_from_file_location(
            "_mig_0002", ROOT / "alembic" / "versions" / "0002_boolean_columns.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual([(t, c) for t, c, _new, _old in module.BOOLEAN_COLUMNS],
                         list(BOOLEAN_COLUMNS))

    def test_only_yield_samples_boolean_is_importable(self):
        """از ۵ ستون بولی، فقط ``yield_samples.accepted`` در جدول‌های قابل import است."""
        self.assertEqual(boolean_columns_of("yield_samples"), ("accepted",))
        self.assertEqual(boolean_columns_of("users"), ("is_verified", "email_verified", "is_active"))
        for table in import_db.ALLOWED_IMPORT_TABLES:
            if table != "yield_samples":
                self.assertEqual(boolean_columns_of(table), (), table)

    def test_values_become_bool_for_postgres(self):
        """``1/0`` ⇒ ``True/False`` و ``NULL`` دست‌نخورده (فقط برای مقصد PG)."""
        cols = ["printer_ip", "color", "accepted"]
        for raw, expected in ((1, True), (0, False), (2, True), (None, None)):
            with self.subTest(raw=raw):
                out = import_db._prepare_row(
                    "yield_samples", cols, ("10.0.0.1", "black", raw), for_postgres=True)
                self.assertIs(out[2], expected)

    def test_values_are_untouched_for_sqlite(self):
        """روی SQLite هیچ تبدیلی انجام نمی‌شود (۰ تغییر رفتاری)."""
        cols = ["printer_ip", "color", "accepted"]
        for raw in (1, 0, 2, None):
            with self.subTest(raw=raw):
                row = ("10.0.0.1", "black", raw)
                self.assertIs(
                    import_db._prepare_row("yield_samples", cols, row, for_postgres=False), row)

    def test_non_boolean_table_is_identity(self):
        """جدولی که ستون بولی ندارد عیناً برمی‌گردد (همان tuple، بدون کپی)."""
        row = ("203.0.113.40", 5)
        self.assertIs(
            import_db._prepare_row("printer_counters", ["ip", "print_total"], row,
                                   for_postgres=True),
            row)


# ─────────────────────────────────────────────────────────────────────
# E) endpoint روی SQLite واقعی — گارد فعال نیست
# ─────────────────────────────────────────────────────────────────────
# marker `sqlite_only`: این تست SQL/اسکیمای مختص SQLite می‌نویسد ⇒ زیر
# `DB_BACKEND=postgresql` با auto-skip رد می‌شود (tests/conftest.py، docs/phase-e-plan.md §۵.۳).
@pytest.mark.sqlite_only
class ImportEndpointSqliteTests(unittest.TestCase):
    """روی SQLite، ``confirm_import`` همان مسیر امروز را می‌رود (بدون پیام گارد)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                    # DB_PATH نسبی ⇒ DB موقت
        self.addCleanup(self._restore)

        from core.database import init_db
        from web import create_app
        init_db()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        _login(self.client, _new_admin_user("imp_admin_sqlite"))

    def _restore(self):
        os.chdir(self.old_cwd)

    def test_confirm_without_uploaded_file_is_the_old_error(self):
        """بدون فایل موقت ⇒ همان پیام همیشگی، نه پیام گارد PG."""
        response = self.client.post("/api/import/confirm",
                                    json={"tables": ["logs"], "options": {}})
        self.assertEqual(response.status_code, 400)
        error = response.get_json()["error"]
        self.assertIn("فایل موقت یافت نشد", error)
        self.assertNotIn("TD-007", error)


# ─────────────────────────────────────────────────────────────────────
# F) PostgreSQL واقعی — SAVEPOINT، بولی و گارد سرتاسری
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresLiveImportTests(unittest.TestCase):
    """
    همان رفتارها، این بار روی PG ۱۶ واقعی.

    ``_columns`` ماژول روی PG با ``PRAGMA`` می‌شکند (ردیف ۱۱ strictness، کار E.6)،
    پس این‌جا با هِلپر cross-backend ``schema_columns`` جایگزین می‌شود — همان
    تعویضی که E.6 انجام خواهد داد.

    ``duplicate_mode="allow"`` استفاده می‌شود (یکی از سه مقدار مجاز API:
    ``skip | allow | replace``) چون این تست‌ها رفتار «درج ساده» را می‌سنجند.
    مسیر ``skip`` روی PG به ``_exists_by_cols`` می‌رسد و آن‌جا ``col IS $1``
    ساخته می‌شود که نحو PostgreSQL نیست — همان TD-009 که امروز زیر گارد
    یکپارچه‌ی endpoint است و در E.6 رفع می‌شود.
    """

    def setUp(self):
        if not _PG_DSN:                     # نگهبان دوم
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = connect_postgres(_PG_DSN)
        self.addCleanup(self.target.close)
        patcher = mock.patch.object(
            import_db, "_columns",
            side_effect=lambda conn, table: schema_columns(conn, table))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _cleanup_ips(self, table, column, ips):
        """پاک‌کردن ردیف‌های تست (بعد از هر خطای احتمالی، تراکنش زنده می‌شود)."""
        self.target.rollback()
        for ip in ips:
            self.target.execute(f"DELETE FROM {table} WHERE {column} = ?", (ip,))
        self.target.commit()

    def test_good_rows_survive_a_bad_row(self):
        """
        سه ردیف، وسطی ``ip = NULL`` (نقض NOT NULL ⇒ ``UniqueViolation`` روی PG):
        بدون SAVEPOINT، کل دسته از بین می‌رفت؛ با SAVEPOINT دو ردیف سالم می‌مانند.
        """
        good_a, good_b = "203.0.113.240", "203.0.113.241"
        source = connect_sqlite(_make_source_db(
            self.tmp.name, rows=[(good_a, 11), (None, 12), (good_b, 13)]))
        self.addCleanup(source.close)
        self._cleanup_ips("printer_counters", "ip", [good_a, good_b])

        result = import_db._import_table(source, self.target, "printer_counters", {}, "allow")
        self.target.commit()
        try:
            self.assertEqual(result["imported"], 2, result)
            self.assertEqual(result["skipped"], 1, result)
            rows = dict(self.target.execute(
                "SELECT ip, print_total FROM printer_counters WHERE ip IN (?, ?)",
                (good_a, good_b)).fetchall())
            self.assertEqual(rows, {good_a: 11, good_b: 13},
                             "ردیف‌های سالم باید کامیت شده باشند (SAVEPOINT کار کند)")
        finally:
            self._cleanup_ips("printer_counters", "ip", [good_a, good_b])

    def test_boolean_accepted_is_inserted_as_real_boolean(self):
        """``accepted`` از فایل SQLite (``0/1``) روی PG به ``BOOLEAN`` واقعی می‌رسد."""
        ip = "203.0.113.242"
        ddl = ("CREATE TABLE yield_samples (printer_ip TEXT, color TEXT, created_at TEXT, "
               "start_counter INTEGER, end_counter INTEGER, start_level INTEGER, "
               "end_level INTEGER, accepted INTEGER)")
        source = connect_sqlite(_make_source_db(
            self.tmp.name, table="yield_samples", ddl=ddl,
            rows=[(ip, "black", "2026-10-04T00:00:00.000000Z", 100, 200, 90, 80, 1),
                  (ip, "cyan", "2026-10-04T00:00:01.000000Z", 100, 200, 90, 80, 0)]))
        self.addCleanup(source.close)
        self._cleanup_ips("yield_samples", "printer_ip", [ip])

        result = import_db._import_table(source, self.target, "yield_samples", {}, "allow")
        self.target.commit()
        try:
            self.assertEqual(result["imported"], 2, result)
            self.assertEqual(result["skipped"], 0, result)
            rows = self.target.execute(
                "SELECT color, accepted FROM yield_samples WHERE printer_ip = ? ORDER BY color",
                (ip,)).fetchall()
            self.assertEqual(rows, [("black", True), ("cyan", False)])
            self.assertIsInstance(rows[0][1], bool, "روی PG نوع ستون BOOLEAN است، نه int")
        finally:
            self._cleanup_ips("yield_samples", "printer_ip", [ip])


@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresLiveEndpointGuardTests(unittest.TestCase):
    """``POST /api/import/confirm`` روی PG واقعی: هر سه حالت ⇒ ۴۰۰ با پیام گارد."""

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)

        from core.database import init_db
        from web import create_app
        init_db()                                   # روی PG = تأیید اسکیمای Alembic
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.user = _new_admin_user("imp_admin_pg")
        self.addCleanup(self._delete_user)
        _login(self.client, self.user)

    def _delete_user(self):
        from core.database import db_connection
        try:
            with db_connection(commit=True) as conn:
                conn.execute("DELETE FROM users WHERE username = ?", ("imp_admin_pg",))
        except Exception:                            # پاک‌سازی نباید تست را بی‌اعتبار کند
            pass

    def test_all_modes_are_blocked_before_any_file_check(self):
        """گارد پیش از بررسی فایل می‌زند: بدون حتی یک فایل آپلودی، ۴۰۰ گارد."""
        for mode in ("skip", "allow", "replace"):
            with self.subTest(mode=mode):
                response = self.client.post(
                    "/api/import/confirm",
                    json={"tables": ["logs"], "options": {"duplicate_mode": mode}})
                self.assertEqual(response.status_code, 400)
                error = response.get_json()["error"]
                for marker in GUARD_MARKERS:
                    self.assertIn(marker, error)
                self.assertNotIn("خطای داخلی", error, "نباید ۵۰۰ عمومی شود")

    def test_rejected_confirm_discards_the_uploaded_file(self):
        """E.6.1: مسیر بازنشسته نباید فایل آپلودی را روی دیسک جا بگذارد."""
        import io as _io
        old_cwd = os.getcwd()
        tmp = tempfile.TemporaryDirectory()
        os.chdir(tmp.name)                       # مسیر آپلود نسبی است ⇒ پوشهٔ موقت
        try:
            ddl = ("CREATE TABLE logs (printer_ip TEXT, timestamp TEXT, type TEXT, "
                   "message TEXT)")
            rows = [("10.9.9.20", "2026-10-04T00:00:00.000000Z", "PRINT", "x")]
            source_path = _make_source_db(tmp.name, table="logs", ddl=ddl, rows=rows)
            with open(source_path, "rb") as fh:
                payload = fh.read()
            resp = self.client.post(
                "/api/import/analyze",
                data={"file": (_io.BytesIO(payload), "old.db")},
                content_type="multipart/form-data")
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertTrue([f for f in os.listdir(".") if f.startswith("temp_import_")],
                            "analyze باید فایل موقت را ساخته باشد")

            blocked = self.client.post("/api/import/confirm", json={"tables": ["logs"]})
            self.assertEqual(blocked.status_code, 400)
            for marker in GUARD_MARKERS:
                self.assertIn(marker, blocked.get_json()["error"])
            self.assertEqual([f for f in os.listdir(".") if f.startswith("temp_import_")], [],
                             "پس از رد گارد نباید آپلودی بماند")
        finally:
            os.chdir(old_cwd)
            tmp.cleanup()

    def test_analyze_is_not_blocked_on_pg(self):
        """``analyze`` روی PG مسدود نیست؛ فقط روی فایل مبدأ (SQLite) کار می‌کند."""
        import io as _io
        response = self.client.post(
            "/api/import/analyze",
            data={"file": (_io.BytesIO(b"not a database"), "old.db")},
            content_type="multipart/form-data")
        self.assertEqual(response.status_code, 500, "فایل نامعتبر ⇒ خطای تحلیل، نه گارد")
        self.assertNotIn("TD-007", response.get_json().get("error", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
