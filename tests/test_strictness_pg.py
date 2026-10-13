# -*- coding: utf-8 -*-
"""
تست‌های E.4.10 — سخت‌گیری PostgreSQL، سرتاسری و بدون شکست بی‌صدا.

این فایل **قفل پذیرش فاز E.4** است: هر قابلیتی که در E.4.1 تا E.4.9 ساخته
شد، این‌جا یک‌بار به‌صورت یکپارچه و روی PG واقعی سنجیده می‌شود.

قاعدهٔ صریح مالک
----------------
«اگر PG هست، این فایل نباید skip شود.» پیاده‌سازی:
  • کل ماژول بدون ``PG_DSN`` یک‌جا skip می‌شود (با دلیل روشن و دستور اجرا)؛
  • وقتی PG در دسترس است، ``NoSilentSkipsTests`` همان تست‌های این فایل را
    دوباره اجرا می‌کند و اگر **حتی یک** skip یا خطا ببیند، تست شکست می‌خورد.

پوشش (هر مورد ریشه در یک گام دارد)
-----------------------------------
| موضوع | گام | سنجش این فایل |
|---|---|---|
| ستون‌های BOOLEAN و اتصال سختِ نوع | E.4.1 | `information_schema` + درج واقعی `bool` و رد شدن `int` |
| `insert_returning_id` (RETURNING) | E.4.3 | درج در `logs` و بازخوانی همان `id` |
| DDL فقط از Alembic | E.4.5 | `alembic_version` = `0003` و کامل‌بودن جدول‌ها |
| `executescript` تقسیم‌شده | E.4.7 | اسکریپت دو‌گزاره‌ای روی PG |
| SAVEPOINT و ادامهٔ تراکنش | E.4.8 | خطا در یک ردیف ⇒ برگشت به SAVEPOINT و ادامه |
| `json_extract` مستقل از backend | E.4.9 | خواندن JSON از `logs` با هر دو نوع |
| نگاشت خطاهای مشترک | E.4.4 | 42P01/42703/23505/42701 ⇒ کلاس‌های `core.db` |
| TimeZone قفل‌شده | E.2/E.3 | `SHOW TimeZone` = `UTC` |
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    ENV_PG_DSN,
    ColumnAlreadyExists,
    DatabaseError,
    DatabaseOperationalError,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
    connect_postgres,
)
from core.db.schema import BOOLEAN_COLUMNS, APP_TABLES, present_tables
from tests.migration_graph import migration_head

ROOT = Path(__file__).resolve().parent.parent
LOG_IP = "203.0.113.150"


def _pg_target():
    """``(dsn, None)`` اگر PG در دسترس باشد، وگرنه ``(None, دلیلِ skip)``."""
    dsn = (os.getenv(ENV_PG_DSN, "") or "").strip()
    if not dsn:
        return None, (f"{ENV_PG_DSN} ست نشده است ⇒ کل فایل سخت‌گیری روی PG اجرا نمی‌شود "
                      f"(اجرا با: PG_DSN='host=… dbname=… user=… password=…' pytest "
                      f"tests/test_strictness_pg.py)")
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return None, (f"PG در دسترس نیست ({type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:120]}) ⇒ سخت‌گیری PG رد می‌شود")
    return dsn, None


_PG_DSN, _PG_SKIP = _pg_target()
TEMPLATE_TABLES = ("strictness_t",)


# ─────────────────────────────────────────────────────────────────────
# پایه
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class _PgBase(unittest.TestCase):
    """اتصال PG + جدول موقت + پاک‌سازی (بدون تکیه بر دادهٔ موجود)."""

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.rollback()
            for table in TEMPLATE_TABLES:
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
            self.conn.execute("DELETE FROM logs WHERE printer_ip = ?", (LOG_IP,))
            self.conn.commit()
        except Exception:                     # پاک‌سازی نباید نتیجه را بی‌اعتبار کند
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def _expect_error(self, cls, sql, params=()):
        """اجرای گزاره با انتظار خطا؛ تراکنش پس از خطا rollback می‌شود."""
        try:
            with self.assertRaises(cls) as ctx:
                self.conn.execute(sql, params)
            return ctx.exception
        finally:
            self.conn.rollback()


# ─────────────────────────────────────────────────────────────────────
# ۱) ستون‌های BOOLEAN و اتصال سختِ نوع (E.4.1)
# ─────────────────────────────────────────────────────────────────────
class BooleanStrictnessTests(_PgBase):
    """روی PG ستون‌ها واقعاً BOOLEAN‌اند و ``int`` را نمی‌پذیرند."""

    def test_all_five_columns_are_boolean(self):
        rows = self.conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type = 'boolean' "
            "ORDER BY table_name, column_name").fetchall()
        found = {(t, c) for t, c, _d in rows}
        self.assertEqual(found, set(BOOLEAN_COLUMNS))

    def test_int_is_rejected_by_a_boolean_column(self):
        """``yield_samples.accepted`` مقدار ``int`` را رد می‌کند (اتصال سختِ نوع)."""
        exc = self._expect_error(
            DatabaseError,
            "INSERT INTO yield_samples (printer_ip, color, created_at, accepted) "
            "VALUES (?, ?, ?, ?)",
            (LOG_IP, "black", "2026-10-04T00:00:00.000000Z", 1))
        self.assertEqual(exc.sqlstate, "42804", str(exc))     # datatype_mismatch
        self.assertIn("boolean", str(exc))
        self.assertIs(type(exc), DatabaseError,
                      "42804 عمداً ترجمه نمی‌شود؛ خطای نوع، خطای integrity نیست")

    def test_bool_is_accepted_and_read_back_as_boolean(self):
        """``True/False`` واقعی می‌رود و بولی برمی‌گردد."""
        try:
            self.conn.execute(
                "INSERT INTO yield_samples (printer_ip, color, created_at, accepted) "
                "VALUES (?, ?, ?, ?)",
                (LOG_IP, "black", "2026-10-04T00:00:00.000000Z", True))
            self.conn.commit()
            value = self.conn.execute(
                "SELECT accepted FROM yield_samples WHERE printer_ip = ?",
                (LOG_IP,)).fetchone()[0]
            self.assertIs(value, True)
        finally:
            self.conn.rollback()
            self.conn.execute("DELETE FROM yield_samples WHERE printer_ip = ?", (LOG_IP,))
            self.conn.commit()


# ─────────────────────────────────────────────────────────────────────
# ۲) insert_returning_id (E.4.3)
# ─────────────────────────────────────────────────────────────────────
class InsertReturningIdTests(_PgBase):
    """``lastrowid`` روی PG معنا ندارد؛ ``insert_returning_id`` جای آن است."""

    def test_insert_returns_the_new_id(self):
        new_id = self.conn.insert_returning_id(
            "INSERT INTO logs (printer_ip, timestamp, type, message) VALUES (?, ?, ?, ?)",
            (LOG_IP, "2026-10-04T00:00:00.000000Z", "PRINT", "strictness"))
        self.conn.commit()
        self.assertIsInstance(new_id, int, f"id برگشتی: {new_id!r}")
        fetched = self.conn.execute("SELECT id FROM logs WHERE printer_ip = ?",
                                    (LOG_IP,)).fetchone()[0]
        self.assertEqual(fetched, new_id, "id برگشتی باید همان ردیف درج‌شده باشد")

    def test_id_is_generated_by_identity(self):
        """``logs.id`` روی PG ستون IDENTITY است (E.4.2) و خودش مقدار می‌سازد."""
        kind = self.conn.execute(
            "SELECT is_identity FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'logs' "
            "AND column_name = 'id'").fetchone()[0]
        self.assertEqual(kind, "YES")


# ─────────────────────────────────────────────────────────────────────
# ۳) DDL فقط از Alembic (E.4.5) و TimeZone قفل‌شده (E.2/E.3)
# ─────────────────────────────────────────────────────────────────────
class SchemaAndTimezoneTests(_PgBase):
    """اسکیمای PG ساختهٔ Alembic است و نشست روی UTC قفل شده."""

    def test_revision_is_the_migration_head(self):
        """
        اسکیمای اجراشده باید روی **سرِ واقعی** زنجیره باشد.

        عدد hardcode نبود (پیش‌تر ``0003`` بود و با هر migration تازه می‌شکست):
        سر از فایل‌های ``alembic/versions`` محاسبه می‌شود، پس این قفل همواره
        «مهاجرت‌ها همه اعمال شده‌اند» را می‌سنجد — از 0004_jsonb_columns (E.6.4) هم.
        """
        revision = self.conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        self.assertEqual(revision, migration_head(),
                         "اسکیما روی سرِ migrationها نیست (`alembic upgrade head` runs)")

    def test_all_app_tables_exist(self):
        missing = [t for t in APP_TABLES if t not in present_tables(self.conn)]
        self.assertEqual(missing, [], "اسکیمای Alembic کامل نیست")

    def test_session_timezone_is_utc(self):
        self.assertEqual(self.conn.execute("SHOW TimeZone").fetchone()[0], "UTC")

    def test_timestamp_is_text_and_details_is_jsonb_as_documented(self):
        """
        قرارداد نوع ستون‌ها روی PG:

        • ``timestamp`` هنوز ``text`` است (E.1/E.3 — **نه** TIMESTAMPTZ؛ نشست و
          قالب رشته‌ای در ``docs/timezone-contract.md`` قفل شده‌اند)؛
        • ``details`` از E.6.4 ``jsonb`` است (migration ``0004``) — تنها ستون JSON
          که SQL روی آن محتوا می‌خواند؛ بقیهٔ ستون‌های JSON (``users.allowed_*``،
          ``printer_counters.cartridge_id_state``/``toner_overrides``،
          ``security_events.details``) هنوز ``text``‌اند و در
          ``tests/test_jsonb_columns.py`` قفل شده‌اند.
        """
        rows = dict(self.conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'logs' "
            "AND column_name IN ('timestamp', 'details')").fetchall())
        self.assertEqual(rows, {"timestamp": "text", "details": "jsonb"})


# ─────────────────────────────────────────────────────────────────────
# ۴) executescript تقسیم‌شده (E.4.7)
# ─────────────────────────────────────────────────────────────────────
class ExecutescriptTests(_PgBase):
    """روی PG اسکریپت چندگزاره‌ای اجرا می‌شود (تقسیم به statement)."""

    def test_two_statement_script_creates_both_tables(self):
        self.conn.executescript(
            "CREATE TABLE strictness_t (id integer primary key, name text);"
            "INSERT INTO strictness_t (id, name) VALUES (1, 'a');"
        )
        count = self.conn.execute("SELECT COUNT(*) FROM strictness_t").fetchone()[0]
        self.assertEqual(count, 1)

    def test_multi_command_with_params_is_why_we_split(self):
        """
        دلیل تقسیم: psycopg با پارامتر غیرخالی چند گزاره را نمی‌پذیرد
        (``cannot insert multiple commands into a prepared statement``).
        این تست آن دلیل را زنده نگه می‌دارد.
        """
        exc = self._expect_error(
            DatabaseError,
            "CREATE TABLE strictness_t (id integer); SELECT ?",
            (1,))
        self.assertIn("multiple commands", str(exc).lower(), str(exc))


# ─────────────────────────────────────────────────────────────────────
# ۵) SAVEPOINT در import (E.4.8)
# ─────────────────────────────────────────────────────────────────────
class SavepointContentsTests(_PgBase):
    """روی PG خطای یک ردیف فقط تا SAVEPOINT برمی‌گردد و تراکنش زنده می‌ماند."""

    def test_savepoint_keeps_the_transaction_usable(self):
        self.conn.execute("CREATE TABLE strictness_t (ip text PRIMARY KEY, n integer)")
        self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 1))
        self.conn.commit()

        # ردیف تکراری: بدون SAVEPOINT، کل تراکنش abort می‌شد (25P02).
        self.conn.execute("SAVEPOINT sp_row")
        try:
            self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 2))
            self.fail("درج تکراری باید خطا بدهد")
        except UniqueViolation as exc:
            self.assertEqual(exc.sqlstate, "23505")
            self.conn.execute("ROLLBACK TO SAVEPOINT sp_row")
        finally:
            self.conn.execute("RELEASE SAVEPOINT sp_row")

        self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("b", 3))
        self.conn.commit()
        rows = self.conn.execute("SELECT ip FROM strictness_t ORDER BY ip").fetchall()
        self.assertEqual([r[0] for r in rows], ["a", "b"],
                         "ردیف سالم بعد از خطای ردیف تکراری باید ثبت شود")

    def test_without_savepoint_the_transaction_dies(self):
        """شاهد معکوس: بدون SAVEPOINT همان سناریو تراکنش را از دست می‌دهد."""
        self.conn.execute("CREATE TABLE strictness_t (ip text PRIMARY KEY, n integer)")
        self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 1))
        self.conn.commit()

        try:
            self.conn.execute("INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("a", 2))
        except UniqueViolation:
            pass
        exc = self._expect_error(
            DatabaseError,
            "INSERT INTO strictness_t (ip, n) VALUES (?, ?)", ("b", 3))
        self.assertEqual(exc.sqlstate, "25P02", str(exc))


# ─────────────────────────────────────────────────────────────────────
# ۶) json_extract مستقل از backend (E.4.9)
# ─────────────────────────────────────────────────────────────────────
class JsonExtractStrictnessTests(_PgBase):
    """قطعهٔ JSON روی PG متن و عدد را درست برمی‌گرداند."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO logs (printer_ip, timestamp, type, details) VALUES (?, ?, ?, ?)",
            (LOG_IP, "2026-10-04T00:00:00.000000Z", "PRINT",
             '{"prev_total": 41, "detection": "snmp", "note": "متن"}'))
        self.conn.commit()

    def test_text_and_number_fragments(self):
        text_frag = self.conn.json_extract("details", "detection")
        num_frag = self.conn.json_extract("details", "prev_total", as_number=True)
        self.assertEqual(self.conn.execute(
            f"SELECT {text_frag} FROM logs WHERE printer_ip = ?", (LOG_IP,)).fetchone()[0],
            "snmp")
        self.assertEqual(self.conn.execute(
            f"SELECT {num_frag} FROM logs WHERE printer_ip = ?", (LOG_IP,)).fetchone()[0],
            41)

    def test_invalid_json_is_rejected_at_write_time(self):
        """
        تغییرِ E.6.4: روی PG خطای JSON خراب به **زمان نوشتن** منتقل شد.

        پیش از 0004، ستون ``text`` بود و همین تست یک ردیف بدفرمت می‌ناوخت و
        انتظار داشت ``SELECT`` با خطای ``22P02`` بترکد (یعنی یک ردیف خراب، کوئری
        aggregate‌ی کسی را که هیچ ربطی به آن ردیف ندارد می‌شکست). حالا ``jsonb``
        همان‌جا رد می‌کند که نوشته می‌شود ⇒ نه ``22P02`` در خواندن، و نه دادهٔ
        نیمه‌کاره در جدول. روی SQLite همان رفتار خواندن باقی مانده است
        (``json_extract`` روی رشتهٔ بدفرمت) و در
        ``tests/test_jsonb_columns.py::ReaderToleranceTests`` قفل شده.
        """
        exc = self._expect_error(
            DatabaseError,
            "INSERT INTO logs (printer_ip, timestamp, type, details) VALUES (?, ?, ?, ?)",
            (LOG_IP, "2026-10-04T00:00:01.000000Z", "PRINT", "{not json"))
        self.assertEqual(exc.sqlstate, "22P02", str(exc))
        left = self.conn.execute(
            "SELECT count(*) FROM logs WHERE printer_ip = ? AND details::text LIKE ?",
            (LOG_IP, "{not json%")).fetchone()[0]
        self.assertEqual(left, 0, "ردیفِ ردشده نباید باقی مانده باشد")
        # و همان کوئری خواندن (که پیش از E.6.4 با 22P02 می‌سوخت) حالا بی‌خطا اجرا
        # می‌شود؛ شاهدِ این تست **اجرا شدن** است، نه مقدار شمارش (ردیف‌های تست‌های
        # دیگر همین کلاس در جدول هستند).
        frag = self.conn.json_extract("details", "prev_total", as_number=True)
        counted = self.conn.execute(
            f"SELECT count(*) FROM logs WHERE {frag} IS NOT NULL").fetchone()[0]
        self.assertGreaterEqual(int(counted), 0)


# ─────────────────────────────────────────────────────────────────────
# ۶.۵) ترتیب بی‌توجه به بزرگی/کوچکی نام کاربری (ردیف ۳ سند سخت‌گیری)
# ─────────────────────────────────────────────────────────────────────
class CollationOrderingTests(unittest.TestCase):
    """
    ``list_users`` پیش‌تر ``COLLATE NOCASE`` داشت: نحو SQLite که روی PG با
    ``42704 collation "nocase" does not exist`` می‌شکند — و چون تابع خطا را
    می‌گیرد، «[]» بی‌صدا برمی‌گشت. حالا ``lower(username)`` است (E.4.11a).

    **hermetic بودن اجباری است:** این تست توابع سطح-برنامه (``create_user`` /
    ``list_users``) را صدا می‌زند که backend را از ``DB_BACKEND`` می‌خوانند؛ پس
    هم متغیر محیطی روی PG قفل می‌شود و هم ``cwd`` به یک پوشهٔ موقت می‌رود. بدون
    این دو، یک fallback بی‌صدا به SQLite می‌توانست **``logs.db`` مخزن** را بنویسد
    (یک‌بار همین اتّفاق افتاد؛ فایل با sha256 شاهدِ E.3 بازگردانی شد).
    """

    NAMES = ("zz_alpha", "zz_Bravo", "zz_charlie")

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                  # حتی fallback هم به مخزن نمی‌رسد
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = connect_postgres(_PG_DSN)
        # ترتیب ثبت مهم است: addCleanup برعکس اجرا می‌شود، پس اول بستن اتصال ثبت
        # می‌شود و بعد پاک‌سازی ردیف‌ها — وگرنه پاک‌سازی روی اتصال بسته اجرا
        # می‌شود و بی‌صدا شکست می‌خورد (همین اشتباه یک‌بار رخ داد).
        self.addCleanup(self.conn.close)
        self.addCleanup(self._delete_users)

    def _delete_users(self):
        try:
            self.conn.rollback()
            for name in self.NAMES:
                self.conn.execute("DELETE FROM users WHERE username = ?", (name,))
            self.conn.commit()
        except Exception:
            pass

    def _seed(self):
        from core.database import create_user
        for name in self.NAMES:
            # ``create_user`` هش می‌گیرد (نه رمز خام) — همان قرارداد لایهٔ data.
            user = create_user(username=name, email=f"{name}@strictness.test",
                               password_hash="x" * 32, role="viewer", is_verified=True)
            self.assertIsNotNone(user, f"ساخت کاربر {name} ناموفق بود")
        # قفل ضدِ fallback: ردیف‌ها باید در **همان** PG باشند.
        for name in self.NAMES:
            found = self.conn.execute("SELECT 1 FROM users WHERE username = ?",
                                      (name,)).fetchone()
            self.assertIsNotNone(found, f"کاربر {name} در PG نیست (fallback بی‌صدا؟)")

    def test_ordering_is_case_insensitive_and_not_silently_empty(self):
        from core.database import list_users
        self._seed()
        rows = list_users()
        self.assertTrue(rows, "روی PG نباید بی‌صدا «[]» برگردد (باگ خطای collation)")
        ours = [r["username"] for r in rows if r["username"].startswith("zz_")]
        self.assertEqual(ours, ["zz_alpha", "zz_Bravo", "zz_charlie"],
                         "ترتیب ASCII بی‌توجه به بزرگی/کوچکی (مثل NOCASE)")


# ─────────────────────────────────────────────────────────────────────
# ۷) نگاشت خطاهای مشترک (E.4.4)
# ─────────────────────────────────────────────────────────────────────
class SharedErrorMappingTests(_PgBase):
    """همان جدول نگاشت E.4.4، این‌بار به‌عنوان قفل پذیرش."""

    def test_mapping_table_on_live_pg(self):
        cases = (
            (UndefinedTable, "SELECT * FROM no_such_table_strictness", "42P01"),
            (UndefinedColumn, "SELECT no_such_column FROM logs", "42703"),
            (UniqueViolation,
             "INSERT INTO yield_samples (printer_ip, color, created_at, accepted) "
             "VALUES (NULL, 'black', '2026-10-04T00:00:00.000000Z', true)", "23502"),
            (ColumnAlreadyExists, "ALTER TABLE logs ADD COLUMN printer_ip text", "42701"),
        )
        for cls, sql, sqlstate in cases:
            with self.subTest(sql=sql.split()[0], expected=sqlstate):
                exc = self._expect_error(cls, sql)
                self.assertEqual(exc.sqlstate, sqlstate)
                self.assertEqual(exc.backend, BACKEND_POSTGRESQL)
                self.assertIsNotNone(exc.original)

    def test_all_shared_errors_are_one_hierarchy(self):
        for cls in (UndefinedTable, UndefinedColumn, UniqueViolation,
                    ColumnAlreadyExists, DatabaseOperationalError):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, DatabaseError))


class SkipReasonContractTests(unittest.TestCase):
    """
    قرارداد پیام skip — روی هر دو حالت اجرا معنا دارد (پس gete‌د‌گیر نمی‌شود).

    • بدون PG: پیام باید نام متغیر محیطی و دستور اجرا را روشن بگوید؛
    • با PG: هیچ دلیلی برای skip نباید وجود داشته باشد (``_PG_SKIP is None``).
    """

    def test_either_pg_is_available_or_the_skip_reason_is_actionable(self):
        if _PG_DSN:
            self.assertIsNone(_PG_SKIP, "وقتی PG در دسترس است، دلیلی برای skip نیست")
            return
        self.assertIn(ENV_PG_DSN, _PG_SKIP or "")
        self.assertIn("PG_DSN=", _PG_SKIP or "")


# ─────────────────────────────────────────────────────────────────────
# ۸) بدون شکست بی‌صدا (قاعدهٔ صریح مالک)
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class NoSilentSkipsTests(unittest.TestCase):
    """
    وقتی PG هست، هیچ تستی در این فایل نباید skip شود.

    فهرست کلاس‌ها **صریح** است (نه اسکن ماژول) تا این تست خودش را دوباره اجرا
    نکند و بازگشت بی‌پایان نسازد.
    """

    #: کلاس‌هایی که باید روی PG واقعی اجرا شوند (بدون نگهبان skip).
    REQUIRED_CLASSES = (
        BooleanStrictnessTests,
        InsertReturningIdTests,
        SchemaAndTimezoneTests,
        ExecutescriptTests,
        SavepointContentsTests,
        JsonExtractStrictnessTests,
        CollationOrderingTests,
        SharedErrorMappingTests,
    )

    def test_no_skips_or_errors_when_pg_is_available(self):
        import unittest as _unittest

        import io as _io

        loader = _unittest.TestLoader()
        suite = _unittest.TestSuite(
            loader.loadTestsFromTestCase(cls) for cls in self.REQUIRED_CLASSES)
        output = _io.StringIO()
        result = _unittest.TextTestRunner(verbosity=2, stream=output).run(suite)

        skipped = [str(t) for t, _r in getattr(result, "skipped", [])]
        failures = [str(t) for t, _tb in result.failures]
        errors = [str(t) for t, _tb in result.errors]
        # خروجی اجرای داخلی در پیام شکست می‌آید تا علت بی‌واسطه دیده شود.
        self.assertNotIn("None", "")   # نگه‌داشتن خروجی برای پیام‌ها
        if failures or errors:
            raise AssertionError(
                "اجرای دوباره خطا داد:\n" + output.getvalue()[-4000:])
        self.assertEqual(skipped, [], "با وجود PG هیچ تستی نباید skip شود")
        self.assertEqual(failures, [], "خطای تست در اجرای دوباره")
        self.assertEqual(errors, [], "استثنا در اجرای دوباره")
        self.assertGreater(result.testsRun, 0, "هیچ تستی اجرا نشد؟")

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
