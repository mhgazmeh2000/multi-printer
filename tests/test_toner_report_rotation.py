# -*- coding: utf-8 -*-
"""Tests for thread-safe 20 MB size-based rotation of toner_report.txt."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import unittest
from unittest import mock

import core.enhanced_collector as ec


class TonerReportRotationTests(unittest.TestCase):
    """Verify _log_to_toner_report rotates toner_report.txt to .1 at 20 MB cap."""

    def test_default_cap_is_20_megabytes(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TONER_REPORT_MAX_MB", None)
            self.assertEqual(ec._resolve_toner_report_max_bytes(), 20 * 1024 * 1024)

    def test_env_override_and_invalid_fallback(self):
        with mock.patch.dict(os.environ, {"TONER_REPORT_MAX_MB": "15"}):
            self.assertEqual(ec._resolve_toner_report_max_bytes(), 15 * 1024 * 1024)
        with mock.patch.dict(os.environ, {"TONER_REPORT_MAX_MB": "not-a-number"}):
            self.assertEqual(ec._resolve_toner_report_max_bytes(), 20 * 1024 * 1024)
        with mock.patch.dict(os.environ, {"TONER_REPORT_MAX_MB": "-5"}):
            self.assertEqual(ec._resolve_toner_report_max_bytes(), 20 * 1024 * 1024)

    def test_writes_below_limit_do_not_rotate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "toner_report.txt")
            ec._log_to_toner_report("line-1", report_path=report_path, max_bytes=200)
            ec._log_to_toner_report("line-2", report_path=report_path, max_bytes=200)

            self.assertTrue(os.path.exists(report_path))
            self.assertFalse(os.path.exists(f"{report_path}.1"))
            with open(report_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "line-1\nline-2\n")

    def test_rotates_to_dot_one_when_exceeding_limit_and_replaces_old_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "toner_report.txt")
            # Seed a 90-byte file under a 100-byte cap
            ec._log_to_toner_report("A" * 89, report_path=report_path, max_bytes=100)
            self.assertFalse(os.path.exists(f"{report_path}.1"))

            # Next 30-byte write pushes total past 100 -> rotates first batch to .1
            ec._log_to_toner_report("B" * 29, report_path=report_path, max_bytes=100)
            self.assertTrue(os.path.exists(f"{report_path}.1"))
            with open(f"{report_path}.1", "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), ("A" * 89) + "\n")
            with open(report_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), ("B" * 29) + "\n")

            # Trigger a second rollover and ensure .1 is overwritten (never unbounded .2/.3)
            ec._log_to_toner_report("C" * 80, report_path=report_path, max_bytes=100)
            self.assertFalse(os.path.exists(f"{report_path}.2"))
            with open(f"{report_path}.1", "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), ("B" * 29) + "\n")
            with open(report_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), ("C" * 80) + "\n")

    def test_concurrent_multithreaded_writes_with_rotation_are_thread_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = os.path.join(tmpdir, "toner_report.txt")

            def _worker(worker_id: int) -> None:
                for seq in range(25):
                    ec._log_to_toner_report(
                        f"worker={worker_id} seq={seq} payload={'X' * 40}",
                        report_path=report_path,
                        max_bytes=1024,
                    )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_worker, range(8)))

            self.assertTrue(os.path.exists(report_path))
            self.assertTrue(os.path.exists(f"{report_path}.1"))
            self.assertLessEqual(os.path.getsize(report_path), 1024)
            self.assertLessEqual(os.path.getsize(f"{report_path}.1"), 1024)


if __name__ == "__main__":
    unittest.main()
