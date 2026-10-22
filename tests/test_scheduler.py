# -*- coding: utf-8 -*-
"""
tests/test_scheduler.py — تست‌های زمان‌بند خودکار وظایف (فاز I.4).
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from core.scheduler import (
    _job_retention,
    _job_rollup,
    start_scheduler,
    stop_scheduler,
)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()
        stop_scheduler()

    def test_start_scheduler_graceful_when_no_apscheduler(self):
        with mock.patch("core.scheduler.HAS_APSCHEDULER", False):
            sched = start_scheduler(self.conn)
            self.assertIsNone(sched)

    def test_start_scheduler_creates_jobs_when_apscheduler_present(self):
        mock_sched_cls = mock.MagicMock()
        mock_sched_inst = mock_sched_cls.return_value
        with mock.patch("core.scheduler.HAS_APSCHEDULER", True), \
             mock.patch("core.scheduler.BackgroundScheduler", mock_sched_cls), \
             mock.patch("core.scheduler.CronTrigger", mock.MagicMock()):
            sched = start_scheduler(self.conn)
            self.assertEqual(sched, mock_sched_inst)
            self.assertEqual(mock_sched_inst.add_job.call_count, 2)
            mock_sched_inst.start.assert_called_once()

    def test_job_rollup_invokes_rollup_safely(self):
        with mock.patch("core.scheduler.rollup_daily_stats") as mock_rollup:
            _job_rollup(self.conn)
            mock_rollup.assert_called_once_with(self.conn)

    def test_job_retention_invokes_retention_safely(self):
        with mock.patch("core.scheduler.run_retention") as mock_retention:
            _job_retention(self.conn)
            mock_retention.assert_called_once_with(self.conn)


if __name__ == "__main__":
    unittest.main()
