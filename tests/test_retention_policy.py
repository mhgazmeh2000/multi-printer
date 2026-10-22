# -*- coding: utf-8 -*-
"""
tests/test_retention_policy.py — تست‌های واحد سیاست نگهداری داده (فاز I.2).
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from core.retention_policy import (
    PERMANENT_TABLES,
    RETENTION_DAYS,
    purge_table,
    run_retention,
)


class RetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cur = self.conn.cursor()

        # ساخت جداول تست
        for tbl in ["logs", "sensor_readings", "toner_snapshots_v2", "security_events"]:
            self.cur.execute(
                f"""
                CREATE TABLE {tbl} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    data TEXT
                )
            """
            )

        # جدول دائمی
        for tbl in PERMANENT_TABLES:
            self.cur.execute(
                f"""
                CREATE TABLE {tbl} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    data TEXT
                )
            """
            )

        self.conn.commit()
        self.now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()

    def test_purge_deletes_old_rows_and_keeps_recent_rows(self):
        old_ts = (self.now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        recent_ts = (self.now - timedelta(days=50)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # درج ۲ ردیف قدیمی و ۳ ردیف جدید
        self.cur.execute("INSERT INTO logs (timestamp, data) VALUES (?, ?)", (old_ts, "old_1"))
        self.cur.execute("INSERT INTO logs (timestamp, data) VALUES (?, ?)", (old_ts, "old_2"))
        self.cur.execute("INSERT INTO logs (timestamp, data) VALUES (?, ?)", (recent_ts, "new_1"))
        self.cur.execute("INSERT INTO logs (timestamp, data) VALUES (?, ?)", (recent_ts, "new_2"))
        self.cur.execute("INSERT INTO logs (timestamp, data) VALUES (?, ?)", (recent_ts, "new_3"))
        self.conn.commit()

        # پاک‌سازی با بازه ۹۰ روز
        deleted = purge_table(self.conn, "logs", days=90, batch_size=10, now_dt=self.now)
        self.assertEqual(deleted, 2)

        remaining = self.cur.execute("SELECT data FROM logs ORDER BY id").fetchall()
        self.assertEqual(len(remaining), 3)
        self.assertEqual([r[0] for r in remaining], ["new_1", "new_2", "new_3"])

    def test_permanent_tables_cannot_be_purged(self):
        for tbl in PERMANENT_TABLES:
            with self.assertRaises(ValueError) as ctx:
                purge_table(self.conn, tbl, days=90, now_dt=self.now)
            self.assertIn("دائمی", str(ctx.exception))

    def test_unregistered_table_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            purge_table(self.conn, "arbitrary_table", days=90, now_dt=self.now)
        self.assertIn("مجاز", str(ctx.exception))

    def test_negative_days_raises_value_error(self):
        with self.assertRaises(ValueError):
            purge_table(self.conn, "logs", days=-5, now_dt=self.now)

    def test_batch_deletion_across_multiple_iterations(self):
        old_ts = (self.now - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # درج ۲۵ ردیف قدیمی
        for i in range(25):
            self.cur.execute("INSERT INTO sensor_readings (timestamp, data) VALUES (?, ?)", (old_ts, f"s_{i}"))
        self.conn.commit()

        # حذف با بسته‌های ۱۰ تایی (باید در ۳ مرحله اجرا شود)
        deleted = purge_table(self.conn, "sensor_readings", days=90, batch_size=10, now_dt=self.now)
        self.assertEqual(deleted, 25)

        remaining = self.cur.execute("SELECT count(*) FROM sensor_readings").fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_run_retention_all_tables(self):
        old_ts = (self.now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        very_old_sec = (self.now - timedelta(days=800)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        self.cur.execute("INSERT INTO logs (timestamp, data) VALUES (?, ?)", (old_ts, "log1"))
        self.cur.execute("INSERT INTO sensor_readings (timestamp, data) VALUES (?, ?)", (old_ts, "sensor1"))
        self.cur.execute("INSERT INTO toner_snapshots_v2 (timestamp, data) VALUES (?, ?)", (old_ts, "toner1"))
        self.cur.execute("INSERT INTO security_events (timestamp, data) VALUES (?, ?)", (very_old_sec, "sec1"))
        self.conn.commit()

        results = run_retention(self.conn, now_dt=self.now)
        self.assertEqual(results["logs"], 1)
        self.assertEqual(results["sensor_readings"], 1)
        self.assertEqual(results["toner_snapshots_v2"], 1)
        self.assertEqual(results["security_events"], 1)


if __name__ == "__main__":
    unittest.main()
