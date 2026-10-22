# -*- coding: utf-8 -*-
"""
تست‌های E.6.4 — ``logs.details`` روی PostgreSQL ⇒ ``jsonb``؛ بقیه ستون‌ها TEXT.

معیار مالک برای این تصمیم «``grep json_extract``» بود: فقط ستونی به JSONB می‌رود
که **SQL** روی آن محتوای JSON می‌خواند. آن گرِپ یک ستون داد (``logs.details`` — در
``web/routes/metrics.py`` و در dedup رویدادهای ``core/database.py``)؛ چهار ستون
JSON دیگر فقط در پایتون سریال‌شده‌اند ⇒ **عمداً** TEXT مانده‌اند. این فایل همان
دامنه را قفل می‌کند تا «یکدستیِ بی‌دلیل» بعداً خودبه‌خود اضافه نشود.

سه چیز تست می‌شود — همان سه چیزی که تصمیم رویشان گرفته شد:
  ۱) نوع ستون روی PG = ``jsonb``، روی SQLite = ``TEXT`` (واگرایی آگاهانه)؛ چهار
     ستون دیگر روی PG هم ``text`` می‌مانند؛
  ۲) خواننده‌ها با هر دو شکل مقدار کار می‌کنند (psycopg مقدار ``jsonb`` را
     دیکدشده می‌دهد) و **معنای SQLite عوض نشده**: JSON بدفرمت همان‌جا همچنان خطا
     می‌دهد، بی‌صدا ``{}`` نه؛
  ۳) preflightِ migration بی‌رحم است: یک ردیف نامعتبر ⇒ توقف با شمارش و idها؛
     رشتهٔ خالی ⇒ فقط هشدار (چون به NULL تبدیل می‌شود و این تغییر داده است).

هزینه/سود اندازه‌گیری‌شده (PG 16.15، ۲۰۰٬۰۰۰ ردیف) در ``docs/phase-e-decisions.md``
ردیف E.6.4 آمده: کوئری metrics ۱۱۹٫۳ms ⇒ ۴۶٫۸ms (۲٫۵×)، dedup ناچیز.

روی PGها از ``PG_DSN`` استفاده می‌شود؛ تست‌های «رفتار برنامه» با ``skipUnless`` روی
``DB_BACKEND=postgresql`` قفل‌اند تا در اجرای SQLite بی‌صدا «سبزِ نادرست» ندهند.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.migration_graph import migration_head

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "alembic" / "versions" / "0004_jsonb_columns.py"

ENV_PG_DSN = "PG_DSN"
_PG_DSN = (os.getenv(ENV_PG_DSN, "") or "").strip()


def _pg_ready():
    if not _PG_DSN:
        return False, f"{ENV_PG_DSN} ست نشده ⇒ رفتار JSONB روی PG آزموده نشد"
    try:
        import psycopg
        with psycopg.connect(_PG_DSN, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        return False, f"PG در دسترس نیست ({type(exc).__name__}) ⇒ رفتار JSONB آزموده نشد"
    return True, None


_PG_OK, _PG_SKIP = _pg_ready()

def _migration():
    spec = importlib.util.spec_from_file_location("migration_0004_under_test", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dsn() -> str:
    """
    DSN **در زمان اجرا** خوانده می‌شود: fixture سوئیت، ``PG_DSN`` را به نشانی
    اسکیمای ایزوله (با ``options=-csearch_path=…``) بازنویسی می‌کند و import این
    فایل زودتر از fixture اتفاق می‌افتد.
    """
    return (os.getenv(ENV_PG_DSN, "") or "").strip() or _PG_DSN


def _dsn_for(schema: str) -> str:
    """DSN خام + ``options=-csearch_path`` (الگوی tests/conftest.py؛ فاصله مجاز نیست)."""
    from tests.conftest import _dsn_with_search_path
    return _dsn_with_search_path(_PG_DSN, schema)


def _pg_conn(dsn=None):
    """اتصال مستقل با autocommit (temp table / DDL درون تست‌ها)."""
    import psycopg
    return psycopg.connect(dsn or _dsn(), autocommit=True)


class MigratedSchemaMixin:
    """
    یک اسکیمای ایزوله که رویش ``alembic upgrade head`` اجرا شده است.

    چرا تست خودش اسکیماساز می‌سازد و به fixture سوئیت اتکا نمی‌کند: ادعای این
    کلاس‌ها «مهاجرت 0004 نوع ستون را عوض می‌کند» است؛ پس باید مستقل از وضعیت
    اسکیمای محیط سنجیده شود و در اجرای SQLite هم **باجوید** (نه skip — قاعدهٔ
    «skip بی‌صدا» در این سوئیت ممنوع است).
    """

    _schema = None

    @classmethod
    def setUpClass(cls):
        if not _PG_OK:
            return
        cls._schema = f"printguard_e64_{uuid.uuid4().hex[:10]}"
        with _pg_conn(_PG_DSN) as conn:
            conn.execute(f"CREATE SCHEMA {cls._schema}")
        env = dict(os.environ)
        env["DB_BACKEND"] = "postgresql"
        env[ENV_PG_DSN] = _dsn_for(cls._schema)
        proc = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                              cwd=str(ROOT), env=env, capture_output=True, text=True,
                              timeout=300)
        if proc.returncode != 0:
            raise AssertionError("alembic upgrade head روی "
                                 f"{cls._schema} شکست:\n"
                                 + (proc.stdout + proc.stderr)[-2500:])

    @classmethod
    def tearDownClass(cls):
        if cls._schema:
            with _pg_conn(_PG_DSN) as conn:
                conn.execute(f"DROP SCHEMA IF EXISTS {cls._schema} CASCADE")
            cls._schema = None

    @classmethod
    def dsn(cls) -> str:
        return _dsn_for(cls._schema)


class MigrationScopeTests(unittest.TestCase):
    """دامنهٔ تصمیم + زنجیرهٔ revisionها."""

    def test_only_logs_details_is_converted(self):
        m = _migration()
        self.assertEqual(m.JSONB_COLUMNS, (("logs", "details"),),
                         "دامنهٔ E.6.4 فقط logs.details است؛ هر ستون تازه باید "
                         "دلیل و اندازه‌گیری خودش را داشته باشد")

    def test_revision_chain_is_linear_after_identity_columns(self):
        m = _migration()
        self.assertEqual((m.revision, m.down_revision), ("0004", "0003"))

    def test_0004_is_the_head_of_the_chain(self):
        """سرِ زنجیره 0007 است (0004 پیش از migration 0005, 0006, 0007 بود)."""
        self.assertEqual(migration_head(), "0007")


class SqliteSchemaUnchangedTests(unittest.TestCase):
    """سمت SQLite: هیچ چیزی تبدیل نمی‌شود — روی schema واقعی مخزن بررسی می‌شود."""

    @staticmethod
    def _decls(table):
        conn = sqlite3.connect(f"file:{ROOT / 'logs.db'}?mode=ro", uri=True)
        try:
            return {r[1]: (r[2] or "").upper() for r in conn.execute(
                f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_logs_details_is_still_text_on_sqlite(self):
        self.assertEqual(self._decls("logs").get("details"), "TEXT")

    def test_the_python_side_columns_are_text_on_both_backends(self):
        for table, column in (("users", "allowed_offices"), ("users", "allowed_modules"),
                              ("printer_counters", "cartridge_id_state"),
                              ("printer_counters", "toner_overrides"),
                              ("security_events", "details")):
            with self.subTest(column=f"{table}.{column}"):
                self.assertEqual(self._decls(table).get(column), "TEXT")


class ReaderToleranceTests(unittest.TestCase):
    """``core.database._row_to_dict`` هر دو شکل مقدار را می‌پذیرد."""

    ROW_HEAD = ("1.2.3.4", "PRN", "2026-10-04T00:00:00Z", "PRINT", "msg",
                7, "black", "C1", "info", "A4", "user1")

    def _row(self, details):
        return self.ROW_HEAD + (details,)

    def test_dict_from_jsonb_is_used_as_is(self):
        from core.database import _row_to_dict
        out = _row_to_dict(self._row({"prev_total": 10, "current_total": 12}))
        self.assertEqual(out["details"], {"prev_total": 10, "current_total": 12})
        self.assertEqual(out["pages"], 7)
        self.assertEqual(out["timestamp"], "2026-10-04T00:00:00Z")

    def test_string_from_sqlite_is_parsed(self):
        from core.database import _row_to_dict
        self.assertEqual(_row_to_dict(self._row('{"prev_total": 10}'))["details"],
                         {"prev_total": 10})

    def test_empty_and_none_yield_empty_dict(self):
        from core.database import _row_to_dict
        for value in ("", None):
            with self.subTest(value=repr(value)):
                self.assertEqual(_row_to_dict(self._row(value))["details"], {})

    def test_malformed_string_still_raises_on_the_sqlite_path(self):
        """
        قیدِ «رفتار SQLite عوض نشد»: امروز JSON بدفرمت در این تابع استثنا می‌دهد.
        اگر کسی بخواهد برای «سازگاری» آن را با ``_load_json_dict`` ببلعد، این تست
        می‌ایستد — بلعیدن خطا یعنی صفحه‌ای که بی‌صدا دادهٔ کم نشان می‌دهد.
        """
        from core.database import _row_to_dict
        with self.assertRaises(json.JSONDecodeError):
            _row_to_dict(self._row("{oops"))


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PgColumnTypeTests(MigratedSchemaMixin, unittest.TestCase):
    """نوع ستون‌ها روی PG، آن‌طور که migration 0004 ساخته است."""

    @staticmethod
    def _type(conn, table, column):
        return conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s "
            "AND column_name = %s", (table, column)).fetchone()

    def test_schema_under_test_is_at_head(self):
        with _pg_conn(self.dsn()) as conn:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        self.assertEqual(row[0], migration_head(), "اسکیمای ایزوله به سرِ زنجیره نرسیده")

    def test_logs_details_is_jsonb_after_the_migration(self):
        with _pg_conn(self.dsn()) as conn:
            row = self._type(conn, "logs", "details")
        self.assertIsNotNone(row, "جدول logs در اسکیمای ایزوله نیست (alembic اجرا نشده؟)")
        self.assertEqual(row[0], "jsonb")

    def test_the_four_python_side_columns_stay_text(self):
        with _pg_conn(self.dsn()) as conn:
            for table, column in (("users", "allowed_offices"),
                                  ("users", "allowed_modules"),
                                  ("printer_counters", "cartridge_id_state"),
                                  ("printer_counters", "toner_overrides"),
                                  ("security_events", "details")):
                with self.subTest(column=f"{table}.{column}"):
                    row = self._type(conn, table, column)
                    self.assertIsNotNone(row, f"{table}.{column} در اسکیمای ایزوله نیست")
                    self.assertEqual(row[0], "text",
                                     "E.6.4 فقط logs.details را تبدیل کرد")

    def test_helper_fragment_still_evaluates_on_a_jsonb_value(self):
        """
        هلپر ``(col::jsonb)->>'key'`` می‌سازد؛ روی ستون ``jsonb`` این cast بی‌هزینه و
        بی‌تغییر معناست — همین است که «کد دامنه دست‌نخورده» را ممکن کرد.
        """
        from core.db import connect_postgres
        conn = connect_postgres(_dsn())
        try:
            frag = conn.json_extract("details", "prev_total", as_number=True)
            self.assertIn("::jsonb", frag)
            row = conn.execute(
                f"SELECT {frag} FROM (SELECT '{{\"prev_total\": 41}}'::jsonb AS details) t"
            ).fetchone()
            self.assertEqual(float(row[0]), 41.0)
        finally:
            conn.close()


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PgAppRoundTripTests(MigratedSchemaMixin, unittest.TestCase):
    """مسیر واقعی برنامه: نوشتن رویداد ⇒ خواندن ردیف ⇒ همان dict روی ستون jsonb."""

    IP = "10.99.99.98"

    def setUp(self):
        # backend و DSN را خودمان قفل می‌کنیم (الگوی CollationOrderingTests در
        # test_strictness_pg.py): تستی که توابع سطح‌بالای برنامه را صدا می‌زند و
        # محیط را نمی‌سازد، می‌تواند روی logs.db مخزن بنویسد.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql",
                                               ENV_PG_DSN: self.dsn()})
        patcher.start()
        self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)

    def _clear(self):
        from core.db import get_connection
        get_connection().execute("DELETE FROM logs WHERE printer_ip = ?", (self.IP,))

    def test_add_event_and_get_log_round_trip(self):
        from core import database as db
        self._clear()
        self.addCleanup(self._clear)
        db.add_event(self.IP, "PRINT", {
            "printer_name": "JSONB PROBE", "message": "round trip",
            "prev_total": 3, "current_total": 9, "probe": "e64",
        })
        rows = db.get_log(self.IP, limit=5)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["details"].get("probe"), "e64")
        self.assertEqual(rows[0]["details"].get("prev_total"), 3)
        self.assertEqual(rows[0]["message"], "round trip")
        # قرارداد E.3 که روی jsonb هم باید برقرار بماند: ستون‌های جدول بر کلیدهای
        # payload برنده‌اند (این IP در store.PRINTERS نیست ⇒ ستون None است، حتی اگر
        # کلید `printer_name` داخل details مقدار داشته باشد).
        self.assertIsNone(rows[0]["printer_name"])
        self.assertEqual(rows[0]["details"]["printer_name"], "JSONB PROBE")

    def test_write_rejects_malformed_json_at_insert_time(self):
        """
        سودِ دومِ تصمیم: اعتبار JSON در زمان **نوشتن**. امروز روی متن، یک ردیف
        بدفرمت همه‌چیز را در زمان خواندن می‌شکند؛ اینجا خودِ درج رد می‌شود.
        """
        from core.db import get_connection
        conn = get_connection()
        with self.assertRaises(Exception) as ctx:
            conn.execute("INSERT INTO logs (printer_ip, type, timestamp, details) "
                         "VALUES (?, 'PRINT', '2026-10-04T00:00:00Z', ?)",
                         (self.IP, "{oops"))
        self.assertIn("json", str(ctx.exception).lower())


@unittest.skipUnless(_PG_OK, _PG_SKIP or "")
class PreflightSafetyTests(unittest.TestCase):
    """preflight migration: نامعتبر ⇒ توقف با شمارش؛ خالی ⇒ هشدار؛ پاک ⇒ رد نشدن."""

    TABLE = "probe_logs_e64"

    #: جدول **موقت** (pg_temp) — preflight نباید در اسکیمای واقعی چیزی بسازد.
    def _setup(self, conn, values):
        conn.execute(f"CREATE TEMP TABLE {self.TABLE} "
                     f"(id serial primary key, details text)")
        for v in values:
            conn.execute(f"INSERT INTO {self.TABLE} (details) VALUES (%s)", (v,))

    def _teardown(self, conn):
        conn.execute(f"DROP TABLE IF EXISTS {self.TABLE}")  # temp ⇒ پایان جلسه کافی است

    def test_malformed_row_aborts_with_count_and_ids(self):
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ['{"a":1}', "{oops", "not json", '{"b":2}'])
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    m._preflight(conn, self.TABLE, "details")
            finally:
                self._teardown(conn)
        msg = str(ctx.exception)
        self.assertIn("2 ردیف", msg, "پیام باید بشمارد که چند ردیف خراب است")
        self.assertIn("0004_jsonb_columns", msg)
        self.assertIn("نمونه idها", msg)
        self.assertIn("!~ '^[{[]'", msg, "کوئری راهنما باید روی PG معتبر باشد")

    def test_clean_data_passes(self):
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ['{"a":1}', "[1,2]", "13", '"str"', "null", None])
            try:
                m._preflight(conn, self.TABLE, "details")
            finally:
                self._teardown(conn)

    def test_empty_string_only_warns_because_it_becomes_null(self):
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ["", "   ", '{"a":1}'])
            try:
                with self.assertLogs("alembic.runtime.migration", level="WARNING") as logs:
                    m._preflight(conn, self.TABLE, "details")
            finally:
                self._teardown(conn)
        joined = "\n".join(logs.output)
        self.assertIn("2 ردیف", joined)
        self.assertIn("NULL", joined)

    def test_validator_function_is_dropped_afterwards(self):
        """تابع pg_temp نباید بماند؛ «شیء دائمیِ بی‌سند» در اسکیما ساختیم نه."""
        m = _migration()
        with _pg_conn(_PG_DSN) as conn:
            self._setup(conn, ['{"a":1}'])
            try:
                m._preflight(conn, self.TABLE, "details")
                left = conn.execute(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname LIKE 'pg_temp%' AND p.proname = 'pg_json_is_valid'").fetchone()[0]
            finally:
                self._teardown(conn)
        self.assertEqual(int(left), 0, "تابع موقتِ اعتبارسنجی پاک نشد")


class MigrationHygieneTests(unittest.TestCase):
    """ساختاری، از درس‌های همین فازها (بلعیدن بی‌صدا = بزرگ‌ترین خطر migration)."""

    def test_no_bare_except_or_silent_pass(self):
        src = MIGRATION.read_text(encoding="utf-8")
        lines = src.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("except") and stripped.endswith(":"):
                self.assertNotEqual(lines[i].strip(), "pass",
                                    f"سطر {i + 1}: بلعیدن بی‌صدای استثنا در migration ممنوع")
                self.assertNotEqual(stripped, "except:",
                                    f"سطر {i + 1}: except بدون نوع استثنا")

    def test_sqlite_branch_returns_without_writing(self):
        """
        مسیر SQLite باید «بررسی و return» باشد: لاگِ no-op و هیچ op.execute پیش از
        return. (قفل می‌کند که کسی اشتباهی ALTER را روی SQLite هم اجرایی نکند.)
        """
        import ast
        tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
        upgrade = next(n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
        guard = next((n for n in upgrade.body if isinstance(n, ast.If)), None)
        self.assertIsNotNone(guard, "شاخهٔ بررسی backend در upgrade پیدا نشد")
        test = guard.test
        self.assertIsInstance(test, ast.UnaryOp)
        self.assertIsInstance(getattr(test, "operand", None), ast.Call)
        self.assertEqual(test.operand.func.id, "_is_postgres")
        for node in ast.walk(guard):
            if isinstance(node, ast.Attribute) and node.attr == "execute":
                self.fail("در شاخهٔ SQLite نباید op.execute باشد (no-op بودن را نقض می‌کند)")
        self.assertTrue(any(isinstance(n, ast.Return) for n in ast.walk(guard)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
