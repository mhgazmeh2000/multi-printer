# =============================================================================
# @file:         tests/test_preflight_data_quality.py
# @brief:        Tests for pre-flight data-quality validation before SQLite-to-PostgreSQL migration.
# @details:      Responsibilities: Check the preflight rules.
#                Main API: PreflightDataQualityTests
#                Side effects: database access, file I/O
# @dependencies: internal: tools.migrate_sqlite_to_pg
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Preflight Data Quality Validation tests in SQLite to PostgreSQL migration.

Checks that:
1. Healthy data pass the validation without errors.
2. Data with data type errors less than 5% will only be logged/reported and will not prevent migration.
3. Data with an error of more than 5% will lead to a permanent stop of the migration (RuntimeError).
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.migrate_sqlite_to_pg import (
    TablePlan,
    _preflight_data_quality,
)

ROOT = Path(__file__).resolve().parent.parent


class PreflightDataQualityTests(unittest.TestCase):
    """A set of pre-validation tests for SQLite data before sending to PG."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self.conn.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_clean_table_passes_without_issues(self):
        """The table with correct and healthy data should be verified without any problems."""
        self.conn.execute(
            """
            CREATE TABLE printer_counters (
                ip TEXT PRIMARY KEY,
                print_total INTEGER,
                full_color INTEGER
            )
            """
        )
        for i in range(10):
            self.conn.execute(
                "INSERT INTO printer_counters VALUES (?, ?, ?)",
                (f"192.168.1.{i}", 1000 + i, 200 + i),
            )
        self.conn.commit()

        plan = [
            TablePlan(
                name="printer_counters",
                sqlite_rows=10,
                pg_rows=0,
                columns=["ip", "print_total", "full_color"],
                strategy="BATCH",
            )
        ]

        findings = _preflight_data_quality(self.conn, plan)
        self.assertEqual(findings, {}, "نباید هیچ خطایی برای جدول سالم گزارش شود")

    def test_corrupt_data_under_5_percent_is_reported_without_error(self):
        """Data type mismatch error below 5% is only reported and migration is not stopped."""
        self.conn.execute(
            """
            CREATE TABLE printer_counters (
                ip TEXT PRIMARY KEY,
                print_total INTEGER,
                full_color INTEGER
            )
            """
        )
        # 100 lines: 99 healthy lines and 1 shifted/text line (1% error)
        for i in range(99):
            self.conn.execute(
                "INSERT INTO printer_counters VALUES (?, ?, ?)",
                (f"192.168.1.{i}", 1000 + i, 200 + i),
            )
        # Shifted line (crash.recover)
        self.conn.execute(
            "INSERT INTO printer_counters VALUES (?, ?, ?)",
            ("172.16.25.54", "MFP07622411", 500),
        )
        self.conn.commit()

        plan = [
            TablePlan(
                name="printer_counters",
                sqlite_rows=100,
                pg_rows=0,
                columns=["ip", "print_total", "full_color"],
                strategy="BATCH",
            )
        ]

        # It should not issue an Exception, but give a dictionary of problems
        findings = _preflight_data_quality(self.conn, plan)
        self.assertIn("printer_counters", findings)
        issues = findings["printer_counters"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["column"], "print_total")
        self.assertEqual(issues[0]["corrupt_count"], 1)
        self.assertEqual(issues[0]["expected_type"], "integer")
        self.assertEqual(issues[0]["actual_type"], "text")
        self.assertAlmostEqual(issues[0]["ratio"], 0.01)

    def test_corrupt_data_over_5_percent_halts_migration(self):
        """If the rate of damaged rows is greater than 5%, it should immediately throw a RuntimeError."""
        self.conn.execute(
            """
            CREATE TABLE printer_counters (
                ip TEXT PRIMARY KEY,
                print_total INTEGER
            )
            """
        )
        # 20 lines: 16 healthy and 4 bad (20% error)
        for i in range(16):
            self.conn.execute(
                "INSERT INTO printer_counters VALUES (?, ?)",
                (f"192.168.1.{i}", 1000 + i),
            )
        for j in range(4):
            self.conn.execute(
                "INSERT INTO printer_counters VALUES (?, ?)",
                (f"172.16.25.{j}", f"CORRUPT_{j}"),
            )
        self.conn.commit()

        plan = [
            TablePlan(
                name="printer_counters",
                sqlite_rows=20,
                pg_rows=0,
                columns=["ip", "print_total"],
                strategy="BATCH",
            )
        ]

        with self.assertRaises(RuntimeError) as ctx:
            _preflight_data_quality(self.conn, plan)

        err_msg = str(ctx.exception)
        self.assertIn("printer_counters", err_msg)
        self.assertIn("print_total", err_msg)
        self.assertIn("بیش از ۵٪", err_msg)


if __name__ == "__main__":
    unittest.main()
