# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_db_migration_columns.py
# @brief:        Migration gap tests for the printer_counters table.
# @details:      Responsibilities: Verify that expected columns exist after migrations.
#                Main API: CounterMigrationTests
#                Side effects: database access
# @dependencies: internal: core.database | external: pytest
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Migration gap tests of the printer_counters table (task 2 phase C).

Previously, the three columns ``alert_codes'' / ``a3_total'' / ``a4_total'' in
``ensure_printer_counters_columns()'' were not (but read in ``_COUNTERS_COLS'').
and ``toner_overrides'' was only in the same function, not in ``init_db()''.
On an old DB the result was a ``load_printer_counters'' error
It would give ``no such column'' and **baseline would be lost**.

This file locks three scenarios:
  1) New DB → all 20 ``_COUNTERS_COLS'' columns are created (idempotent).
  2) Old DB → after ``init_db()'' all 20 columns are used and the baseline is maintained.
  3) Semi-migrated DB (has toner_overrides, does not have alert_codes) → the rest are added.
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
# This whole file tests "migrate old columns to SQLite file"; On the DDL source PG
# It is Alembic and the corresponding helper is inactive according to E.4.5 rule ⇒ sqlite_only.
pytestmark = pytest.mark.sqlite_only

# Explicit list of 20 columns — implementation independent, to allow random change/deletion of columns.
EXPECTED_COUNTER_COLUMNS = (
    "device_type", "print_total", "full_color", "black_white", "toner_level",
    "manual_override", "override_color", "override_base_level", "override_start_total",
    "override_start_toner", "yield_per_page", "force_estimate", "yield_learning_failures",
    "last_alert_codes", "a3_total", "a4_total", "alert_codes", "cartridge_id_state",
    "toner_overrides", "updated_at",
)

IP = "10.10.10.10"

# Old version of the table: no a3_total/a4_total/alert_codes/toner_overrides
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

# Semi-migrated version: added toner_overrides but not the other three columns
HALF_MIGRATED_TABLE_SQL = LEGACY_TABLE_SQL.replace(
    "cartridge_id_state TEXT,",
    "cartridge_id_state TEXT,\n    toner_overrides TEXT DEFAULT NULL,",
)


class CounterMigrationTests(unittest.TestCase):

    """Tests for counter migration."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)          # DB_PATH is relative ("logs.db") → temporary DB
        self.addCleanup(self._restore)

    def _restore(self):
        """Helper: restore."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # Auxiliaries
    def _conn(self):
        """Helper: conn."""
        return sqlite3.connect("logs.db")

    def _columns(self, table="printer_counters"):
        """Helper: columns."""
        with self._conn() as conn:
            return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

    def _create_legacy(self, ddl, **row):
        """Helper: create legacy."""
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
        """Helper: assert all counter columns."""
        cols = self._columns()
        self.assertEqual(
            sorted(cols), sorted(("ip",) + EXPECTED_COUNTER_COLUMNS),
            f"ستون‌های printer_counters کامل نیست: کم={sorted(set(EXPECTED_COUNTER_COLUMNS) - set(cols))}",
        )

    # Test 1: Fresh DB
    def test_1_fresh_db_has_all_twenty_columns(self):
        """Verifies 1 fresh db has all twenty columns."""
        init_db()                        # Empty DB → table creation + migration
        self._assert_all_counter_columns()

        # The only source of the columns must be the same as the SELECT list (single source lock).
        select_names = [c.strip() for c in _COUNTERS_COLS.split(",") if c.strip()]
        self.assertEqual(select_names, list(EXPECTED_COUNTER_COLUMNS),
                         "_COUNTERS_COLS با فهرست مورد انتظار یکی نیست")

        alters = _counter_column_alters()
        self.assertEqual(len(alters), 20, "باید دقیقاً ۲۰ ALTER ستون برگردد")
        alter_names = [a.split("ADD COLUMN ", 1)[1].split(" ", 1)[0] for a in alters]
        # ALTER order is intentionally "base, then appended" (not SELECT order);
        # But the *set* of names must be exactly the same.
        self.assertEqual(set(alter_names), set(EXPECTED_COUNTER_COLUMNS),
                         "لیست ALTER با _COUNTERS_COLS ناهم‌خوان است")
        self.assertEqual(alter_names[-1], "toner_overrides",
                         "ستون‌های افزوده‌شده باید در انتهای لیست ALTER باشند")
        self.assertEqual(set(alter_names[:-1]),
                         set(EXPECTED_COUNTER_COLUMNS) - {"toner_overrides"})
        for sql in alters:
            self.assertTrue(sql.startswith("ALTER TABLE printer_counters ADD COLUMN "), sql)

        # idempotent: re-execution will neither give error nor change a column
        init_db()
        db.ensure_printer_counters_columns()
        self._assert_all_counter_columns()

    # ── Test 2: Old DB (without three added columns)
    def test_2_legacy_db_gains_missing_columns_and_keeps_baseline(self):
        """Verifies 2 legacy db gains missing columns and keeps baseline."""
        self._create_legacy(LEGACY_TABLE_SQL, ip=IP, print_total=5000,
                            full_color=100, black_white=4900,
                            updated_at="2026-01-05T09:00:00")
        # Read must fail before migrating (same bug as P0-2)
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
        self.assertEqual(data["alert_codes"], [])          # New column, empty list
        self.assertEqual(data["last_alert_codes"], [])
        self.assertEqual(data["yield_per_page"], 2000)     # The default value of the column
        self.assertEqual(data["manual_override"], 0)

    # ── Test 3: Semi-migrated DB
    def test_3_half_migrated_db_completes_missing_columns(self):
        """Verifies 3 half migrated db completes missing columns."""
        self._create_legacy(HALF_MIGRATED_TABLE_SQL, ip=IP, print_total=7777,
                            black_white=7777, toner_overrides='{"black": {"start_toner": 90}}',
                            updated_at="2026-02-01T08:00:00")
        before = self._columns()
        self.assertIn("toner_overrides", before)
        for col in ("a3_total", "a4_total", "alert_codes"):
            self.assertNotIn(col, before)

        init_db()
        self._assert_all_counter_columns()

        data = load_printer_counters(IP)               # without error
        self.assertIsNotNone(data)
        self.assertEqual(data["print_total"], 7777)
        self.assertEqual(data["a3_total"], None)       # New column, NULL value
        self.assertEqual(data["alert_codes"], [])
        self.assertEqual(data["toner_overrides"], {"black": {"start_toner": 90}},
                         "داده‌ی override نباید در مهاجرت از دست برود")


if __name__ == "__main__":
    unittest.main()
