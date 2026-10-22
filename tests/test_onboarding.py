"""
Unit & integration tests for PrintGuard Onboarding Tours (Phase J.9).
Tests tour completion, dismissal, global disabling, reset, and REST APIs.
"""

import os
import unittest

from core.db.connection import connect
from core.dashboard.manager import bootstrap_dashboard_tables
from core.dashboard.onboarding import (
    get_user_tours_status,
    set_tour_status,
    set_user_onboarding_disabled,
    reset_user_tours,
    KNOWN_TOURS,
)
from web import create_app


class TestOnboarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["SECRET_KEY"] = "test-secret-phase-j-onboarding"
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.app.config["WTF_CSRF_ENABLED"] = False
        cls.client = cls.app.test_client()

        with connect() as conn:
            bootstrap_dashboard_tables(conn)

    def setUp(self):
        # Reset test user
        reset_user_tours(user_id=42)

    def test_default_status(self):
        status = get_user_tours_status(user_id=42)
        self.assertFalse(status["disabled_all"])
        for tour in KNOWN_TOURS:
            self.assertIn(tour, status["completed"])
            self.assertFalse(status["completed"][tour])
            self.assertFalse(status["dismissed"][tour])

    def test_mark_tour_completed_and_dismissed(self):
        # Complete tour 1
        ok = set_tour_status(user_id=42, tour_name="dashboard_intro", completed=True)
        self.assertTrue(ok)

        # Dismiss tour 2
        ok2 = set_tour_status(user_id=42, tour_name="dashboard_builder", completed=False, dismissed=True)
        self.assertTrue(ok2)

        status = get_user_tours_status(user_id=42)
        self.assertTrue(status["completed"]["dashboard_intro"])
        self.assertFalse(status["dismissed"]["dashboard_intro"])

        self.assertFalse(status["completed"]["dashboard_builder"])
        self.assertTrue(status["dismissed"]["dashboard_builder"])

    def test_disable_and_enable_all(self):
        set_user_onboarding_disabled(user_id=42, disabled=True)
        status = get_user_tours_status(user_id=42)
        self.assertTrue(status["disabled_all"])

        set_user_onboarding_disabled(user_id=42, disabled=False)
        status2 = get_user_tours_status(user_id=42)
        self.assertFalse(status2["disabled_all"])

    def test_reset_tours(self):
        set_tour_status(user_id=42, tour_name="panel_creation", completed=True)
        self.assertTrue(get_user_tours_status(user_id=42)["completed"]["panel_creation"])

        reset_user_tours(user_id=42, tour_name="panel_creation")
        self.assertFalse(get_user_tours_status(user_id=42)["completed"]["panel_creation"])

    def test_api_onboarding_endpoints(self):
        from models import User
        u = User.find_by_email("onboard@test.io")
        if not u:
            u = User.create(
                username="onboard_tester",
                email="onboard@test.io",
                password="secret_pass_123",
                role="admin",
                is_verified=True,
            )
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True

        # 1. Get status
        res = self.client.get("/api/onboarding/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("dashboard_intro", data["completed"])

        # 2. Complete tour via API
        post_res = self.client.post("/api/onboarding/complete", json={
            "tour": "dashboard_intro",
            "action": "complete",
        })
        self.assertEqual(post_res.status_code, 200)
        self.assertTrue(post_res.get_json()["success"])

        # Verify status
        res2 = self.client.get("/api/onboarding/status")
        self.assertTrue(res2.get_json()["completed"]["dashboard_intro"])

        # 3. Dismiss tour via API
        post_res2 = self.client.post("/api/onboarding/complete", json={
            "tour": "datasource_connect",
            "action": "dismiss",
        })
        self.assertEqual(post_res2.status_code, 200)

        # 4. Disable all via API
        post_dis = self.client.post("/api/onboarding/complete", json={
            "action": "disable_all",
        })
        self.assertEqual(post_dis.status_code, 200)
        self.assertTrue(post_dis.get_json()["disabled_all"])

        # 5. Reset via API
        post_reset = self.client.post("/api/onboarding/reset", json={})
        self.assertEqual(post_reset.status_code, 200)
        res3 = self.client.get("/api/onboarding/status")
        self.assertFalse(res3.get_json()["disabled_all"])


if __name__ == "__main__":
    unittest.main()
