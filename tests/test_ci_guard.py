# -*- coding: utf-8 -*-
"""
tests/test_ci_guard.py — تست راستی‌آزمایی گارد ضد دوبارشماری و تمامیت کشف آزمون‌ها.

این تست تضمین می‌کند:
  ۱) pytest تمام آزمون‌های پروژه را بدون هیچ‌گونه دوبارشماری (Zero Duplicates) کشف می‌کند.
  ۲) ۱۰۰٪ کلاس‌ها و متدهای مبتنی بر unittest.TestCase توسط pytest شناسایی و اجرا می‌شوند.
  ۳) هیچ تست جدید تابعی یا کلاسی ناخواسته از قلم نمی‌افتد.
"""
from __future__ import annotations

import subprocess
import sys
import unittest


class CiGuardParityTests(unittest.TestCase):
    """تست‌های تمامیت گارد ضد دوبارشماری و انطباق کامل pytest با unittest."""

    @classmethod
    def setUpClass(cls):
        # استخراج شناسه‌های آزمون‌های کشف‌شده توسط pytest
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/",
             "--collect-only", "-q", "--color=no"],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise RuntimeError(f"pytest --collect-only failed:\n{out.stdout}\n{out.stderr}")

        cls.pytest_nodeids = [
            line.strip() for line in out.stdout.splitlines() if "::" in line
        ]
        cls.pytest_clean = {line.split("[", 1)[0] for line in cls.pytest_nodeids}

        # استخراج آزمون‌های کشف‌شده توسط unittest discover
        loader = unittest.TestLoader()
        suite = loader.discover("tests", pattern="test_*.py")

        unittest_ids = set()

        def _walk_suite(s):
            for item in s:
                if isinstance(item, unittest.TestCase):
                    unittest_ids.add(item.id())
                elif hasattr(item, "__iter__"):
                    _walk_suite(item)

        _walk_suite(suite)

        cls.normalized_unittest = set()
        for tid in unittest_ids:
            parts = tid.split(".")
            if parts[0] == "tests":
                parts = parts[1:]
            if len(parts) >= 3:
                filename = f"tests/{parts[0]}.py"
                classname = parts[1]
                methodname = ".".join(parts[2:])
                cls.normalized_unittest.add(f"{filename}::{classname}::{methodname}")

    def test_zero_double_counting_in_pytest(self):
        """هر nodeid کشف‌شده توسط pytest باید کاملاً یکتا باشد (بدون دوبارشماری)."""
        total = len(self.pytest_nodeids)
        unique = len(set(self.pytest_nodeids))
        self.assertEqual(
            total, unique,
            f"دوبارشماری در pytest رخ داده است: {total - unique} مورد تکراری",
        )

    def test_all_unittest_testcases_are_discovered_by_pytest(self):
        """تمامی ۶۲۴ تست unittest.TestCase باید ۱۰۰٪ در خروجی pytest وجود داشته باشند."""
        missing = self.normalized_unittest - self.pytest_clean
        self.assertEqual(
            missing, set(),
            f"تست‌های زیر در unittest کشف شدند اما در pytest جا افتادند: {missing}",
        )

    def test_total_test_baseline_is_preserved(self):
        """مجموع آزمون‌های پروژه نباید از کف شناخته‌شده (۸۵۰ تست) کمتر شود."""
        self.assertGreaterEqual(
            len(self.pytest_nodeids), 850,
            f"افت مشکوک تست‌ها: انتظار حداقل ۸۵۰ تست، یافت شد: {len(self.pytest_nodeids)}",
        )


if __name__ == "__main__":
    unittest.main()
