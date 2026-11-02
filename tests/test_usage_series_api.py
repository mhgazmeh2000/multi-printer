# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_usage_series_api.py
# @brief:        Tests for /api/stats/usage/series (color / black-white print volume).
# @details:      Responsibilities: color/B&W/mixed/unknown classification, per-bucket totals,
#                zero-filled continuous axis, automatic bucket choice, range parsing,
#                validation errors and office-based access control.
#                Main API: UsageSplitTests, UsageSeriesApiTests
#                Side effects: temporary SQLite database in a temp directory
# @dependencies: internal: core.database, core.db, web | external: unittest
# @notes:        Sessions are injected like in test_sensor_series_api.py.
# =============================================================================

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core import store
from core.database import init_db
from core.db import get_connection
from core.security_audit import init_security_audit
from core.time_utils import local_tz
from web import create_app
from web.auth import User
from web.routes.stats import _usage_split

IP_A, IP_B = "172.16.25.10", "172.16.25.11"
MIXED_MSG = "12 صفحه رنگی + 30 صفحه سیاه‌سفید = 42 صفحه چاپ شد"


class UsageSplitTests(unittest.TestCase):
    def test_pure_color_and_black_white(self):
        self.assertEqual(_usage_split("رنگی", 5, ""), (5, 0, 0))
        self.assertEqual(_usage_split("سیاه‌سفید", 4, ""), (0, 4, 0))
        self.assertEqual(_usage_split("سیاه\u200cسفید", 4, ""), (0, 4, 0))   # with ZWNJ
        self.assertEqual(_usage_split("سیاهسفید", 4, ""), (0, 4, 0))          # without ZWNJ

    def test_mixed_is_split_from_the_message(self):
        self.assertEqual(_usage_split("مختلط", 42, MIXED_MSG), (12, 30, 0))
        self.assertEqual(_usage_split("مختلط", 42, "۱۲ صفحه رنگی + ۳۰ صفحه سیاه‌سفید"), (12, 30, 0))  # Persian digits

    def test_unparsable_or_inconsistent_mixed_is_unknown_not_guessed(self):
        self.assertEqual(_usage_split("مختلط", 10, "garbage"), (0, 0, 10))
        self.assertEqual(_usage_split("مختلط", 10, MIXED_MSG), (0, 0, 10))    # 12+30 > 10

    def test_unknown_and_empty_values(self):
        self.assertEqual(_usage_split("نامشخص", 7, ""), (0, 0, 7))
        self.assertEqual(_usage_split(None, 3, ""), (0, 0, 3))
        self.assertEqual(_usage_split("رنگی", 0, ""), (0, 0, 0))
        self.assertEqual(_usage_split("رنگی", None, ""), (0, 0, 0))


class UsageSeriesApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["DB_BACKEND"] = "sqlite"
        init_db()
        init_security_audit()
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.admin = User.create("usage_admin", "usage@printguard.org", "pass12345", role="admin", is_verified=True)
        self._login(self.admin)

        tz = local_tz()
        # Anchored on *yesterday* so the tests do not depend on the time of day they run at.
        self.noon = (datetime.now(tz) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        # yesterday:       A color 10 · A B/W 20 · B mixed (12+30) · A unknown 5
        # day before:      A B/W 7
        self._add(IP_A, self.noon - timedelta(hours=3), "رنگی", 10, "10 صفحه رنگی چاپ شد")
        self._add(IP_A, self.noon - timedelta(hours=2), "سیاه‌سفید", 20, "20 صفحه سیاه‌سفید چاپ شد")
        self._add(IP_B, self.noon - timedelta(hours=1), "مختلط", 42, MIXED_MSG)
        self._add(IP_A, self.noon - timedelta(hours=4), "نامشخص", 5, "5 صفحه چاپ شد (تفکیک رنگ نامطمئن)")
        self._add(IP_A, self.noon - timedelta(days=1), "سیاه‌سفید", 7, "7 صفحه سیاه‌سفید چاپ شد")
        # must never be counted: non-PRINT event, zero pages, older than any tested window
        self._add(IP_A, self.noon, "رنگی", 99, "x", etype="ALERT")
        self._add(IP_A, self.noon, "رنگی", 0, "x")
        self._add(IP_A, self.noon - timedelta(days=500), "رنگی", 1000, "old")

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def _add(self, ip, local_dt, color, pages, message, etype="PRINT"):
        ts = local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        conn = get_connection(timeout=10.0)
        try:
            conn.execute(
                "INSERT INTO logs (printer_ip, printer_name, timestamp, type, message, pages, color, severity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ip, "T", ts, etype, message, pages, color, "info"),
            )
            conn.commit()
        finally:
            conn.close()

    def _get(self, query):
        return self.client.get(f"/api/stats/usage/series?{query}")

    def test_days_range_splits_color_bw_and_unknown(self):
        body = self._get("days=7").get_json()
        self.assertEqual(body["bucket"], "day")
        self.assertEqual(len(body["labels"]), 7)
        for key in ("color", "bw", "unknown", "total"):
            self.assertEqual(len(body[key]), 7)
        # yesterday is the second-to-last bucket (today is the last one and is empty)
        self.assertEqual((body["color"][-2], body["bw"][-2], body["unknown"][-2]), (10 + 12, 20 + 30, 5))
        self.assertEqual(body["total"][-2], 77)
        self.assertEqual(body["total"][-1], 0)
        self.assertEqual(body["bw"][-3], 7)
        self.assertEqual(body["summary"]["total"], 84)
        self.assertEqual(body["summary"]["color"] + body["summary"]["bw"] + body["summary"]["unknown"], 84)
        self.assertEqual(body["summary"]["peak"]["value"], 77)

    def test_empty_buckets_are_real_zeros(self):
        body = self._get("days=30").get_json()
        self.assertEqual(len(body["labels"]), 30)
        self.assertEqual(body["total"][:20], [0] * 20)
        self.assertEqual(body["total"][-1], 0)

    def test_bucket_is_chosen_from_the_span(self):
        self.assertEqual(self._get("hours=24").get_json()["bucket"], "hour")
        self.assertEqual(self._get("days=90").get_json()["bucket"], "day")
        week = self._get("days=365").get_json()
        self.assertEqual(week["bucket"], "week")
        self.assertLessEqual(len(week["labels"]), 54)
        self.assertEqual(self._get("days=7&bucket=hour").get_json()["bucket"], "hour")

    def test_hourly_buckets_hold_the_right_hour(self):
        body = self._get("hours=72&bucket=hour").get_json()
        hour_label = (self.noon - timedelta(hours=2)).strftime("%Y-%m-%dT%H:00")
        self.assertEqual(body["bw"][body["labels"].index(hour_label)], 20)

    def test_printer_filter(self):
        body = self._get(f"days=7&ip={IP_B}").get_json()
        self.assertEqual(body["printer_ip"], IP_B)
        self.assertEqual((body["summary"]["color"], body["summary"]["bw"]), (12, 30))

    def test_custom_range_naive_values_are_tehran_local_time(self):
        start = (self.noon - timedelta(days=1, hours=1)).strftime("%Y-%m-%dT%H:%M")
        end = (self.noon + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M")
        body = self._get(f"start={start}&end={end}").get_json()
        self.assertEqual(body["summary"]["total"], 84)

    def test_validation_errors(self):
        self.assertEqual(self._get("start=garbage").status_code, 400)
        self.assertEqual(self._get("start=2026-01-02&end=2026-01-01").status_code, 400)

    def test_viewer_role_may_read_the_series(self):
        viewer = User.create("usage_viewer", "viewer@printguard.org", "pass12345", role="viewer", is_verified=True)
        self._login(viewer)
        self.assertEqual(self._get("days=7").status_code, 200)

    def test_office_restricted_user_is_limited_to_own_printers(self):
        saved = list(store.PRINTERS)
        store.PRINTERS[:] = [
            {"ip": IP_A, "name": "A", "group": "office-a", "device_type": "printer"},
            {"ip": IP_B, "name": "B", "group": "office-b", "device_type": "printer"},
        ]
        try:
            user = User.create("usage_office", "office@printguard.org", "pass12345", role="manager",
                               is_verified=True, allowed_offices=["office-a"])
            self._login(user)
            own = self._get("days=7").get_json()                      # all printers -> only A
            self.assertEqual((own["summary"]["color"], own["summary"]["bw"], own["summary"]["unknown"]), (10, 27, 5))
            self.assertEqual(self._get(f"days=7&ip={IP_B}").status_code, 403)
            self.assertEqual(self._get(f"days=7&ip={IP_A}").status_code, 200)
        finally:
            store.PRINTERS[:] = saved


if __name__ == "__main__":
    unittest.main()
