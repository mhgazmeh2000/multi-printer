# -*- coding: utf-8 -*-
"""
tests/test_yield_engine_toshiba.py — تست‌های بهبودهای موتور تخمین بازدهی کارتریج توشیبا (فاز I.5).
"""

from __future__ import annotations

import sqlite3
import unittest

from core.yield_engine import (
    compute_yield_per_color,
    estimate_yield_fallback,
    filter_invalid_toner_levels,
)


class TestYieldEngineToshiba(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE cartridge_lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                cartridge_color TEXT NOT NULL,
                actual_yield_per_page REAL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE yield_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                estimated_yield INTEGER
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE cartridge_state (
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                printer_model TEXT,
                yield_per_page REAL,
                PRIMARY KEY (printer_ip, color)
            )
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_filter_invalid_toner_levels(self):
        """بهبود ۲: فیلتر مقادیر منفی (-1, -2, -3)، مقادیر پوچ و خارج از بازه 0-100."""
        raw_levels = [-1, -2, -3, None, "invalid", 0, 45.5, 100, 105, -10]
        cleaned = filter_invalid_toner_levels(raw_levels)
        self.assertEqual(cleaned, [0.0, 45.5, 100.0])

    def test_estimate_yield_fallback_toshiba(self):
        """بهبود ۶: تخمین پایه بر اساس برند و رنگ برای توشیبا."""
        black_yield = estimate_yield_fallback("Toshiba", "black")
        cyan_yield = estimate_yield_fallback("Toshiba", "cyan")
        magenta_yield = estimate_yield_fallback("Toshiba", "magenta")
        yellow_yield = estimate_yield_fallback("Toshiba", "yellow")

        self.assertEqual(black_yield, 38400)
        self.assertEqual(cyan_yield, 33600)
        self.assertEqual(magenta_yield, 33600)
        self.assertEqual(yellow_yield, 33600)

    def test_estimate_yield_fallback_fuzzy_model_name(self):
        """تخمین بر اساس برند وقتی نام مدل کامل پاس داده می‌شود."""
        yield_val = estimate_yield_fallback("Toshiba e-Studio 2505AC", "cyan")
        self.assertEqual(yield_val, 33600)

        unknown_val = estimate_yield_fallback("Acme Corp", "black", default_pages=4000)
        self.assertEqual(unknown_val, 4000)

    def test_compute_yield_per_color_median_and_outliers(self):
        """بهبود ۱ و ۴ و ۵: تفکیک بر اساس رنگ، استخراج از cartridge_lifecycle و محاسبه میانه."""
        # اضافه کردن داده برای مشکی: شامل مقادیر واقعی و یک outlier
        # مقادیر: 30000, 31000, 32000, 50000 (outlier) -> میانه 31500
        for val in [30000, 31000, 32000, 50000]:
            self.conn.execute(
                "INSERT INTO cartridge_lifecycle (printer_ip, cartridge_color, actual_yield_per_page) VALUES (?, ?, ?)",
                ("192.168.1.50", "black", val),
            )
        # داده برای زرد: 25000, 26000, 27000 -> میانه 26000
        for val in [25000, 26000, 27000]:
            self.conn.execute(
                "INSERT INTO cartridge_lifecycle (printer_ip, cartridge_color, actual_yield_per_page) VALUES (?, ?, ?)",
                ("192.168.1.50", "yellow", val),
            )
        self.conn.commit()

        yield_black = compute_yield_per_color("192.168.1.50", "black", conn=self.conn)
        yield_yellow = compute_yield_per_color("192.168.1.50", "yellow", conn=self.conn)

        self.assertEqual(yield_black, 31500.0)
        self.assertEqual(yield_yellow, 26000.0)

    def test_compute_yield_per_color_default_row_masks_brand_fallback(self):
        """Current behavior: a cartridge_state row holding the DEFAULT 2000 is returned as-is.

        Step 4 of compute_yield_per_color accepts any yield_per_page > 0, so the
        brand/color fallback (step 5) is not reached when the row holds the
        default. This test documents that behavior. See the audit report, R5,
        for the open decision on whether step 4 should skip default rows.
        """
        self.conn.execute(
            "INSERT INTO cartridge_state (printer_ip, color, printer_model, yield_per_page) VALUES (?, ?, ?, ?)",
            ("192.168.1.60", "cyan", "Toshiba e-Studio", 2000),
        )
        self.conn.commit()

        val = compute_yield_per_color("192.168.1.60", "cyan", conn=self.conn)
        self.assertEqual(val, 2000.0)

    def test_compute_yield_per_color_brand_fallback_when_state_yield_is_zero(self):
        """A cartridge_state row with yield_per_page = 0 falls through to the brand/color fallback."""
        self.conn.execute(
            "INSERT INTO cartridge_state (printer_ip, color, printer_model, yield_per_page) VALUES (?, ?, ?, ?)",
            ("192.168.1.60", "cyan", "Toshiba e-Studio", 0),
        )
        self.conn.commit()

        val = compute_yield_per_color("192.168.1.60", "cyan", conn=self.conn)
        self.assertEqual(val, 33600.0)

    def test_compute_yield_per_color_reads_estimated_yield_from_samples(self):
        """Step 2 reads estimated_yield from accepted yield_samples and ignores rejected rows."""
        for val, accepted in ((30000, 1), (31000, 1), (32000, 1), (99999, 0)):
            self.conn.execute(
                "INSERT INTO yield_samples (printer_ip, color, accepted, estimated_yield) VALUES (?, ?, ?, ?)",
                ("192.168.1.70", "magenta", accepted, val),
            )
        self.conn.commit()

        val = compute_yield_per_color("192.168.1.70", "magenta", conn=self.conn)
        self.assertEqual(val, 31000.0)

    def test_compute_yield_per_color_logs_when_samples_query_fails(self):
        """A failing yield_samples query is logged instead of being silently ignored."""
        self.conn.execute("DROP TABLE yield_samples")
        self.conn.execute(
            """
            CREATE TABLE yield_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                color TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                yield_per_page REAL
            )
            """
        )
        self.conn.commit()

        with self.assertLogs("PrinterMonitor", level="WARNING") as cm:
            val = compute_yield_per_color("192.168.1.80", "black", conn=self.conn)

        self.assertIsNone(val)
        self.assertTrue(any("yield_samples query failed" in line for line in cm.output))

if __name__ == "__main__":
    unittest.main()
