# =============================================================================
# @file:         tests/test_generated_columns_migration.py
# @brief:        Tests detection and migration of generated columns from SQLite to PostgreSQL.
# @details:      Responsibilities: Check generated-column detection.
#                Main API: GeneratedColumnsDetectionTests
#                Side effects: database access, file I/O
# @dependencies: internal: tools.migrate_sqlite_to_pg
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Validation tests for detection and migration of GENERATED calculation columns in SQLite to PostgreSQL.

In SQLite VIRTUAL GENERATED columns (like raw_delta in logs table) by PRAGMA table_info
Ignored raw, but present in DDL and PRAGMA table_xinfo. This suite guarantees
The migrate_sqlite_to_pg tool will correctly migrate these columns to PG as regular columns.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.migrate_sqlite_to_pg import (
    detect_sqlite_generated_columns,
    get_sqlite_table_ddl,
    get_table_columns_sqlite,
)

ROOT = Path(__file__).resolve().parent.parent


class GeneratedColumnsDetectionTests(unittest.TestCase):
    """Checking the detection functions of GENERATED columns from DDL and the internal structure of SQLite."""

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

    def test_detect_virtual_generated_column(self):
        """Detection of VIRTUAL GENERATED column defined with ALWAYS AS statement."""
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

        # 1. Check DDL in sqlite_master
        ddl = get_sqlite_table_ddl(self.conn, "test_logs")
        self.assertIn("GENERATED", ddl.upper())
        self.assertIn("raw_delta", ddl)

        # 2. Column detection check with tool function
        gen_cols = detect_sqlite_generated_columns(self.conn, "test_logs")
        self.assertIn("raw_delta", gen_cols)

        # 3. Ensure that get_table_columns_sqlite does not discard the column
        cols = get_table_columns_sqlite(self.conn, "test_logs")
        self.assertEqual(cols, ["id", "raw_counters", "raw_delta"])

    def test_detect_stored_generated_column(self):
        """Detecting STORED GENERATED column in SQLite."""
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
        """Normal table has no GENERATED column."""
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
        """Verify that the calculated column value is extracted by SELECT and can be inserted as data."""
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
