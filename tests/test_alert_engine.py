"""
Unit & integration tests for PrintGuard Alert Engine (Phase J.11).
Tests alert aggregation from logs/toner/sensors/security, filters,
suggested actions, and REST endpoints.
"""

import os
import unittest
from datetime import datetime, timezone

from core.db.connection import connect
from core.dashboard.manager import bootstrap_dashboard_tables
from core.alerting.alert_service import (
    get_active_alerts,
    get_alert_summary,
    get_alert_by_id,
    _resolve_suggested_action,
)
from web import create_app


class TestAlertEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SECRET_KEY"] = "test-secret-phase-j-alerts"
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

        # Bootstrap DB and seed test alerts
        with connect() as conn:
            bootstrap_dashboard_tables(conn)
            now_iso = datetime.now(timezone.utc).isoformat()

            # Seed a log alert
            conn.execute(
                """
                INSERT INTO logs (printer_ip, printer_name, timestamp, type, message, severity, code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("172.16.25.10", "HP LaserJet Pro", now_iso, "ALERT", "درب جلویی باز است", "critical", "DOOR_OPEN"),
            )
            # Seed another log event (warning)
            conn.execute(
                """
                INSERT INTO logs (printer_ip, printer_name, timestamp, type, message, severity, code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("172.16.25.20", "Toshiba Studio", now_iso, "STATUS", "دستگاه آفلاین شد", "warning", "NET_OFFLINE"),
            )
            # Seed security event
            conn.execute(
                """
                INSERT INTO security_events (timestamp, event_type, ip_address, user_identifier, severity, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso, "login_failed", "192.168.1.100", "attacker", "critical", "Multiple failed logins", now_iso),
            )
            conn.commit()

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

        # Check required fields
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
            sess["_user_id"] = "1"

        res = self.client.get("/api/alerts/active?severity=all")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIsInstance(data["alerts"], list)

    def test_api_alert_summary(self):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = "1"

        res = self.client.get("/api/alerts/summary")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("summary", data)


if __name__ == "__main__":
    unittest.main()
