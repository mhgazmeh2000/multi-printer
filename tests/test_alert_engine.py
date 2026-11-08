"""
Unit & integration tests for PrintGuard Alert Engine (Phase J.11).
Tests alert aggregation from logs/toner/sensors/security, filters,
suggested actions, and REST endpoints.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from core.alerting.alert_service import (
    _resolve_suggested_action,
    get_active_alerts,
    get_alert_by_id,
    get_alert_summary,
)
from core.database import init_db
from core.db.connection import connect
from core.security_audit import init_security_audit
from models import User
from web import create_app


class TestAlertEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["SECRET_KEY"] = "test-secret-phase-j-alerts"
        if not os.getenv("PG_DSN") and not os.getenv("DATABASE_URL"):
            os.environ["DB_BACKEND"] = "sqlite"

        init_db()
        init_security_audit()

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.viewer = User.create(
            username="alert_viewer",
            email="alerts@printguard.org",
            password="secret_pass_123",
            role="viewer",
            is_verified=True,
        )

        with connect() as conn:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO logs (printer_ip, printer_name, timestamp, type, message, severity, code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("172.16.25.10", "HP LaserJet Pro", now_iso, "ALERT", "درب جلویی باز است", "critical", "DOOR_OPEN"),
            )
            conn.execute(
                """
                INSERT INTO logs (printer_ip, printer_name, timestamp, type, message, severity, code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("172.16.25.20", "Toshiba Studio", now_iso, "STATUS", "دستگاه آفلاین شد", "warning", "NET_OFFLINE"),
            )
            conn.execute(
                """
                INSERT INTO security_events (timestamp, event_type, ip_address, user_identifier, severity, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso, "login_failed", "192.168.1.100", "attacker", "critical", "Multiple failed logins", now_iso),
            )
            conn.commit()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_suggested_action_resolution(self):
        act_toner = _resolve_suggested_action("toner", "TONER_LOW", "کاهش سطح کارتریج")
        self.assertIn("کارتریج", act_toner)

        act_door = _resolve_suggested_action("hardware", "DOOR_OPEN", "درب پرینتر باز است")
        self.assertIn("درب", act_door)

        act_offline = _resolve_suggested_action("connectivity", "NET_OFFLINE", "دستگاه آفلاین شد")
        self.assertIn("شبکه", act_offline)

        act_sec = _resolve_suggested_action("security", "login_failed", "login")
        self.assertIn("امنیت", act_sec)

    def test_get_active_alerts(self):
        alerts = get_active_alerts(limit=20, since_hours=48)
        self.assertIsInstance(alerts, list)
        self.assertGreaterEqual(len(alerts), 2)

        first = alerts[0]
        self.assertIn("id", first)
        self.assertIn("title", first)
        self.assertIn("severity", first)
        self.assertIn("suggested_action", first)
        self.assertIn("timestamp", first)

    def test_severity_filter(self):
        critical_alerts = get_active_alerts(severity="critical", limit=20)
        for a in critical_alerts:
            self.assertEqual(a["severity"], "critical")

        warning_alerts = get_active_alerts(severity="warning", limit=20)
        for a in warning_alerts:
            self.assertEqual(a["severity"], "warning")

    def test_printer_filter(self):
        hp_alerts = get_active_alerts(printer_ip="172.16.25.10", limit=10)
        self.assertTrue(all(a["printer_ip"] == "172.16.25.10" for a in hp_alerts))

    def test_alert_summary(self):
        summary = get_alert_summary(since_hours=48)
        self.assertIn("total", summary)
        self.assertIn("critical", summary)
        self.assertIn("warning", summary)
        self.assertIn("by_category", summary)
        self.assertGreaterEqual(summary["total"], 2)

    def test_get_alert_by_id(self):
        alerts = get_active_alerts(limit=10)
        if alerts:
            aid = alerts[0]["id"]
            found = get_alert_by_id(aid)
            self.assertIsNotNone(found)
            self.assertEqual(found["id"], aid)

    def test_api_active_alerts(self):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.viewer.id)
            sess["_fresh"] = True

        res = self.client.get("/api/alerts/active?severity=all")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["alerts"], list)

    def test_api_alert_summary(self):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.viewer.id)
            sess["_fresh"] = True

        res = self.client.get("/api/alerts/summary")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("summary", data)


if __name__ == "__main__":
    unittest.main()
