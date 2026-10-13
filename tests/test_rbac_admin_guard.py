# -*- coding: utf-8 -*-
"""
تست‌های سخت‌سازی RBAC (کار ۳ فاز C).

سه نقص بسته‌شده:
  ۳.۱) manager می‌توانست ادمین را تنزل دهد / حذف کند / رمزش را بازنشانی کند
       (در `web/routes/users.py` گارد «اثر روی admin فقط با admin» اضافه شد).
  ۳.۲) `toner_reset` هیچ decorator محافظی نداشت (اکنون `@admin_required`).
  ۳.۳) اولین ثبت‌نام خودکار admin می‌شد؛ اکنون با `ADMIN_SETUP_TOKEN` قابل قفل‌کردن است.

پنج تست طبق قرارداد کار ۳:
  ۱) manager → تنزل ادمین ⇒ 403
  ۲) admin → تنزل ادمین (با ۲ ادمین) ⇒ 200
  ۳) viewer → toner_reset ⇒ 403
  ۴) اولین ثبت‌نام با ADMIN_SETUP_TOKEN اشتباه ⇒ 403
  ۵) اولین ثبت‌نام بدون ADMIN_SETUP_TOKEN ⇒ admin (رفتار فعلی)
"""
import os
import tempfile
import unittest
from unittest import mock

import pytest

from core.database import init_db
from models import User
from web import create_app

# این فایل فقط با pytest اجرا میشود (به‌عنوان بخشی از مسیر ۲۱ تستی CI).
# load_tests خالی ⇒ `python -m unittest discover` این فایل را کنار میگذارد و
# تستها دوبار شمرده نمیشوند.
pytestmark = pytest.mark.pytest_only


def load_tests(loader, tests, pattern):  # noqa: ARG001 - امضای پروتکل unittest
    """پروتکل unittest: این فایل با unittest کشف/اجرا نمیشود."""
    return unittest.TestSuite()


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


class RbacAdminGuardTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                 # DB_PATH نسبی ⇒ DB موقت
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
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _fresh(self, user_id):
        return User.get(user_id)

    # ── ۱) manager نمی‌تواند ادمین را تنزل دهد ─────────────────────────────
    def test_1_manager_cannot_demote_admin(self):
        _login(self.client, self.manager)
        r = self.client.post(f"/api/users/{self.admin.id}/role", json={"role": "viewer"})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertEqual(self._fresh(self.admin.id).role, "admin",
                         "نقش ادمین نباید تغییر کرده باشد")

        # حذف و بازنشانی رمز ادمین هم برای manager ممنوع است
        r = self.client.delete(f"/api/users/{self.admin.id}")
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))
        self.assertIsNotNone(self._fresh(self.admin.id), "ادمین نباید حذف شده باشد")

        r = self.client.post(f"/api/users/{self.admin.id}/reset-password", json={})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    # ── ۲) admin می‌تواند ادمین را تنزل دهد (با ≥۲ ادمین) ────────────────────
    def test_2_admin_can_demote_admin_when_multiple_admins(self):
        _login(self.client, self.admin)
        r = self.client.post(f"/api/users/{self.admin2.id}/role", json={"role": "manager"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["user"]["role"], "manager")
        self.assertEqual(self._fresh(self.admin2.id).role, "manager")

    # ── ۳) viewer نمی‌تواند toner_reset کند ─────────────────────────────────
    def test_3_viewer_cannot_reset_toner(self):
        _login(self.client, self.viewer)
        r = self.client.post("/api/printer/10.0.0.5/toner_reset",
                             json={"color": "black", "level": 100})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

        # کنترل مثبت: همان درخواست با manager هم 403 است (سیاست فعلی: admin-only)
        _login(self.client, self.manager)
        r = self.client.post("/api/printer/10.0.0.5/toner_reset",
                             json={"color": "black", "level": 100})
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    # ── ۴) ثبت‌نام اولین کاربر با توکن اشتباه ⇒ 403 ──────────────────────────
    # «اولین ثبت‌نام» فرض می‌کند DB خالی است؛ روی PG همهٔ تست‌ها یک اسکیمای
    # جلسه‌ای را می‌بینند (setUp همین کلاس ۴ کاربر ساخته) ⇒ sqlite_only.
    @pytest.mark.sqlite_only
    def test_4_first_register_requires_matching_setup_token(self):
        with tempfile.TemporaryDirectory() as tmp2:
            old = os.getcwd()
            os.chdir(tmp2)
            try:
                init_db()                        # DB خالی ⇒ هیچ کاربری وجود ندارد
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

    # ── ۵) بدون ADMIN_SETUP_TOKEN: رفتار فعلی (اولین کاربر = admin) ─────────
    # «اولین ثبت‌نام» فرض می‌کند DB خالی است؛ روی PG همهٔ تست‌ها یک اسکیمای
    # جلسه‌ای را می‌بینند (setUp همین کلاس ۴ کاربر ساخته) ⇒ sqlite_only.
    @pytest.mark.sqlite_only
    def test_5_first_register_without_token_keeps_current_behaviour(self):
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


    # ── ۶) فرم ثبت‌نام با ADMIN_SETUP_TOKEN: فیلد توکن نمایش داده می‌شود ─────
    def test_6_register_form_shows_setup_token_field_when_configured(self):
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

    # ── ۷) بدون ADMIN_SETUP_TOKEN: فیلد توکن نمایش داده نمی‌شود ─────────────
    def test_7_register_form_hides_setup_token_field_without_token(self):
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
