# -*- coding: utf-8 -*-
"""
tests/test_migrate_timestamps.py — تست‌های نام‌گذاری دارای برچسب زمانی manifest (TD-005).

بررسی الزامات بدهی فنی TD-005:
۱. نام‌گذاری فایل manifest شامل پسوند زمانی {db_path}.e3-manifest-{timestamp}.json
۲. حفظ فایل‌های قبلی در اجراهای پیاپی (عدم بازنویسی و تولید چند manifest)
۳. حفظ ساختار و محتوای استاندارد مانیفست
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "migrate_timestamps_to_utc",
        PROJECT_ROOT / "tools" / "migrate_timestamps_to_utc.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


class TestTimestampedManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "logs.db")
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE logs (id INTEGER PRIMARY KEY, printer_ip TEXT, type TEXT,
                               pages INT, details TEXT, timestamp TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL,
                               updated_at TEXT NOT NULL);
            INSERT INTO meta VALUES ('timezone_cutover_at', '2026-10-03T06:35:11+00:00',
                                     '2026-10-03T06:35:11+00:00');
            INSERT INTO logs (printer_ip, type, pages, timestamp)
            VALUES ('192.168.1.10', 'PRINT', 5, '2026-10-01T08:51:39.937749');
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_filename_has_timestamp(self):
        """نام فایل مانیفست بدون پارامتر --manifest باید حاوی برچسب زمانی YYYYmmdd-HHMMSS باشد."""
        exit_code = tool.main(["--db", self.db, "--dry-run", "--quiet"])
        self.assertEqual(exit_code, 0)

        pattern = os.path.join(self.tmp.name, "logs.db.e3-manifest-*.json")
        matches = glob.glob(pattern)
        self.assertEqual(len(matches), 1, f"انتظار یک مانیفست؛ یافت شد: {matches}")
        filename = os.path.basename(matches[0])
        self.assertTrue(
            re.match(r"^logs\.db\.e3-manifest-\d{8}-\d{6}(\w*)\.json$", filename),
            f"نام فایل با الگوی تاریخ منطبق نیست: {filename}",
        )

    def test_multiple_runs_create_multiple_manifests(self):
        """اجراهای متوالی بدون پرچم --manifest نباید فایل قبلی را رونویسی کنند (TD-005)."""
        code1 = tool.main(["--db", self.db, "--dry-run", "--quiet"])
        self.assertEqual(code1, 0)

        code2 = tool.main(["--db", self.db, "--dry-run", "--quiet"])
        self.assertEqual(code2, 0)

        pattern = os.path.join(self.tmp.name, "logs.db.e3-manifest-*.json")
        matches = glob.glob(pattern)
        self.assertGreaterEqual(
            len(matches),
            2,
            f"باید حداقل ۲ فایل مانیفست برای دو اجرا موجود باشد؛ یافت شد: {matches}",
        )

    def test_manifest_content_unchanged(self):
        """محتوای مانیفست با نام‌گذاری زمانی ساختار و کلیدهای کامل E.3 را حفظ می‌کند."""
        code = tool.main(["--db", self.db, "--dry-run", "--quiet"])
        self.assertEqual(code, 0)

        pattern = os.path.join(self.tmp.name, "logs.db.e3-manifest-*.json")
        matches = glob.glob(pattern)
        self.assertTrue(matches)
        with open(matches[0], encoding="utf-8") as fh:
            data = json.load(fh)

        self.assertEqual(data["mode"], "dry-run")
        self.assertIn("cutover", data)
        self.assertIn("totals", data)
        self.assertIn("tables", data)
        self.assertIn("db", data)
        self.assertIn("sha256_before", data)
        self.assertIn("logs", data["tables"])
