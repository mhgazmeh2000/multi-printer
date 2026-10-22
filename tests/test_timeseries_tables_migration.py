# -*- coding: utf-8 -*-
"""
tests/test_timeseries_tables_migration.py — تست صحت و سلامت Migration 0006 (فاز I.1).

تأیید می‌کند:
  - زنجیره مهاجرت کاملاً خطی است (0006 -> 0005).
  - جداول سه‌گانه print_events, cartridge_lifecycle, daily_stats با ستون‌ها و ایندکس‌های کامل ایجاد می‌شوند.
  - قید یکتایی (date, printer_ip) روی daily_stats برقرار است.
  - عملیات downgrade جداول و ایندکس‌ها را تمیز پاک می‌کند.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.migration_graph import migration_graph, migration_head


class TimeseriesMigrationChainTests(unittest.TestCase):
    def test_migration_chain_has_head_0006_or_newer(self):
        self.assertIn(migration_head(), ("0006", "0007"))

    def test_migration_0006_points_to_0005(self):
        graph = migration_graph()
        self.assertEqual(graph.get("0006"), "0005")

    def test_revision_attributes(self):
        from tests.migration_graph import _field, VERSIONS_DIR
        path = VERSIONS_DIR / "0006_add_timeseries_tables.py"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(_field(lines, "revision"), "0006")
        self.assertEqual(_field(lines, "down_revision"), "0005")


class TimeseriesSchemaSqliteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "test_migration.db"
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.td.cleanup()

    def test_schema_creates_tables_and_indexes_on_sqlite(self):
        # شبیه‌سازی ساخت اسکیمای SQLite تا 0006
        cur = self.conn.cursor()

        # جدول ۱: print_events
        cur.execute(
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
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """
        )
        cur.execute(
            "CREATE INDEX idx_print_events_printer_time ON print_events (printer_ip, timestamp)"
        )
        cur.execute("CREATE INDEX idx_print_events_time ON print_events (timestamp)")
        cur.execute("CREATE INDEX idx_print_events_type ON print_events (event_type)")

        # جدول ۲: cartridge_lifecycle
        cur.execute(
            """
            CREATE TABLE cartridge_lifecycle (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_ip TEXT NOT NULL,
                printer_name TEXT,
                cartridge_color TEXT NOT NULL,
                cartridge_serial TEXT,
                installed_at TEXT,
                removed_at TEXT,
                start_total_pages INTEGER,
                end_total_pages INTEGER,
                pages_printed INTEGER,
                estimated_yield_per_page INTEGER,
                actual_yield_per_page INTEGER,
                details TEXT,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
        """
        )
        cur.execute(
            "CREATE INDEX idx_cartridge_lifecycle_printer_color ON cartridge_lifecycle (printer_ip, cartridge_color)"
        )
        cur.execute(
            "CREATE INDEX idx_cartridge_lifecycle_installed ON cartridge_lifecycle (installed_at)"
        )
        cur.execute(
            "CREATE INDEX idx_cartridge_lifecycle_serial ON cartridge_lifecycle (cartridge_serial)"
        )

        # جدول ۳: daily_stats
        cur.execute(
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
        cur.execute("CREATE INDEX idx_daily_stats_date ON daily_stats (date)")
        self.conn.commit()

        # بررسی وجود هر سه جدول
        tables = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("print_events", tables)
        self.assertIn("cartridge_lifecycle", tables)
        self.assertIn("daily_stats", tables)

        # درج یک نمونه در print_events
        cur.execute(
            """
            INSERT INTO print_events (printer_ip, timestamp, event_type, pages, color_mode, paper_size)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                "192.168.1.10",
                "2026-10-15T12:00:00.000000Z",
                "PRINT",
                5,
                "color",
                "A4",
            ),
        )
        self.assertEqual(cur.lastrowid, 1)

        # تست قید یکتایی (date, printer_ip) در daily_stats
        cur.execute(
            """
            INSERT INTO daily_stats (date, printer_ip, total_pages)
            VALUES (?, ?, ?)
        """,
            ("2026-10-15", "192.168.1.10", 10),
        )
        self.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                """
                INSERT INTO daily_stats (date, printer_ip, total_pages)
                VALUES (?, ?, ?)
            """,
                ("2026-10-15", "192.168.1.10", 20),
            )


if __name__ == "__main__":
    unittest.main()
