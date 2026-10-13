# -*- coding: utf-8 -*-
"""
تست‌های شکاف مهاجرت جدول printer_counters (کار ۲ فاز C).

پیش از این، سه ستون ``alert_codes`` / ``a3_total`` / ``a4_total`` در
``ensure_printer_counters_columns()`` نبودند (ولی در ``_COUNTERS_COLS`` خوانده
می‌شدند) و ``toner_overrides`` هم فقط در همان تابع بود، نه در ``init_db()``.
روی یک DB قدیمی نتیجه این بود که ``load_printer_counters`` خطای
``no such column`` می‌داد و **baseline از دست می‌رفت**.

این فایل سه سناریو را قفل می‌کند:
  ۱) DB تازه  → همه‌ی ۲۰ ستون ``_COUNTERS_COLS`` ساخته می‌شوند (idempotent).
  ۲) DB قدیمی → پس از ``init_db()`` هر ۲۰ ستون هستند و baseline حفظ می‌شود.
  ۳) DB نیمه‌مهاجرت‌شده (toner_overrides دارد، alert_codes ندارد) → بقیه اضافه می‌شوند.
"""
import os
import sqlite3
import tempfile
import unittest

import pytest

import core.database as db
from core.database import (
    _COUNTERS_COLS,
    _counter_column_alters,
    init_db,
    load_printer_counters,
)
# کل این فایل «مهاجرت ستون‌های قدیمی روی فایل SQLite» را می‌آزماید؛ روی PG منبع DDL
# Alembic است و helper مربوطه طبق قاعدهٔ E.4.5 بی‌عمل ⇒ sqlite_only.
pytestmark = pytest.mark.sqlite_only

# فهرست صریح ۲۰ ستون — مستقل از پیاده‌سازی، تا تغییر/حذف تصادفی ستون‌ها گرفته شود.
EXPECTED_COUNTER_COLUMNS = (
    "device_type", "print_total", "full_color", "black_white", "toner_level",
    "manual_override", "override_color", "override_base_level", "override_start_total",
    "override_start_toner", "yield_per_page", "force_estimate", "yield_learning_failures",
    "last_alert_codes", "a3_total", "a4_total", "alert_codes", "cartridge_id_state",
    "toner_overrides", "updated_at",
)

IP = "10.10.10.10"

# نسخه‌ی قدیمی جدول: بدون a3_total/a4_total/alert_codes/toner_overrides
LEGACY_TABLE_SQL = """
CREATE TABLE printer_counters (
    ip TEXT PRIMARY KEY,
    device_type TEXT,
    print_total INTEGER,
    full_color INTEGER,
    black_white INTEGER,
    toner_level INTEGER,
    manual_override INTEGER DEFAULT 0,
    override_color TEXT,
    override_base_level INTEGER,
    override_start_total INTEGER,
    override_start_toner INTEGER,
    yield_per_page INTEGER DEFAULT 2000,
    force_estimate INTEGER DEFAULT 0,
    yield_learning_failures INTEGER DEFAULT 0,
    last_alert_codes TEXT,
    cartridge_id_state TEXT,
    updated_at TEXT
)
"""

# نسخه‌ی نیمه‌مهاجرت‌شده: toner_overrides اضافه شده ولی سه ستون دیگر نه
HALF_MIGRATED_TABLE_SQL = LEGACY_TABLE_SQL.replace(
    "cartridge_id_state TEXT,",
    "cartridge_id_state TEXT,\n    toner_overrides TEXT DEFAULT NULL,",
)


class CounterMigrationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)          # DB_PATH نسبی است ("logs.db") → DB موقت
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ── کمکی‌ها ────────────────────────────────────────────────────────────
    def _conn(self):
        return sqlite3.connect("logs.db")

    def _columns(self, table="printer_counters"):
        with self._conn() as conn:
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

    def _create_legacy(self, ddl, **row):
        con = self._conn()
        with con:
            con.execute(ddl)
            cols = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            con.execute(
                f"INSERT INTO printer_counters ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
        con.close()

    def _assert_all_counter_columns(self):
        cols = self._columns()
        self.assertEqual(
            sorted(cols), sorted(("ip",) + EXPECTED_COUNTER_COLUMNS),
            f"ستون‌های printer_counters کامل نیست: کم={sorted(set(EXPECTED_COUNTER_COLUMNS) - set(cols))}",
        )

    # ── تست ۱: DB تازه ─────────────────────────────────────────────────────
    def test_1_fresh_db_has_all_twenty_columns(self):
        init_db()                        # DB خالی → ساخت جدول + مهاجرت
        self._assert_all_counter_columns()

        # تنها منبع ستون‌ها باید با SELECT list یکی باشد (قفلِ تک‌منبعی)
        select_names = [c.strip() for c in _COUNTERS_COLS.split(",") if c.strip()]
        self.assertEqual(select_names, list(EXPECTED_COUNTER_COLUMNS),
                         "_COUNTERS_COLS با فهرست مورد انتظار یکی نیست")

        alters = _counter_column_alters()
        self.assertEqual(len(alters), 20, "باید دقیقاً ۲۰ ALTER ستون برگردد")
        alter_names = [a.split("ADD COLUMN ", 1)[1].split(" ", 1)[0] for a in alters]
        # ترتیب ALTER عمداً «پایه، سپس افزوده‌شده» است (نه ترتیب SELECT)؛
        # اما *مجموعه‌ی* نام‌ها باید دقیقاً یکی باشد.
        self.assertEqual(set(alter_names), set(EXPECTED_COUNTER_COLUMNS),
                         "لیست ALTER با _COUNTERS_COLS ناهم‌خوان است")
        self.assertEqual(alter_names[-1], "toner_overrides",
                         "ستون‌های افزوده‌شده باید در انتهای لیست ALTER باشند")
        self.assertEqual(set(alter_names[:-1]),
                         set(EXPECTED_COUNTER_COLUMNS) - {"toner_overrides"})
        for sql in alters:
            self.assertTrue(sql.startswith("ALTER TABLE printer_counters ADD COLUMN "), sql)

        # idempotent: اجرای دوباره نه خطا بدهد نه ستونی را عوض کند
        init_db()
        db.ensure_printer_counters_columns()
        self._assert_all_counter_columns()

    # ── تست ۲: DB قدیمی (بدون سه ستون افزوده) ─────────────────────────────
    def test_2_legacy_db_gains_missing_columns_and_keeps_baseline(self):
        self._create_legacy(LEGACY_TABLE_SQL, ip=IP, print_total=5000,
                            full_color=100, black_white=4900,
                            updated_at="2026-01-05T09:00:00")
        # پیش از مهاجرت، خواندن باید شکست بخورد (همان باگ P0-2)
        self.assertIsNone(load_printer_counters(IP),
                          "DB قدیمی نباید بدون مهاجرت خوانده شود")

        init_db()
        self._assert_all_counter_columns()
        for col in ("a3_total", "a4_total", "alert_codes", "toner_overrides"):
            self.assertIn(col, self._columns(), f"ستون {col} بعد از init_db اضافه نشد")

        data = load_printer_counters(IP)
        self.assertIsNotNone(data, "baseline بعد از مهاجرت باید خوانده شود")
        self.assertEqual(data["print_total"], 5000, "baseline از دست رفت")
        self.assertEqual(data["black_white"], 4900)
        self.assertEqual(data["alert_codes"], [])          # ستون تازه، لیست خالی
        self.assertEqual(data["last_alert_codes"], [])
        self.assertEqual(data["yield_per_page"], 2000)     # مقدار پیش‌فرض ستون
        self.assertEqual(data["manual_override"], 0)

    # ── تست ۳: DB نیمه‌مهاجرت‌شده ──────────────────────────────────────────
    def test_3_half_migrated_db_completes_missing_columns(self):
        self._create_legacy(HALF_MIGRATED_TABLE_SQL, ip=IP, print_total=7777,
                            black_white=7777, toner_overrides='{"black": {"start_toner": 90}}',
                            updated_at="2026-02-01T08:00:00")
        before = self._columns()
        self.assertIn("toner_overrides", before)
        for col in ("a3_total", "a4_total", "alert_codes"):
            self.assertNotIn(col, before)

        init_db()
        self._assert_all_counter_columns()

        data = load_printer_counters(IP)               # بدون خطا
        self.assertIsNotNone(data)
        self.assertEqual(data["print_total"], 7777)
        self.assertEqual(data["a3_total"], None)       # ستون تازه، مقدار NULL
        self.assertEqual(data["alert_codes"], [])
        self.assertEqual(data["toner_overrides"], {"black": {"start_toner": 90}},
                         "داده‌ی override نباید در مهاجرت از دست برود")


if __name__ == "__main__":
    unittest.main()
