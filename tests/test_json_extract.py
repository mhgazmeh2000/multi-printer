# -*- coding: utf-8 -*-
"""
تست‌های E.4.9 — ``json_extract`` مستقل از backend.

مسئله
-----
``json_extract(col, '$.key')`` تابع SQLite است (افزونهٔ JSON1) و روی PostgreSQL
وجود ندارد: ``function json_extract(text, unknown) does not exist``
(سنجیده‌شده در E.4.6 — مسیر ``/metrics`` روی PG همین‌جا می‌شکست). معادل PostgreSQL
عملگر ``->>`` است، با یک تفاوت مهم: ``->>`` همیشه **متن** می‌دهد، در حالی که
``json_extract`` SQLite مقدار JSON را با نوع خودش (عدد ⇒ INTEGER) برمی‌گرداند.

راه‌حل (E.4.9): هِلپر ``ConnectionAdapter.json_extract(column, key, *, as_number)``
قطعهٔ SQL را می‌سازد — روی SQLite بایت‌به‌بایت همان ``json_extract`` امروز، و روی PG
``->>`` (و در صورت نیاز ``::numeric`` برای مقایسهٔ عددی).

چهار چیزی که این فایل قفل می‌کند
--------------------------------
۱) شکل قطعهٔ SQL روی هر دو backend (و نقل‌قول‌شدن نام ستون)؛
۲) اعتبارسنجی کلید — این هِلپر متن SQL می‌سازد، پس کلید نامعتبر باید ``ValueError``
   بدهد (مرز امنیتی)؛
۳) رفتار واقعی مسیرهای dedupe در ``core.database.add_event`` روی SQLite
   (CARTRIDGE_CHANGED / PRINT / COUNTER_RESET) — یعنی مهاجرت، رفتار را عوض نکرده؛
۴) هیچ متن ``json_extract(`` در ``core/`` و ``web/`` نماند (جز خود هِلپر) — قفل رگرسیون.
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import (
    BACKEND_POSTGRESQL,
    ENV_PG_DSN,
    ConnectionAdapter,
    connect_postgres,
    connect_sqlite,
)

ROOT = Path(__file__).resolve().parent.parent
IP = "203.0.113.99"


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


class _StubRaw:
    """اتصال ساختگی: فقط برای ساختن adapter با backend مشخص (بدون اجرای SQL)."""

    def execute(self, sql, params=()):  # pragma: no cover - اجرا نمی‌شود
        raise AssertionError("این استاب نباید SQL اجرا کند")


# ─────────────────────────────────────────────────────────────────────
# A) شکل قطعهٔ SQL روی هر دو backend
# ─────────────────────────────────────────────────────────────────────
class FragmentTests(unittest.TestCase):
    """قطعهٔ SQL باید روی هر backend نحو معتبر و قابل انتظار بدهد."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sqlite = connect_sqlite(os.path.join(self.tmp.name, "frag.db"))
        self.addCleanup(self.sqlite.close)
        self.pg = ConnectionAdapter(_StubRaw(), backend=BACKEND_POSTGRESQL)

    def test_sqlite_fragment_is_the_historic_expression(self):
        """روی SQLite همان ``json_extract`` امروز می‌ماند (فقط ستون نقل‌قول شده)."""
        self.assertEqual(self.sqlite.json_extract("details", "event_id"),
                         "json_extract(\"details\", '$.event_id')")
        self.assertEqual(self.sqlite.json_extract("details", "prev_total", as_number=True),
                         "json_extract(\"details\", '$.prev_total')",
                         "روی SQLite نوع از قبل درست است ⇒ as_number چیزی اضافه نمی‌کند")

    def test_postgres_fragment_uses_arrow_operator(self):
        """روی PG عملگر ``->>`` (و در صورت نیاز cast عددی)."""
        self.assertEqual(self.pg.json_extract("details", "event_id"),
                         "(\"details\"::jsonb)->>'event_id'")
        self.assertEqual(self.pg.json_extract("details", "prev_total", as_number=True),
                         "((\"details\"::jsonb)->>'prev_total')::numeric")

    def test_column_name_is_quoted(self):
        """نام ستون نقل‌قول می‌شود (نحو استاندارد؛ همان قاعدهٔ ``quote_ident``)."""
        self.assertEqual(self.pg.json_extract("weird name", "k"),
                         '("weird name"::jsonb)->>\'k\'')
        self.assertEqual(self.pg.json_extract('has"quote', "k"),
                         '("has""quote"::jsonb)->>\'k\'')

    def test_invalid_key_is_rejected(self):
        """کلید نامعتبر ⇒ ``ValueError`` (هِلپر متن SQL می‌سازد؛ مرز امنیتی)."""
        bad_keys = ("a.b", "x' OR 1=1 --", "x; DROP TABLE logs", "", "1abc",
                    "با فاصله", None, 3, "k)", "$.k")
        for key in bad_keys:
            for adapter in (self.sqlite, self.pg):
                with self.subTest(key=key, backend=adapter.backend):
                    with self.assertRaises(ValueError):
                        adapter.json_extract("details", key)

    def test_fragment_runs_on_a_real_sqlite_table(self):
        """قطعهٔ ساخته‌شده روی SQLite واقعی کار می‌کند (متن و عدد)."""
        self.sqlite.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, details TEXT)")
        self.sqlite.execute("INSERT INTO t (details) VALUES (?)",
                            (json.dumps({"event_id": "e-1", "prev_total": 12}),))
        text_frag = self.sqlite.json_extract("details", "event_id")
        num_frag = self.sqlite.json_extract("details", "prev_total", as_number=True)
        self.assertEqual(
            self.sqlite.execute(f"SELECT 1 FROM t WHERE {text_frag} = ?", ("e-1",)).fetchone(),
            (1,))
        self.assertEqual(
            self.sqlite.execute(f"SELECT 1 FROM t WHERE {num_frag} = ?", (12,)).fetchone(),
            (1,))
        self.assertIsNone(
            self.sqlite.execute(f"SELECT 1 FROM t WHERE {num_frag} = ?", (13,)).fetchone())


# ─────────────────────────────────────────────────────────────────────
# B) رفتار dedupe روی SQLite (مهاجرت، منطق دامنه را عوض نکرده)
# ─────────────────────────────────────────────────────────────────────
class SqliteDedupeBehaviourTests(unittest.TestCase):
    """سه مسیر dedupe که از این عبارت استفاده می‌کردند، روی SQLite واقعی."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                 # DB_PATH نسبی ⇒ DB موقت
        self.addCleanup(self._restore)
        from core.database import init_db
        init_db()

    def _restore(self):
        os.chdir(self.old_cwd)

    def _rows(self, etype):
        from core.database import db_connection
        with db_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM logs WHERE printer_ip = ? AND type = ?",
                (IP, etype)).fetchone()[0]

    def test_cartridge_changed_same_event_id_is_skipped(self):
        """``event_id`` یکسان ⇒ رویداد دوم ثبت نمی‌شود (کلید متن)."""
        from core.database import add_event
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "ev-1", "color": "black"})
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "ev-1", "color": "black"})
        self.assertEqual(self._rows("CARTRIDGE_CHANGED"), 1)
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "ev-2", "color": "black"})
        self.assertEqual(self._rows("CARTRIDGE_CHANGED"), 2,
                         "رویداد با event_id دیگر باید ثبت شود")

    def test_print_duplicate_transition_is_skipped(self):
        """``PRINT`` با همان prev/current ⇒ رکورد دوم ثبت نمی‌شود (مقایسهٔ عددی)."""
        from core.database import add_event
        add_event(IP, "PRINT", {"prev_total": 100, "current_total": 105})
        add_event(IP, "PRINT", {"prev_total": 100, "current_total": 105})
        self.assertEqual(self._rows("PRINT"), 1)
        add_event(IP, "PRINT", {"prev_total": 105, "current_total": 110})
        self.assertEqual(self._rows("PRINT"), 2)

    def test_counter_reset_duplicate_transition_is_skipped(self):
        """``COUNTER_RESET`` با همان prev/current ⇒ رکورد دوم ثبت نمی‌شود."""
        from core.database import add_event
        add_event(IP, "COUNTER_RESET", {"prev_total": 5000, "current_total": 3})
        add_event(IP, "COUNTER_RESET", {"prev_total": 5000, "current_total": 3})
        self.assertEqual(self._rows("COUNTER_RESET"), 1)
        add_event(IP, "COUNTER_RESET", {"prev_total": 5000, "current_total": 9})
        self.assertEqual(self._rows("COUNTER_RESET"), 2)


# ─────────────────────────────────────────────────────────────────────
# C) قفل رگرسیون روی متن کد
# ─────────────────────────────────────────────────────────────────────
class SourceGuardTests(unittest.TestCase):
    """پس از E.4.9 هیچ متن ``json_extract(`` در کد برنامه نمی‌ماند."""

    def test_no_inline_json_extract_in_app_code(self):
        offenders = []
        for root in ("core", "web"):
            for path in sorted((ROOT / root).rglob("*.py")):
                if path.relative_to(ROOT).as_posix() == "core/db/connection.py":
                    continue                        # خود هِلپر (و docstringهایش)
                for lineno, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), start=1):
                    code = line.split("#", 1)[0]     # کامنت‌ها شمرده نمی‌شوند
                    # ``conn.json_extract(...)`` درست است؛ آنچه ممنوع است متن SQL
                    # دستی است (``json_extract(`` بدون نقطه در ابتدای فراخوانی).
                    if re.search(r"(?<![\.\w])json_extract\(", code):
                        offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "باید از conn.json_extract(...) استفاده شود، نه متن SQL دستی")

    def test_helper_is_used_by_both_consumers(self):
        """دو مصرف‌کننده (core/database.py و web/routes/metrics.py) از هِلپر استفاده می‌کنند."""
        for rel, minimum in (("core/database.py", 5), ("web/routes/metrics.py", 1)):
            source = (ROOT / rel).read_text(encoding="utf-8")
            self.assertGreaterEqual(source.count("conn.json_extract("), minimum, rel)


# ─────────────────────────────────────────────────────────────────────
# D) PostgreSQL واقعی
# ─────────────────────────────────────────────────────────────────────
@unittest.skipUnless(_PG_DSN, _PG_SKIP or "")
class PostgresLiveJsonExtractTests(unittest.TestCase):
    """روی PG ۱۶ واقعی: قطعهٔ ``->>`` کار می‌کند و مسیرهای dedupe/`metrics` سالم‌اند."""

    def setUp(self):
        if not _PG_DSN:
            self.skipTest(_PG_SKIP or "PG در دسترس نیست")
        self.env = mock.patch.dict(os.environ, {
            "DB_BACKEND": "postgresql", ENV_PG_DSN: _PG_DSN})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = connect_postgres(_PG_DSN)
        self.addCleanup(self.conn.close)

    def _cleanup_logs(self):
        self.conn.rollback()
        self.conn.execute("DELETE FROM logs WHERE printer_ip = ?", (IP,))
        self.conn.commit()

    def test_fragment_queries_the_real_logs_table(self):
        """همان قطعه‌ها روی جدول واقعی ``logs`` (متن + عدد) جواب می‌دهند."""
        from core.database import add_event
        from core.database import db_connection
        self._cleanup_logs()
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "pg-ev-1", "color": "black"})
        add_event(IP, "PRINT", {"prev_total": 100, "current_total": 105})

        with db_connection() as conn:
            text_frag = conn.json_extract("details", "event_id")
            num_frag = conn.json_extract("details", "prev_total", as_number=True)
            hit = conn.execute(
                f"SELECT COUNT(*) FROM logs WHERE printer_ip = ? AND {text_frag} = ?",
                (IP, "pg-ev-1")).fetchone()[0]
            num_hit = conn.execute(
                f"SELECT COUNT(*) FROM logs WHERE printer_ip = ? AND {num_frag} = ?",
                (IP, 100)).fetchone()[0]
        self.assertEqual(hit, 1)
        self.assertEqual(num_hit, 1)
        self._cleanup_logs()

    def test_dedupe_is_alive_on_postgres(self):
        """
        سه مسیر dedupe روی PG هم کار می‌کنند (پیش از E.4.9 همه‌شان با
        «function json_extract(text, unknown) does not exist» می‌شکستند و
        ``add_event`` خطا را فقط لاگ می‌کرد ⇒ شکست بی‌صدا).
        """
        from core.database import add_event, db_connection
        self._cleanup_logs()
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "pg-dup", "color": "black"})
        add_event(IP, "CARTRIDGE_CHANGED", {"event_id": "pg-dup", "color": "black"})
        add_event(IP, "PRINT", {"prev_total": 7, "current_total": 9})
        add_event(IP, "PRINT", {"prev_total": 7, "current_total": 9})
        with db_connection() as conn:
            rows = dict(conn.execute(
                "SELECT type, COUNT(*) FROM logs WHERE printer_ip = ? GROUP BY type",
                (IP,)).fetchall())
        self.assertEqual(rows, {"CARTRIDGE_CHANGED": 1, "PRINT": 1},
                         "ثبت دوباره باید skip شود، ولی رکورد اول باید باشد")

        # نرمال‌سازی از سمت پروژه و متقارن با درج: ستون relational مالِ تست است.
        self._cleanup_logs()

    # ``/metrics`` روی PG این‌جا سنجیده نمی‌شود: آن کوئری یک اشکال **مستقل** دارد
    # (ستون ``printer_name`` بدون ``GROUP BY`` ⇒ ``GroupingError`` روی PG). تصمیم
    # مالک: رفع در گام جدا ``E.4.9b`` با فایل تست خودش (``tests/test_metrics_pg.py``)
    # و کامیت مستقل. تست زندهٔ همین فایل فقط ``json_extract``/dedupe را می‌سنجد.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
