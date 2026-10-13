# -*- coding: utf-8 -*-
"""
تست‌های E.5 — ``upsert_sql`` و سه محلی که از ``INSERT OR REPLACE`` منتقل شدند.

چرا این فایل هست
----------------
``INSERT OR REPLACE`` نحو SQLite است؛ روی PostgreSQL با
``syntax error at or near "OR"`` (42601) می‌شکند. در نخستین سنجش E.5 (کل سوئیت با
``DB_BACKEND=postgresql``) ۲۰ شکست فقط از همین یک الگو بود. هِلپر
``core.db.upsert_sql`` جمله را backend-aware می‌سازد:

  • SQLite — ``INSERT OR REPLACE INTO t (…) VALUES (…)``  ⇒ بایت‌به‌بایت همان امروز؛
  • PG — ``INSERT … ON CONFLICT (کلید) DO UPDATE SET c = EXCLUDED.c …``.

این ترجمه **فقط** وقتی معنای داده را عوض نمی‌کند که جمله *همهٔ* ستون‌های جدول را
ببنویسد (``docs/insert-or-replace-inventory.md`` §۳.۱): SQLite با REPLACE ردیف را
حذف و دوباره می‌سازد (پس ستون نوشته‌نشده به پیش‌فرض برمی‌گردد) و PG درجا UPDATE
می‌کند (ستون نوشته‌نشده ارزش قبلی را نگه می‌دارد). پس بخشی از همین فایل، قفل
هم‌خوانی «فهرست ستون‌های درج == همهٔ ستون‌های جدول» است — اگر روزی ستونی به جدول
اضافه شود، این تست می‌شکند و یادآوری می‌کند که آن جمله دیگر «pernِ کامل» نیست.

دو رویه که سنجیده می‌شوند: **درج جدید**، **تعارض ⇒ به‌روزرسانی**، **idempotent**
(اجرای دوباره ⇒ همان نتیجه)، و **حفظ ستون‌های دیگر** وقتی واقعاً نوشته می‌شوند.
"""
import ast
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_PG_DSN,
    DatabaseError,
    connect_postgres,
    connect_sqlite,
    upsert_sql,
)
from core.db.schema import schema_columns

ROOT = Path(__file__).resolve().parent.parent
IP = "203.0.113.160"


def _pg_target():
    """``(dsn, None)`` اگر PG در دسترس باشد، وگرنه ``(None, دلیلِ skip)``."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ تست‌های PGِ زنده‌ی این فایل اجرا نمی‌شوند "
                      f"(اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ تست‌های PG رد می‌شوند")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()


def _pin_sqlite(test):
    """
    محیطِ یک تستِ SQLite-محور را قفل می‌کند.

    چرا لازم است: ``init_db()`` و ``ensure_yield_tables()`` بک‌اند را از محیط
    می‌خوانند؛ اگر سوئیت با ``DB_BACKEND=postgresql`` اجرا شود و این قفل نباشد،
    جدول‌ها روی PG ساخته می‌شوند و فایل موقت خالی می‌ماند ⇒ «no such table»
    (همین اتّفاق در گام E.5 افتاد). قفل PG_DSN هم مانع از آن می‌شود که تصادفی
    روی DB مخزن نوشته شود.
    """
    env = mock.patch.dict(os.environ, {"DB_BACKEND": BACKEND_SQLITE, ENV_PG_DSN: ""})
    env.start()
    test.addCleanup(env.stop)
    from core.db import resolve_backend
    assert resolve_backend() == BACKEND_SQLITE


# ─────────────────────────────────────────────────────────────────────
# A) خودِ هِلپر (بدون دیتابیس)
# ─────────────────────────────────────────────────────────────────────
class HelperFragmentTests(unittest.TestCase):

    def test_sqlite_keeps_the_historic_statement(self):
        sql = upsert_sql("t", ("a", "b"), ("a",), backend=BACKEND_SQLITE)
        self.assertEqual(sql, 'INSERT OR REPLACE INTO "t" ("a", "b") VALUES (?, ?)')

    def test_postgres_targets_the_conflict_key(self):
        sql = upsert_sql("t", ("a", "b"), ("a",), backend=BACKEND_POSTGRESQL)
        self.assertEqual(
            sql,
            'INSERT INTO "t" ("a", "b") VALUES (?, ?) '
            'ON CONFLICT ("a") DO UPDATE SET "b" = EXCLUDED."b"')

    def test_conflict_columns_are_excluded_from_the_update_list(self):
        sql = upsert_sql("t", ("ip", "color", "n"), ("ip", "color"), backend=BACKEND_POSTGRESQL)
        self.assertIn('ON CONFLICT ("ip", "color")', sql)
        self.assertEqual(sql.count("= EXCLUDED."), 1, sql)
        self.assertIn('"n" = EXCLUDED."n"', sql)
        for key in ("ip", "color"):
            self.assertNotIn(f'"{key}" = EXCLUDED.', sql, f"کلید نباید در SET بیاید: {sql}")

    def test_no_updatable_column_falls_back_to_do_nothing(self):
        """اگر همهٔ ستون‌ها کلید باشند، ``DO UPDATE SET`` خالی نحوی نامعتبر است."""
        sql = upsert_sql("t", ("a",), ("a",), backend=BACKEND_POSTGRESQL)
        self.assertTrue(sql.endswith("DO NOTHING"), sql)

    def test_explicit_values_sql_is_passed_through(self):
        """مقدار ثابت در VALUES (مثل ``'high'``) حفظ می‌شود و تعداد جای‌نما چک نمی‌شود."""
        sql = upsert_sql("t", ("a", "b", "c"), ("a",), values_sql="?, 'x', ?",
                         backend=BACKEND_POSTGRESQL)
        self.assertIn("VALUES (?, 'x', ?)", sql)
        self.assertIn('"c" = EXCLUDED."c"', sql)

    def test_validation_is_strict(self):
        for kwargs in (
            {"table": "t", "columns": (), "conflict_columns": ("a",)},
            {"table": "t", "columns": ("a",), "conflict_columns": ()},
            {"table": "t", "columns": ("a", "b"), "conflict_columns": ("zz",)},
        ):
            with self.subTest(**{k: v for k, v in kwargs.items() if k != "table"}):
                with self.assertRaises(ValueError):
                    upsert_sql(**kwargs)

    def test_backend_default_follows_env(self):
        with mock.patch.dict(os.environ, {"DB_BACKEND": "postgresql"}):
            self.assertIn("ON CONFLICT", upsert_sql("t", ("a", "b"), ("a",)))
        with mock.patch.dict(os.environ, {"DB_BACKEND": "sqlite"}):
            self.assertIn("INSERT OR REPLACE", upsert_sql("t", ("a", "b"), ("a",)))


# ─────────────────────────────────────────────────────────────────────
# B) قفل‌های ساختاری روی کد
# ─────────────────────────────────────────────────────────────────────
class SourceContractTests(unittest.TestCase):
    """هیچ ``INSERT OR REPLACE`` دستی در کد برنامه نماند (تصمیم E.5)."""

    #: تنها جاهای مجاز برای متن «INSERT OR REPLACE» در core/ و web/ — آگاهانه و
    #: مستند. اگر سومی اضافه شود، این تست می‌شکند.
    #: (در E.6.5 سومِ پیشین — کد مردهٔ TD-001 در enhanced_collector — حذف شد، پس
    #:  allow-list هم کوتاه شد؛ شاهدِ «حذف‌شدن» در تست پایین همین فایل است.)
    ALLOWED_SITES = {
        # خودِ هِلپر: جملهٔ SQLite را همین‌جا می‌سازد.
        "core/db/upsert.py": "سازندهٔ جمله",
        # حالت replace در import: فقط برای مقصد SQLite ساخته می‌شود و مسیر PG
        # پیش از آن با گارد یکپارچه بسته شده (E.4.8b) و در E.6.1 بازنشسته شد.
        "web/routes/import_db.py": "جملهٔ پویا، زیر گارد PG",
    }

    def _docstring_linenos(self, source):
        """خطوط docstring (نه کد) تا آن‌ها در شمارش نیایند."""
        lines = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc = body[0].value
                    lines.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
        return lines

    def test_only_the_allowed_sites_mention_insert_or_replace(self):
        """
        قاعدهٔ E.5: کد برنامه جملهٔ ``INSERT OR REPLACE`` دستی نمی‌نویسد، جز سه
        محلی که در ``ALLOWED_SITES`` با دلیل ثبت شده‌اند. شمارش با AST است تا
        docstringها و کامنتها به‌عنوان «کد» شمرده نشوند.
        """
        found = {}
        for root in ("core", "web"):
            for path in sorted((ROOT / root).rglob("*.py")):
                rel = path.relative_to(ROOT).as_posix()
                source = path.read_text(encoding="utf-8")
                doc_lines = self._docstring_linenos(source)
                for lineno, line in enumerate(source.splitlines(), start=1):
                    if lineno in doc_lines:
                        continue
                    code = line.split("#", 1)[0]
                    if re.search(r"INSERT\s+OR\s+REPLACE", code, re.I):
                        found.setdefault(rel, []).append(lineno)
        self.assertEqual(sorted(found), sorted(self.ALLOWED_SITES),
                         f"محل‌های غیرمنتظره: {found}")

    def test_the_untouched_sites_are_still_documented(self):
        """
        یک محل آگاهانه دست‌نخورده (fixtureهای golden، تصمیم E.5)، یک محل زیر گارد
        (import_db، تصمیم E.6.1: بازنشستگی) و یک محل **حذف‌شده** (TD-001 در E.6.5).
        اگر روزی کسی این وضعیت را بی‌مقدمه عوض کند، این تست می‌پرسد چرا.
        """
        dead = (ROOT / "core" / "enhanced_collector.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT OR REPLACE", dead,
                         "TD-001 در E.6.5 حذف شد؛ بازگشتش باید با دلیل و کامیت جدا باشد")
        self.assertNotIn("_save_counters_to_db", dead, "تابع مرده نباید احیا شود")
        harness = (ROOT / "tests" / "golden_harness.py").read_text(encoding="utf-8")
        self.assertIn("INSERT OR REPLACE", harness, "fixtureهای SQLite golden دست‌نخورده‌اند")
        route = (ROOT / "web" / "routes" / "import_db.py").read_text(encoding="utf-8")
        self.assertIn("_reject_import_on_postgres", route, "import روی PG زیر گارد است")
        self.assertNotIn("upsert_sql", route)

    def test_three_live_sites_call_the_helper(self):
        """سه محل زنده باید از هِلپر صدا بزنند (نه رشتهٔ SQL دستی)."""
        expected = {"core/database.py": 1, "core/yield_engine.py": 2}
        for rel, count in expected.items():
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_upsert_sql"
            ]
            self.assertEqual(len(calls), count,
                             f"{rel}: انتظار {count} فراخوانی upsert_sql داشتیم")


# ─────────────────────────────────────────────────────────────────────
# C) قفل «همهٔ ستون‌ها» — شرط درستیِ ترجمه
# ─────────────────────────────────────────────────────────────────────
class ColumnCompletenessTests(unittest.TestCase):
    """فهرست‌های ستونِ سه محل منتقل‌شده باید همان «همهٔ ستون‌های جدول» بمانند."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                 # DB_PATH نسبی ⇒ DB موقت
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        _pin_sqlite(self)                      # init_db() باید همین فایل را ببیند
        import core.database as database
        import core.yield_engine as yield_engine
        database.init_db()
        yield_engine.ensure_yield_tables()
        self.conn = connect_sqlite(os.path.join(self.tmp.name, "logs.db"))
        self.addCleanup(self.conn.close)

    def test_printer_counters_columns_are_complete(self):
        import core.database as database
        table_cols = schema_columns(self.conn, "printer_counters")
        # ترتیب *اعلام* جدول شرط نیست (toner_overrides با ALTER آخر اضافه شده)، ولی
        # «پوشش همهٔ ستون‌ها» شرط است — وگرنه ترجمه معنای REPLACE را عوض می‌کند.
        self.assertEqual(sorted(database._COUNTERS_UPSERT_COLUMNS), sorted(table_cols),
                         "فهرست درج باید همهٔ ستون‌های جدول را بنویسد (شرط §۳.۱)")
        # همان فهرستی که خواننده می‌سازد، بدون «ip» ⇒ باید زیرمجموعه باشد:
        read_cols = [c.strip() for c in database._COUNTERS_COLS.split(",")]
        self.assertEqual(sorted(read_cols), sorted(t for t in table_cols if t != "ip"))

    def test_cartridge_state_columns_are_complete(self):
        import core.yield_engine as yield_engine
        self.assertEqual(list(yield_engine._STATE_UPSERT_COLUMNS),
                         schema_columns(self.conn, "cartridge_state"))

    def test_cartridge_yield_profiles_columns_are_complete(self):
        import core.yield_engine as yield_engine
        self.assertEqual(list(yield_engine._PROFILE_UPSERT_COLUMNS),
                         schema_columns(self.conn, "cartridge_yield_profiles"))

    def test_upsert_param_counts_match_the_columns(self):
        """هر جای‌نمای ``VALUES`` باید یک پارامتر داشته باشد (به‌جز مقادیر ثابت)."""
        import core.database as database
        import core.yield_engine as yield_engine
        state_sql = upsert_sql("cartridge_state", yield_engine._STATE_UPSERT_COLUMNS,
                               ("printer_ip", "color"), backend=BACKEND_SQLITE)
        self.assertEqual(state_sql.count("?"), len(yield_engine._STATE_UPSERT_COLUMNS))
        prof_sql = upsert_sql("cartridge_yield_profiles", yield_engine._PROFILE_UPSERT_COLUMNS,
                               ("cartridge_key",), values_sql=yield_engine._PROFILE_UPSERT_VALUES,
                               backend=BACKEND_SQLITE)
        self.assertEqual(prof_sql.count("?"), 9, "۹ جای‌نما + یک مقدار ثابت 'high'")
        counters_sql = upsert_sql("printer_counters", database._COUNTERS_UPSERT_COLUMNS,
                                  ("ip",), backend=BACKEND_SQLITE)
        self.assertEqual(counters_sql.count("?"), len(database._COUNTERS_UPSERT_COLUMNS))


# ─────────────────────────────────────────────────────────────────────
# D) رفتار روی هر دو backend — پایهٔ مشترک
# ─────────────────────────────────────────────────────────────────────
class _UpsertBehaviourMixin:
    """درج جدید / تعارض ⇒ به‌روزرسانی / idempotent، روی اتصالِ ``self.conn``."""

    def _state(self, **over):
        state = {
            "printer_ip": IP, "color": "black", "printer_model": "M-1",
            "cartridge_name": "C-1", "cartridge_key": f"{IP}:M-1:C-1",
            "current_level": 55, "last_valid_level": 60, "last_counter": 1000,
            "anchor_counter": 900, "anchor_level": 70,
            "anchor_timestamp": "2026-10-04T00:00:00.000000Z",
            "yield_per_page": 1800, "yield_source": "auto_learn", "confidence": "high",
            "sample_count": 3, "total_weight": 2.5,
            "learned_at": "2026-10-04T00:00:00.000000Z", "last_refill_at": None,
            "last_refill_counter": None, "pending_refill_level": None,
            "pending_refill_counter": None, "pending_refill_timestamp": None,
            "cycle_start_counter": 800, "cycle_start_level": 80,
            "cycle_start_timestamp": "2026-10-04T00:00:00.000000Z",
            "zero_reached_counter": None, "zero_reached_timestamp": None,
            "pages_after_zero": 0, "cycle_status": "active", "force_estimate": 0,
            "updated_at": "2026-10-04T00:00:00.000000Z",
        }
        state.update(over)
        return state

    def test_fresh_insert_then_conflict_updates(self):
        import core.yield_engine as yield_engine
        self._delete_rows()

        yield_engine._upsert_state(self.conn, self._state(current_level=55))
        self.conn.commit()
        row = yield_engine._load_state(self.conn, IP, "black")
        self.assertIsNotNone(row, "ردیف تازه باید درج شود")
        self.assertEqual(row["current_level"], 55)

        # همان کلید (printer_ip, color) ⇒ باید **به‌روزرسانی** شود، نه ردیف دوم.
        yield_engine._upsert_state(self.conn, self._state(current_level=33, sample_count=4))
        self.conn.commit()
        row2 = yield_engine._load_state(self.conn, IP, "black")
        self.assertEqual(row2["current_level"], 33)
        self.assertEqual(row2["sample_count"], 4)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM cartridge_state WHERE printer_ip = ?", (IP,)).fetchone()[0]
        self.assertEqual(count, 1, "REPLACE/upsert باید یک ردیف نگه دارد")

    def test_upsert_is_idempotent(self):
        """اجرای دوباره با همان داده ⇒ همان وضعیت (نه خطا، نه ردیف اضافه)."""
        import core.yield_engine as yield_engine
        self._delete_rows()
        state = self._state()
        for _ in range(3):
            yield_engine._upsert_state(self.conn, state)
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT current_level, sample_count, updated_at FROM cartridge_state "
            "WHERE printer_ip = ?", (IP,)).fetchall()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0], (55, 3, "2026-10-04T00:00:00.000000Z"))

    def test_profile_upsert_replaces_instead_of_accumulating(self):
        """
        ``_upsert_profile_if_high`` با همان ``cartridge_key`` باید profile را
        جایگزین کند (منطق max/میانگین وزنی داخل تابع است؛ اینجا فقط «یک ردیف»
        و «مقدار تازه» سنجیده می‌شود).
        """
        import core.yield_engine as yield_engine
        self._delete_rows()
        key = f"{IP}:M-1:C-1"
        self.conn.execute("DELETE FROM cartridge_yield_profiles WHERE cartridge_key = ?", (key,))
        self.conn.commit()

        yield_engine._upsert_profile_if_high(self.conn, self._state(confidence="high",
                                                                    yield_per_page=1500))
        self.conn.commit()
        prof = yield_engine._load_profile(self.conn, key)
        self.assertIsNotNone(prof)
        self.assertEqual(prof["yield_per_page"], 1500)
        self.assertEqual(prof["confidence"], "high")

        yield_engine._upsert_profile_if_high(self.conn, self._state(confidence="high",
                                                                    yield_per_page=1700))
        self.conn.commit()
        prof2 = yield_engine._load_profile(self.conn, key)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM cartridge_yield_profiles WHERE cartridge_key = ?",
            (key,)).fetchone()[0]
        self.assertEqual(rows, 1, "تعارض باید به جایگزینی ختم شود، نه ردیف دوم")
        self.assertEqual(prof2["yield_per_page"], 1700)

    def _delete_rows(self):
        """پاک‌سازی ردیف‌های تست (کلید profile ``source_printer_ip`` است، نه printer_ip)."""
        self.conn.execute("DELETE FROM cartridge_state WHERE printer_ip = ?", (IP,))
        self.conn.execute("DELETE FROM cartridge_yield_profiles WHERE source_printer_ip = ?", (IP,))
        self.conn.commit()


class SqliteUpsertBehaviourTests(_UpsertBehaviourMixin, unittest.TestCase):
    """روی SQLite: رفتار عیناً همان قبل (REPLACE).

    محیط عمداً روی SQLite قفل می‌شود: ``init_db()`` و ``ensure_yield_tables()``
    backend را از محیط می‌خوانند، پس اگر سوئیت با ``DB_BACKEND=postgresql`` اجرا
    شود و اینجا قفل نباشد، جدول‌ها روی PG ساخته می‌شوند و فایل موقت خالی می‌ماند
    («no such table: cartridge_state» — اتّفاق واقعی همین گام).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        _pin_sqlite(self)
        import core.database as database
        import core.yield_engine as yield_engine
        database.init_db()
        yield_engine.ensure_yield_tables()
        self.conn = connect_sqlite(os.path.join(self.tmp.name, "logs.db"))
        self.addCleanup(self.conn.close)


@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresUpsertBehaviourTests(_UpsertBehaviourMixin, unittest.TestCase):
    """روی PG واقعی: همان چهار سنجش، از راه ``ON CONFLICT DO UPDATE``."""

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        # این کلاس توابع سطح‌برنامه را هم صدا می‌زند (save_printer_counters،
        # ensure_printer_counters_columns) و آن‌ها backend را از محیط می‌خوانند.
        # بدون قفل DB_BACKEND، آن فراخوانی‌ها بی‌صدا روی SQLite می‌روند و **DB
        # مخزن** را می‌نویسند — یک‌بار همین اتّفاق افتاد و logs.db با بازتولید
        # مهاجرت E.3.5 بایت‌به‌بایت بازگردانی شد. پس: env + cwd موقت + تأیید.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        from core.db import resolve_backend
        self.assertEqual(resolve_backend(), BACKEND_POSTGRESQL,
                         "محیط PG قفل نشد — خطر نوشتن روی DB مخزن")
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self._cleanup)
        self.addCleanup(self.conn.close)

    def _cleanup(self):
        try:
            self.conn.rollback()
            self._delete_rows()
            self.conn.execute("DELETE FROM printer_counters WHERE ip = ?", (IP,))
            self.conn.commit()
        except Exception:
            pass

    def test_helper_sql_is_what_pg_accepts(self):
        """شاهد دوتایی: جملهٔ helper اجرا می‌شود و جملهٔ قدیمی همان‌جا 42601 می‌دهد."""
        import core.yield_engine as yield_engine
        new_sql = upsert_sql("cartridge_state", yield_engine._STATE_UPSERT_COLUMNS,
                             ("printer_ip", "color"), backend=BACKEND_POSTGRESQL)
        old_sql = new_sql.replace(
            'INSERT INTO "cartridge_state"', 'INSERT OR REPLACE INTO "cartridge_state"', 1)
        old_sql = old_sql.split(" ON CONFLICT", 1)[0]
        # پارامترها پر می‌شوند تا خطا از مفسر PG بیاید، نه از بررسی تعداد
        # جای‌نما در خود adapter (آن بررسی جلوتر از ارسال انجام می‌شود).
        params = (IP, "black") + (None,) * (len(yield_engine._STATE_UPSERT_COLUMNS) - 2)
        with self.assertRaises(DatabaseError) as ctx:
            self.conn.execute(old_sql, params)
        self.assertEqual(ctx.exception.sqlstate, "42601", str(ctx.exception))
        self.conn.rollback()
        self.assertIn("ON CONFLICT", new_sql)
        self.assertIn("DO UPDATE SET", new_sql)

    def test_save_printer_counters_round_trip_on_pg(self):
        """``save_printer_counters`` / ``load_printer_counters`` روی PG زنده (محل ۱)."""
        import core.database as database
        self.conn.execute("DELETE FROM printer_counters WHERE ip = ?", (IP,))
        self.conn.commit()
        data = {"print_total": 120, "toner_level": 80, "device_type": "toshiba",
                "updated_at": "2026-10-04T00:00:00.000000Z"}
        database.save_printer_counters(IP, data)
        loaded = database.load_printer_counters(IP)
        self.assertIsNotNone(loaded, "پس از درج باید ردیف خوانده شود")
        self.assertEqual(loaded["print_total"], 120)

        data["print_total"] = 240
        database.save_printer_counters(IP, data)          # تعارض ⇒ به‌روزرسانی
        loaded2 = database.load_printer_counters(IP)
        self.assertEqual(loaded2["print_total"], 240)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM printer_counters WHERE ip = ?", (IP,)).fetchone()[0]
        self.assertEqual(rows, 1)
        # اجرای دوم نباید چیزی در log خطا بگذارد (ensure_* روی PG بی‌عمل است)
        with mock.patch("core.database.log") as lg:
            database.ensure_printer_counters_columns()
        lg.exception.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
