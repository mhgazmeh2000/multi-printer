"""
تست‌های ابزار تشخیصی مستقل سلامت و کیفیت داده‌های پایگاه‌داده (diagnose_data_quality.py).

بررسی می‌کند که:
۱. دیتابیس سالم دارای وضعیت healthy=True و بدون اشکال باشد.
۲. ردیف‌های شیفت‌خورده با مقادیر متنی در ستون‌های عددی به دقت شناسایی شوند.
۳. کسری سطرهای cartridge_state نسبت به پرینترها کشف و گزارش شود.
۴. ابزار CLI خروجی استاندارد JSON را در مسیر مشخص‌شده تولید کند.
"""

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tools.diagnose_data_quality import (
    diagnose_database,
    main as cli_main,
    scan_table_data_quality,
)

ROOT = Path(__file__).resolve().parent.parent


class DiagnoseDataQualityTests(unittest.TestCase):
    """مجموعه آزمون‌های تشخیصی کیفیت داده‌های دیتابیس SQLite."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_diagnosis.db")
        self.printers_path = os.path.join(self.tmp_dir.name, "test_printers.json")

        # ساخت فایل پرینترها: ۱ پرینتر رنگی (حداقل ۴ کارتریج)
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
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_healthy_database_reports_no_issues(self):
        """دیتابیس بدون تناقض باید ۱۰۰٪ سالم گزارش شود."""
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
        """تشخیص ردیف شیفت‌خورده مشابه مشکل ۴ پروداکشن (دستگاه با متن در ستون عددی)."""
        # یک سطر شیفت‌خورده که در آن device_type داخل print_total قرار گرفته است
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
        """تشخیص کسری سطرهای cartridge_state ناشی از ریکاوری ناقص (مشکل ۵)."""
        # فقط ۲ سطر کارتریج ثبت شده در حالی که پرینتر رنگی نیاز به ۴ کارتریج دارد
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
        """بررسی اجرای CLI و تولید فایل JSON با ساختار صحیح."""
        out_json = os.path.join(self.tmp_dir.name, "output_diag.json")
        exit_code = cli_main([
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
