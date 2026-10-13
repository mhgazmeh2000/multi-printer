"""
تست‌های اعتبارسنجی تشخیص و مهاجرت ستون‌های محاسباتی GENERATED در SQLite به PostgreSQL.

در SQLite ستون‌های VIRTUAL GENERATED (مانند raw_delta در جدول logs) توسط PRAGMA table_info
خام نادیده گرفته می‌شوند، اما در DDL و PRAGMA table_xinfo حضور دارند. این سوئیت تضمین می‌کند
ابزار migrate_sqlite_to_pg این ستون‌ها را به درستی به عنوان ستون معمولی به PG منتقل کند.
"""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.migrate_sqlite_to_pg import (
    detect_sqlite_generated_columns,
    get_sqlite_table_ddl,
    get_table_columns_sqlite,
)

ROOT = Path(__file__).resolve().parent.parent


class GeneratedColumnsDetectionTests(unittest.TestCase):
    """بررسی توابع تشخیص ستون‌های GENERATED از DDL و ساختار داخلی SQLite."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_detect_virtual_generated_column(self):
        """تشخیص ستون VIRTUAL GENERATED تعریف شده با عبارت ALWAYS AS."""
        self.conn.execute(
            """
            CREATE TABLE test_logs (
                id INTEGER PRIMARY KEY,
                raw_counters TEXT,
                raw_delta INTEGER GENERATED ALWAYS AS (json_extract(raw_counters, '$.delta')) VIRTUAL
            )
            """
        )
        self.conn.commit()

        # ۱. بررسی DDL در sqlite_master
        ddl = get_sqlite_table_ddl(self.conn, "test_logs")
        self.assertIn("GENERATED", ddl.upper())
        self.assertIn("raw_delta", ddl)

        # ۲. بررسی تشخیص ستون با تابع ابزار
        gen_cols = detect_sqlite_generated_columns(self.conn, "test_logs")
        self.assertIn("raw_delta", gen_cols)

        # ۳. تضمین اینکه get_table_columns_sqlite ستون را دور نمی‌اندازد
        cols = get_table_columns_sqlite(self.conn, "test_logs")
        self.assertEqual(cols, ["id", "raw_counters", "raw_delta"])

    def test_detect_stored_generated_column(self):
        """تشخیص ستون STORED GENERATED در SQLite."""
        self.conn.execute(
            """
            CREATE TABLE test_calc (
                id INTEGER PRIMARY KEY,
                base_val INTEGER,
                mult_val INTEGER GENERATED ALWAYS AS (base_val * 2) STORED
            )
            """
        )
        self.conn.commit()

        ddl = get_sqlite_table_ddl(self.conn, "test_calc")
        self.assertIn("GENERATED", ddl.upper())

        gen_cols = detect_sqlite_generated_columns(self.conn, "test_calc")
        self.assertIn("mult_val", gen_cols)

        cols = get_table_columns_sqlite(self.conn, "test_calc")
        self.assertEqual(cols, ["id", "base_val", "mult_val"])

    def test_standard_table_without_generated_columns(self):
        """جدول عادی هیچ ستون GENERATED ندارد."""
        self.conn.execute(
            """
            CREATE TABLE regular_table (
                id INTEGER PRIMARY KEY,
                title TEXT,
                count INTEGER
            )
            """
        )
        self.conn.commit()

        ddl = get_sqlite_table_ddl(self.conn, "regular_table")
        self.assertNotIn("GENERATED", ddl.upper())

        gen_cols = detect_sqlite_generated_columns(self.conn, "regular_table")
        self.assertEqual(gen_cols, [])

        cols = get_table_columns_sqlite(self.conn, "regular_table")
        self.assertEqual(cols, ["id", "title", "count"])

    def test_generated_values_are_read_accurately_as_standard_data(self):
        """تأیید اینکه مقدار ستون محاسباتی توسط SELECT استخراج شده و به عنوان دیتا قابل درج است."""
        self.conn.execute(
            """
            CREATE TABLE test_logs (
                id INTEGER PRIMARY KEY,
                raw_counters TEXT,
                raw_delta INTEGER GENERATED ALWAYS AS (json_extract(raw_counters, '$.delta')) VIRTUAL
            )
            """
        )
        data = {"delta": 55, "total": 120}
        self.conn.execute(
            "INSERT INTO test_logs (id, raw_counters) VALUES (?, ?)",
            (1, json.dumps(data)),
        )
        self.conn.commit()

        cur = self.conn.cursor()
        cols = get_table_columns_sqlite(self.conn, "test_logs")
        cur.execute(f"SELECT {', '.join(cols)} FROM test_logs WHERE id = 1")
        row = cur.fetchone()

        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], json.dumps(data))
        self.assertEqual(row[2], 55, "مقدار ستون محاسباتی باید دقیقا 55 باشد")


if __name__ == "__main__":
    unittest.main()
