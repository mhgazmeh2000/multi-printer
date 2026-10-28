# =============================================================================
# @file:         tests/test_diagnose_data_quality.py
# @brief:        Tests for the standalone data-quality diagnostic tool.
# @details:      Responsibilities: Check the diagnostic counts and reports.
#                Main API: DiagnoseDataQualityTests
#                Side effects: database access, file I/O
# @dependencies: internal: tools.diagnose_data_quality
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Standalone database health and data quality diagnostic tool tests (diagnose_data_quality.py).

Checks that:
1. A healthy database should have a status of healthy=True and no problems.
2. Carefully identify shifted rows with text values ​​in numeric columns.
3. Deficiency of cartridge_state lines relative to printers should be discovered and reported.
4. The CLI tool produces JSON standard output in the specified path.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.diagnose_data_quality import (
    diagnose_database,
    main as cli_main,
)

ROOT = Path(__file__).resolve().parent.parent


class DiagnoseDataQualityTests(unittest.TestCase):
    """A collection of SQLite database data quality diagnostic tests."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_diagnosis.db")
        self.printers_path = os.path.join(self.tmp_dir.name, "test_printers.json")

        # Creating files for printers: 1 color printer (at least 4 cartridges)
        printers_cfg = [
            {"ip": "10.0.0.1", "name": "Color-MFP", "device_type": "color"}
        ]
        Path(self.printers_path).write_text(json.dumps(printers_cfg), encoding="utf-8")

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            """
            CREATE TABLE printer_counters (
                ip TEXT PRIMARY KEY,
                print_total INTEGER,
                full_color INTEGER
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE cartridge_state (
                id INTEGER PRIMARY KEY,
                printer_ip TEXT,
                color TEXT,
                level INTEGER
            )
            """
        )
        self.conn.commit()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_healthy_database_reports_no_issues(self):
        """A database with no conflicts should be reported as 100% healthy."""
        self.conn.execute(
            "INSERT INTO printer_counters VALUES (?, ?, ?)",
            ("10.0.0.1", 5000, 1200),
        )
        for i, color in enumerate(["black", "cyan", "magenta", "yellow"]):
            self.conn.execute(
                "INSERT INTO cartridge_state VALUES (?, ?, ?, ?)",
                (i + 1, "10.0.0.1", color, 90),
            )
        self.conn.commit()

        report = diagnose_database(self.db_path, printers_path=self.printers_path)
        self.assertTrue(report["summary"]["healthy"])
        self.assertEqual(report["summary"]["total_issues"], 0)
        self.assertEqual(report["summary"]["tables_with_issues"], 0)

    def test_detects_corrupt_text_in_numeric_columns(self):
        """Detecting the shifted row similar to production problem 4 (device with text in numeric column)."""
        # A shifted line where device_type is inserted into print_total
        self.conn.execute(
            "INSERT INTO printer_counters VALUES (?, ?, ?)",
            ("172.16.25.54", "MFP07622411", 500),
        )
        self.conn.commit()

        report = diagnose_database(self.db_path, printers_path=self.printers_path)
        self.assertFalse(report["summary"]["healthy"])
        self.assertGreater(report["summary"]["total_issues"], 0)

        pc_issues = report["tables"]["printer_counters"]["issues"]
        self.assertEqual(len(pc_issues), 1)
        self.assertEqual(pc_issues[0]["column"], "print_total")
        self.assertEqual(pc_issues[0]["expected_type"], "integer")
        self.assertEqual(pc_issues[0]["actual_type"], "text")
        self.assertEqual(pc_issues[0]["row_ip"], "172.16.25.54")
        self.assertEqual(pc_issues[0]["value"], "MFP07622411")

    def test_detects_cartridge_state_row_count_deficit(self):
        """Detecting the deficit of cartridge_state lines due to incomplete recovery (problem 5)."""
        # Only 2 lines of cartridges are registered while the color printer needs 4 cartridges
        self.conn.execute(
            "INSERT INTO cartridge_state VALUES (?, ?, ?, ?)",
            (1, "10.0.0.1", "black", 80),
        )
        self.conn.execute(
            "INSERT INTO cartridge_state VALUES (?, ?, ?, ?)",
            (2, "10.0.0.1", "cyan", 75),
        )
        self.conn.commit()

        report = diagnose_database(self.db_path, printers_path=self.printers_path)
        cs_report = report["tables"]["cartridge_state"]
        self.assertEqual(cs_report["actual_rows"], 2)
        self.assertEqual(cs_report["expected_min_rows"], 4)

        issues = cs_report["issues"]
        self.assertTrue(any(iss.get("issue_type") == "row_count_deficit" for iss in issues))

    def test_cli_execution_generates_json_file(self):
        """Checking the CLI implementation and generating a JSON file with the correct structure."""
        out_json = os.path.join(self.tmp_dir.name, "output_diag.json")
        cli_main([
            "--db", self.db_path,
            "--printers", self.printers_path,
            "--output", out_json,
            "--quiet",
        ])
        self.assertTrue(os.path.isfile(out_json))
        data = json.loads(Path(out_json).read_text(encoding="utf-8"))
        self.assertIn("tables", data)
        self.assertIn("summary", data)
        self.assertIn("printer_counters", data["tables"])


if __name__ == "__main__":
    unittest.main()
