# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_rbac_admin_guard.py
# @brief:        Tests for role-based access control on admin routes (task 3, phase C).
# @details:      Responsibilities: Verify that admin-only routes reject non-admin users.
#                Main API: RbacAdminGuardTests
#                Side effects: none at import time
# @dependencies: internal: core.database, models, web | external: pytest
# @notes:        Can be run directly as a script.
# =============================================================================

"""
RBAC hardening tests (task 3 phase C).

Three defects closed:
  3.1) The manager could demote/remove/reset the admin
       (In `web/routes/users.py` added guard "Effect on admin only with admin").
  3.2) `toner_reset` had no guard decorator (now `@admin_required`).
  3.3) The first automatic registration was admin; Now lockable with `ADMIN_SETUP_TOKEN`.

Five tests according to work contract 3:
  1) manager → demote admin ⇒ 403
  2) admin → demoting admin (with 2 admins) ⇒ 200
  3) viewer → toner_reset ⇒ 403
  4) First registration with wrong ADMIN_SETUP_TOKEN ⇒ 403
  5) First registration without ADMIN_SETUP_TOKEN ⇒ admin (current behavior)
"""
import os
import tempfile
import unittest
from unittest import mock

import pytest

from core.database import init_db
from models import User
from web import create_app

# This file is only run with pytest (as part of CI test path 21).
# load_tests is empty ⇒ `python -m unittest discover` ignores this file and
# Tests are not counted twice.
pytestmark = pytest.mark.pytest_only


def load_tests(loader, tests, pattern):  # noqa: ARG001 - unittest protocol signature
    """Unittest protocol: This file is not detected/executed by unittest."""
    return unittest.TestSuite()


def _login(client, user):
    """Helper: login."""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


class RbacAdminGuardTests(unittest.TestCase):

    """Tests for rbac admin guard."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                 # Relative DB_PATH ⇒ Temporary DB
        init_db()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()

        self.admin = User.create(username="admin1", email="admin1@test.io",
                                 password="secret123", role="admin", is_verified=True)
        self.admin2 = User.create(username="admin2", email="admin2@test.io",
                                  password="secret123", role="admin", is_verified=True)
        self.manager = User.create(username="manager1", email="manager1@test.io",
                                   password="secret123", role="manager", is_verified=True)
        self.viewer = User.create(username="viewer1", email="viewer1@test.io",
                                  password="secret123", role="viewer", is_verified=True)

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _fresh(self, user_id):
        """Helper: fresh."""
        return User.get(user_id)

    # ── 1) manager cannot demote admin
    def test_1_manager_cannot_demote_admin(self):
        """Verifies 1 manager cannot demote admin."""
        _login(self.client, self.manager)
        r = self.client.post(f"/api/users/{self.admin.id}/role", json={"role": "viewer"})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(self._fresh(self.admin.id).role, "admin",
                         "نقش ادمین نباید تغییر کرده باشد")

        # Deleting and resetting the admin password is also prohibited for the manager
        r = self.client.delete(f"/api/users/{self.admin.id}")
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIsNotNone(self._fresh(self.admin.id), "ادمین نباید حذف شده باشد")

        r = self.client.post(f"/api/users/{self.admin.id}/reset-password", json={})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    # ── 2) admin can demote admin (with ≥2 admins) ─────────────────────
    def test_2_admin_can_demote_admin_when_multiple_admins(self):
        """Verifies 2 admin can demote admin when multiple admins."""
        _login(self.client, self.admin)
        r = self.client.post(f"/api/users/{self.admin2.id}/role", json={"role": "manager"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["user"]["role"], "manager")
        self.assertEqual(self._fresh(self.admin2.id).role, "manager")

    # ── 3) viewer can't reset toner
    def test_3_viewer_cannot_reset_toner(self):
        """Verifies 3 viewer cannot reset toner."""
        _login(self.client, self.viewer)
        r = self.client.post("/api/printer/10.0.0.5/toner_reset",
                             json={"color": "black", "level": 100})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

        # Positive control: the same request with manager is also 403 (current policy: admin-only)
        _login(self.client, self.manager)
        r = self.client.post("/api/printer/10.0.0.5/toner_reset",
                             json={"color": "black", "level": 100})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    # ── 4) First user registration with wrong token ⇒ 403 ────────────────────────────
    # "first registration" assumes the DB is empty; On PG, all tests are one scheme
    # They see a session (setUp this class created 4 users) ⇒ sqlite_only.
    @pytest.mark.sqlite_only
    @mock.patch("web.auth.verify_captcha", return_value=True)  # CAPTCHA is not under test here
    def test_4_first_register_requires_matching_setup_token(self, _captcha):
        """Verifies 4 first register requires matching setup token."""
        with tempfile.TemporaryDirectory() as tmp2:
            old = os.getcwd()
            os.chdir(tmp2)
            try:
                init_db()                        # DB is empty ⇒ no user exists
                app = create_app()
                app.config["WTF_CSRF_ENABLED"] = False
                client = app.test_client()

                with mock.patch.dict(os.environ, {"ADMIN_SETUP_TOKEN": "s3cret-token"}):
                    bad = client.post("/register", data={
                        "username": "firstuser", "email": "first@test.io",
                        "password": "secret123", "confirm_password": "secret123",
                        "admin_setup_token": "wrong-token",
                    })
                    self.assertEqual(bad.status_code, 403, bad.get_data(as_text=True))
                    self.assertIsNone(User.find_by_identifier("firstuser"),
                                      "با توکن اشتباه نباید کاربری ساخته شود")

                    good = client.post("/register", data={
                        "username": "firstuser", "email": "first@test.io",
                        "password": "secret123", "confirm_password": "secret123",
                        "admin_setup_token": "s3cret-token",
                    })
                    self.assertEqual(good.status_code, 302, good.get_data(as_text=True))
                    created = User.find_by_identifier("firstuser")
                    self.assertIsNotNone(created)
                    self.assertEqual(created.role, "admin")
            finally:
                os.chdir(old)

    # ── 5) Without ADMIN_SETUP_TOKEN: current behavior (first user = admin) ─────────
    # "first registration" assumes the DB is empty; On PG, all tests are one scheme
    # They see a session (setUp this class created 4 users) ⇒ sqlite_only.
    @pytest.mark.sqlite_only
    @mock.patch("web.auth.verify_captcha", return_value=True)  # CAPTCHA is not under test here
    def test_5_first_register_without_token_keeps_current_behaviour(self, _captcha):
        """Verifies 5 first register without token keeps current behaviour."""
        with tempfile.TemporaryDirectory() as tmp2:
            old = os.getcwd()
            os.chdir(tmp2)
            try:
                init_db()
                app = create_app()
                app.config["WTF_CSRF_ENABLED"] = False
                client = app.test_client()

                env = {k: v for k, v in os.environ.items() if k != "ADMIN_SETUP_TOKEN"}
                with mock.patch.dict(os.environ, env, clear=True):
                    r = client.post("/register", data={
                        "username": "solo", "email": "solo@test.io",
                        "password": "secret123", "confirm_password": "secret123",
                    })
                self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
                created = User.find_by_identifier("solo")
                self.assertIsNotNone(created)
                self.assertEqual(created.role, "admin", "اولین کاربر باید admin شود")
                self.assertTrue(created.is_verified, "اولین کاربر باید تأییدشده باشد")
            finally:
                os.chdir(old)


    # ── 6) Registration form with ADMIN_SETUP_TOKEN: Token field is displayed ─────
    def test_6_register_form_shows_setup_token_field_when_configured(self):
        """Verifies 6 register form shows setup token field when configured."""
        with tempfile.TemporaryDirectory() as tmp2:
            old = os.getcwd()
            os.chdir(tmp2)
            try:
                init_db()
                app = create_app()
                app.config["WTF_CSRF_ENABLED"] = False
                client = app.test_client()

                with mock.patch.dict(os.environ, {"ADMIN_SETUP_TOKEN": "s3cret-token"}):
                    r = client.get("/register")
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                body = r.get_data(as_text=True)
                self.assertIn('name="admin_setup_token"', body,
                              "با تنظیم ADMIN_SETUP_TOKEN، فیلد توکن باید در فرم باشد")
                self.assertIn("توکن راه‌اندازی ادمین", body)
            finally:
                os.chdir(old)

    # ── 7) Without ADMIN_SETUP_TOKEN: The token field is not displayed ─────────────
    def test_7_register_form_hides_setup_token_field_without_token(self):
        """Verifies 7 register form hides setup token field without token."""
        with tempfile.TemporaryDirectory() as tmp2:
            old = os.getcwd()
            os.chdir(tmp2)
            try:
                init_db()
                app = create_app()
                app.config["WTF_CSRF_ENABLED"] = False
                client = app.test_client()

                with mock.patch.dict(os.environ, {"ADMIN_SETUP_TOKEN": ""}):
                    r = client.get("/register")
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                body = r.get_data(as_text=True)
                self.assertNotIn('name="admin_setup_token"', body,
                                 "بدون ADMIN_SETUP_TOKEN نباید فیلد توکن دیده شود")
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
