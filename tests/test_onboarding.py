"""
Unit & integration tests for PrintGuard Onboarding Tours (Phase J.9).
Tests tour completion, dismissal, global disabling, reset, PostgreSQL meta storage, and REST APIs.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from core.database import init_db
from core.db.connection import BACKEND_POSTGRESQL, ConnectionAdapter
from core.dashboard.onboarding import (
    KNOWN_TOURS,
    get_user_tours_status,
    reset_user_tours,
    set_tour_status,
    set_user_onboarding_disabled,
)
from core.security_audit import init_security_audit
from models import User
from web import create_app


class TestOnboarding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["SECRET_KEY"] = "test-secret-phase-j-onboarding"
        if not os.getenv("PG_DSN") and not os.getenv("DATABASE_URL"):
            os.environ["DB_BACKEND"] = "sqlite"

        init_db()
        init_security_audit()

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        reset_user_tours(user_id=42)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_default_status(self):
        status = get_user_tours_status(user_id=42)
        self.assertFalse(status["disabled_all"])
        for tour in KNOWN_TOURS:
            self.assertIn(tour, status["completed"])
            self.assertFalse(status["completed"][tour])
            self.assertFalse(status["dismissed"][tour])

    def test_mark_tour_completed_and_dismissed(self):
        ok = set_tour_status(user_id=42, tour_name="dashboard_intro", completed=True)
        self.assertTrue(ok)

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

    def test_postgres_backend_persists_in_meta_table(self):
        """When conn.backend == BACKEND_POSTGRESQL, onboarding state uses the `meta` table cleanly."""
        raw = sqlite3.connect(":memory:")
        raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")

        class _PgMetaConn:
            backend = BACKEND_POSTGRESQL

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                return raw.execute(sql, params)

            def commit(self):
                raw.commit()

        try:
            with mock.patch("core.dashboard.onboarding.connect", side_effect=lambda: _PgMetaConn()):
                self.assertTrue(set_tour_status(user_id=7, tour_name="dashboard_intro", completed=True))
                self.assertTrue(set_user_onboarding_disabled(user_id=7, disabled=True))
                st = get_user_tours_status(user_id=7)
                self.assertTrue(st["completed"]["dashboard_intro"])
                self.assertTrue(st["disabled_all"])

                self.assertTrue(reset_user_tours(user_id=7, tour_name="dashboard_intro"))
                st2 = get_user_tours_status(user_id=7)
                self.assertFalse(st2["completed"]["dashboard_intro"])
                self.assertTrue(st2["disabled_all"])

                self.assertTrue(reset_user_tours(user_id=7))
                st3 = get_user_tours_status(user_id=7)
                self.assertFalse(st3["disabled_all"])
        finally:
            raw.close()

    def test_api_onboarding_endpoints_for_admin_and_viewer(self):
        u = User.create(
            username="onboard_tester",
            email="onboard@test.io",
            password="secret_pass_123",
            role="viewer",
            is_verified=True,
        )
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True

        # 1. Get status (viewer role must get 200 OK, not 403/404)
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
        post_rst = self.client.post("/api/onboarding/reset", json={})
        self.assertEqual(post_rst.status_code, 200)
        res3 = self.client.get("/api/onboarding/status")
        self.assertFalse(res3.get_json()["disabled_all"])


if __name__ == "__main__":
    unittest.main()
