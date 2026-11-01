# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_sensor_series_api.py
# @brief:        Tests for /api/stats/sensor/series (sensor time-series with free range filter).
# @details:      Responsibilities: range presets (hours), custom start/end (Tehran local),
#                bucket selection, validation errors and summary values.
#                Main API: SensorSeriesApiTests
#                Side effects: temporary SQLite database in a temp directory
# @dependencies: internal: core.database, core.db, web | external: unittest
# @notes:        Uses a session cookie for an admin user (same pattern as test_dashboard_builder).
# =============================================================================

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.database import init_db
from core.db import get_connection
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User

IP = "10.0.0.9"


class SensorSeriesApiTests(unittest.TestCase):
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
        admin = User.create("series_admin", "series@printguard.org", "pass12345", role="admin", is_verified=True)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(admin.id)
            sess["_fresh"] = True

        self.now = datetime.now(timezone.utc)
        conn = get_connection(timeout=10.0)
        try:
            for i in range(0, 60 * 6):  # 6 hours, one reading per minute
                ts = (self.now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
                for kind, value in (("temperature", 20.0 + (i % 10)), ("humidity", 50.0)):
                    conn.execute(
                        "INSERT INTO sensor_readings (printer_ip, timestamp, port, kind, value, unit, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (IP, ts, 1, kind, value, "", "active"),
                    )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _get(self, query):
        return self.client.get(f"/api/stats/sensor/series?{query}")

    def test_hours_preset_returns_aligned_arrays(self):
        res = self._get(f"ip={IP}&hours=1")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["bucket_seconds"], 60)
        self.assertGreaterEqual(len(body["timestamps"]), 55)
        self.assertEqual(len(body["timestamps"]), len(body["temperature"]))
        self.assertEqual(len(body["timestamps"]), len(body["humidity"]))
        self.assertEqual(body["summary"]["humidity"]["avg"], 50.0)

    def test_longer_range_uses_coarser_bucket(self):
        short = self._get(f"ip={IP}&hours=1").get_json()["bucket_seconds"]
        long_ = self._get(f"ip={IP}&hours=720").get_json()["bucket_seconds"]
        self.assertGreater(long_, short)

    def test_explicit_bucket_is_honoured_and_unknown_bucket_falls_back(self):
        self.assertEqual(self._get(f"ip={IP}&hours=6&bucket=3600").get_json()["bucket_seconds"], 3600)
        self.assertEqual(self._get(f"ip={IP}&hours=1&bucket=7").get_json()["bucket_seconds"], 60)

    def test_custom_range_naive_values_are_tehran_local_time(self):
        tehran = timezone(timedelta(hours=3, minutes=30))
        start = (self.now - timedelta(hours=3)).astimezone(tehran).strftime("%Y-%m-%dT%H:%M")
        end = (self.now - timedelta(hours=1)).astimezone(tehran).strftime("%Y-%m-%dT%H:%M")
        body = self._get(f"ip={IP}&start={start}&end={end}").get_json()
        self.assertTrue(110 <= len(body["timestamps"]) <= 125, len(body["timestamps"]))

    def test_validation_errors(self):
        self.assertEqual(self._get("hours=1").status_code, 400)                              # ip required
        self.assertEqual(self._get(f"ip={IP}&start=garbage").status_code, 400)               # unparsable
        self.assertEqual(self._get(f"ip={IP}&start=2026-01-02&end=2026-01-01").status_code, 400)

    def test_unknown_port_or_ip_returns_empty_series(self):
        for query in (f"ip={IP}&hours=1&port=9", "ip=10.9.9.9&hours=1"):
            body = self._get(query).get_json()
            self.assertEqual(body["timestamps"], [])
            self.assertEqual(body["summary"], {"temperature": None, "humidity": None})


if __name__ == "__main__":
    unittest.main()
