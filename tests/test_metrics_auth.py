# -*- coding: utf-8 -*-
"""
تست‌های حفاظت توکن Bearer برای /metrics (کار ۴ فاز C).

سیاست:
  • METRICS_TOKEN تنظیم شده + بدون هدر → 401
  • METRICS_TOKEN تنظیم شده + توکن درست → 200 با بدنه‌ی Prometheus
  • METRICS_TOKEN تنظیم شده + توکن اشتباه → 401
  • METRICS_TOKEN تنظیم نشده → باز (رفتار قبلی، با هشدار در لاگ)

نکته‌ی معماری: /metrics از ``public_endpoints`` حذف شده و احراز هویت در خود ویو
انجام می‌شود؛ بنابراین پاسخ بدون توکن باید ۴۰۱ باشد (نه ریدایرکت به login).
"""
import os
import tempfile
import unittest
from unittest import mock

import pytest

from core import store
from core.database import init_db

TOKEN = "test-metrics-token-123"

# این فایل فقط با pytest اجرا میشود (به‌عنوان بخشی از مسیر ۲۱ تستی CI).
# load_tests خالی ⇒ `python -m unittest discover` این فایل را کنار میگذارد و
# تستها دوبار شمرده نمیشوند.
pytestmark = pytest.mark.pytest_only


def load_tests(loader, tests, pattern):  # noqa: ARG001 - امضای پروتکل unittest
    """پروتکل unittest: این فایل با unittest کشف/اجرا نمیشود."""
    return unittest.TestSuite()



class MetricsTokenAuthTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []

    def tearDown(self):
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _client(self):
        from web import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["LOGIN_DISABLED"] = True
        return app.test_client()

    # ── ۱) بدون توکن (وقتی METRICS_TOKEN set است) → 401 ────────────────────
    def test_1_missing_token_is_rejected(self):
        client = self._client()
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": TOKEN}):
            r = client.get("/metrics")                     # بدون هدر Authorization
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            body = r.get_data(as_text=True)
            self.assertNotIn("printer_online", body, "بدنه‌ی ۴۰۱ نباید متریک داشته باشد")

            r2 = client.get("/metrics", headers={"Authorization": "Basic abc"})
            self.assertEqual(r2.status_code, 401, "طرح احراز هویت غیر Bearer پذیرفته نشود")

        # کنترل منفی (خارج از patch): وقتی توکن تنظیم نشده باشد → باز (رفتار قبلی)
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": ""}):
            r3 = client.get("/metrics")
        self.assertEqual(r3.status_code, 200,
                         "بدون METRICS_TOKEN باید مثل قبل باز باشد")
        self.assertIn("scrape_timestamp_seconds", r3.get_data(as_text=True))

    # ── ۲) توکن درست → 200 ────────────────────────────────────────────────
    def test_2_valid_token_is_accepted(self):
        client = self._client()
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": TOKEN}):
            r = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertIn("scrape_timestamp_seconds", body)
        self.assertIn("# HELP cartridge_changes_total", body)
        self.assertIn("text/plain", r.content_type)

    # ── ۳) توکن اشتباه → 401 ──────────────────────────────────────────────
    def test_3_wrong_token_is_rejected(self):
        client = self._client()
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": TOKEN}):
            r = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            # فاصله‌های اضافه نباید توکن را معتبر کند
            r2 = client.get("/metrics", headers={"Authorization": "Bearer  wrong  "})
            self.assertEqual(r2.status_code, 401)
        self.assertNotIn("printer_online", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
