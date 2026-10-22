# -*- coding: utf-8 -*-
"""
tests/test_rollup_jobs.py — تست‌های واحد خلاصه‌سازی روزانه آمار (فاز I.3).
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date

from core.rollup_jobs import (
    backfill_daily_stats,
    rollup_daily_stats,
)


class RollupJobsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cur = self.conn.cursor()

        # ساخت اسکیمای موردنیاز
        self.cur.execute(
            """
            CREATE TABLE daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                printer_ip TEXT NOT NULL,
                printer_name TEXT,
                total_pages INTEGER,
                color_pages INTEGER,
                mono_pages INTEGER,
                a3_pages INTEGER,
                a4_pages INTEGER,
                avg_toner_black REAL,
                avg_toner_cyan REAL,
                avg_toner_magenta REAL,
                avg_toner_yellow REAL,
                min_toner_black REAL,
                print_events_count INTEGER,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                CONSTRAINT uq_daily_stats_date_ip UNIQUE (date, printer_ip)
            )
        """
        )
        self.cur.execute(
            """
            CREATE TABLE print_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                printer_name TEXT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                pages INTEGER,
                color_mode TEXT,
                paper_size TEXT,
                username TEXT,
                details TEXT,
                created_at TEXT
            )
        """
        )
        self.cur.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT,
                type TEXT,
                message TEXT,
                details TEXT,
                timestamp TEXT
            )
        """
        )
        self.cur.execute(
            """
            CREATE TABLE toner_snapshots_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                toner_level REAL,
                timestamp TEXT NOT NULL
            )
        """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_rollup_from_print_events(self):
        target_day = "2026-10-14"
        # درج رویدادهای چاپ برای روز ۱۴ اکتبر
        self.cur.execute(
            """
            INSERT INTO print_events (printer_ip, printer_name, timestamp, event_type, pages, color_mode, paper_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            ("192.168.1.10", "HP-LaserJet", f"{target_day}T08:30:00.000000Z", "PRINT", 10, "mono", "A4"),
        )
        self.cur.execute(
            """
            INSERT INTO print_events (printer_ip, printer_name, timestamp, event_type, pages, color_mode, paper_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            ("192.168.1.10", "HP-LaserJet", f"{target_day}T10:15:00.000000Z", "PRINT", 5, "color", "A3"),
        )
        self.conn.commit()

        # اجرای خلاصه‌سازی
        count = rollup_daily_stats(self.conn, target_date=target_day)
        self.assertEqual(count, 1)

        row = self.cur.execute("SELECT * FROM daily_stats WHERE date = ?", (target_day,)).fetchone()
        self.assertIsNotNone(row)
        # ستون‌ها: date, ip, name, total_pages, color, mono, a3, a4
        self.assertEqual(row[1], target_day)
        self.assertEqual(row[2], "192.168.1.10")
        self.assertEqual(row[3], "HP-LaserJet")
        self.assertEqual(row[4], 15)  # total: 10 + 5
        self.assertEqual(row[5], 5)   # color: 5
        self.assertEqual(row[6], 10)  # mono: 10
        self.assertEqual(row[7], 5)   # a3: 5
        self.assertEqual(row[8], 10)  # a4: 10
        self.assertEqual(row[14], 2)  # print_events_count: 2

    def test_rollup_is_idempotent(self):
        target_day = "2026-10-14"
        self.cur.execute(
            """
            INSERT INTO print_events (printer_ip, printer_name, timestamp, event_type, pages, color_mode, paper_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            ("192.168.1.10", "HP", f"{target_day}T12:00:00.000000Z", "PRINT", 8, "mono", "A4"),
        )
        self.conn.commit()

        # اجرای بار اول
        rollup_daily_stats(self.conn, target_date=target_day)
        # اجرای بار دوم
        rollup_daily_stats(self.conn, target_date=target_day)

        rows = self.cur.execute("SELECT * FROM daily_stats WHERE date = ?", (target_day,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 8)

    def test_rollup_fallback_from_logs(self):
        target_day = "2026-10-12"
        # جدول print_events برای این روز خالی است؛ داده در logs قرار دارد
        details_1 = json.dumps({"pages": 12, "color_mode": "color", "paper_size": "Large (A3/B4)"})
        details_2 = json.dumps({"pages": 4, "color_mode": "mono", "paper_size": "Small (A4/A5)"})

        self.cur.execute(
            """
            INSERT INTO logs (printer_ip, type, message, details, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """,
            ("192.168.1.20", "PRINT", "Toshiba-Office", details_1, f"{target_day}T09:00:00.000000Z"),
        )
        self.cur.execute(
            """
            INSERT INTO logs (printer_ip, type, message, details, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """,
            ("192.168.1.20", "PRINT", "Toshiba-Office", details_2, f"{target_day}T14:30:00.000000Z"),
        )
        self.conn.commit()

        count = rollup_daily_stats(self.conn, target_date=target_day)
        self.assertEqual(count, 1)

        row = self.cur.execute("SELECT * FROM daily_stats WHERE date = ?", (target_day,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[4], 16)  # total: 12 + 4
        self.assertEqual(row[5], 12)  # color: 12
        self.assertEqual(row[6], 4)   # mono: 4
        self.assertEqual(row[7], 12)  # a3: 12
        self.assertEqual(row[8], 4)   # a4: 4

    def test_rollup_includes_toner_averages(self):
        target_day = "2026-10-13"
        self.cur.execute(
            """
            INSERT INTO print_events (printer_ip, printer_name, timestamp, event_type, pages)
            VALUES (?, ?, ?, ?, ?)
        """,
            ("192.168.1.30", "Canon", f"{target_day}T10:00:00.000000Z", "PRINT", 2),
        )
        # ثبت وضعیت‌های مختلف تونر در طول روز
        self.cur.execute(
            "INSERT INTO toner_snapshots_v2 (printer_ip, color, toner_level, timestamp) VALUES (?, ?, ?, ?)",
            ("192.168.1.30", "black", 80.0, f"{target_day}T08:00:00.000000Z"),
        )
        self.cur.execute(
            "INSERT INTO toner_snapshots_v2 (printer_ip, color, toner_level, timestamp) VALUES (?, ?, ?, ?)",
            ("192.168.1.30", "black", 70.0, f"{target_day}T16:00:00.000000Z"),
        )
        self.cur.execute(
            "INSERT INTO toner_snapshots_v2 (printer_ip, color, toner_level, timestamp) VALUES (?, ?, ?, ?)",
            ("192.168.1.30", "cyan", 55.0, f"{target_day}T12:00:00.000000Z"),
        )
        self.conn.commit()

        rollup_daily_stats(self.conn, target_date=target_day)

        row = self.cur.execute("SELECT avg_toner_black, min_toner_black, avg_toner_cyan FROM daily_stats WHERE date = ?", (target_day,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 75.0)  # avg black: (80 + 70) / 2
        self.assertEqual(row[1], 70.0)  # min black: 70
        self.assertEqual(row[2], 55.0)  # avg cyan: 55

    def test_backfill_daily_stats(self):
        # تست بازه ۲ روزه
        for day in ["2026-10-10", "2026-10-11"]:
            self.cur.execute(
                """
                INSERT INTO print_events (printer_ip, printer_name, timestamp, event_type, pages)
                VALUES (?, ?, ?, ?, ?)
            """,
                ("192.168.1.40", "Brother", f"{day}T12:00:00.000000Z", "PRINT", 5),
            )
        self.conn.commit()

        total = backfill_daily_stats(self.conn, start_date="2026-10-10", end_date="2026-10-11")
        self.assertEqual(total, 2)

        rows = self.cur.execute("SELECT date, total_pages FROM daily_stats ORDER BY date").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("2026-10-10", 5))
        self.assertEqual(rows[1], ("2026-10-11", 5))


if __name__ == "__main__":
    unittest.main()
