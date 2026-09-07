# -*- coding: utf-8 -*-
"""
تست‌های reverse-proxy (ProxyFix) و مسیر CSRF روی endpoint های سیستمی.

پوشش:
  ۱) بدون TRUST_PROXY: هدرهای X-Forwarded-* نادیده گرفته می‌شوند (ضد جعل هدر).
  ۲) با TRUST_PROXY=1: X-Forwarded-Proto/For از nginx معتبرند → request.is_secure
     پشت TLS-termination درست می‌شود (پیش‌نیاز WTF_CSRF_SSL_STRICT و redirectهای https).
  ۳) با TRUST_PROXY=1 ولی بدون هدر: هیچ اعتماد کوری وجود ندارد.
  ۴) فقط یک hop اعتماد می‌شود (x_for=1) — در XFF زنجیره‌ای، آخرین hop.
  ۵) POST /api/poll/now با CSRF نامعتبر → 400 + {"error": "csrf_token_invalid"}
     (همان قراردادی که retry فرانت‌اند در apiFetch روی آن سوار است).
  ۶) POST /api/poll/now با توکن تازه‌ی /api/status → 200 {"status": "started"}.
  ۷) busy بودن چرخه‌ی poll → 200 {"status": "busy"} (رفتار فعلی حفظ شود).
"""
import os
import tempfile
import unittest
from unittest import mock

from core.database import init_db


class ProxyProbeBase(unittest.TestCase):
    """پایه: ساخت app با/بدون TRUST_PROXY + ثبت probe برای دیدن is_secure از درون."""

    def _make(self, trust_proxy=None):
        from flask import request
        from web import create_app
        with mock.patch.dict(os.environ, {}, clear=False):
            if trust_proxy is None:
                os.environ.pop("TRUST_PROXY", None)
            else:
                os.environ["TRUST_PROXY"] = trust_proxy
            app = create_app()
        app.config["TESTING"] = True
        seen = self.seen = {}

        @app.after_request
        def _probe(resp):
            seen["secure"] = request.is_secure
            seen["remote"] = request.remote_addr
            return resp

        return app.test_client()


class ProxyFixTests(ProxyProbeBase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_no_trust_proxy_ignores_forwarded_headers(self):
        """پیش‌فرض (توسعه/دسترسی مستقیم): هدر جعل‌شده نباید is_secure را عوض کند."""
        client = self._make(trust_proxy=None)
        client.get("/login", headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "9.9.9.9",
        })
        self.assertFalse(self.seen["secure"])
        self.assertNotEqual(self.seen["remote"], "9.9.9.9")

    def test_trust_proxy_honors_forwarded_proto_and_for(self):
        """با TRUST_PROXY=1 و nginx جلوی برنامه: scheme و IP واقعی دیده می‌شوند."""
        client = self._make(trust_proxy="1")
        client.get("/login", headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "9.9.9.9",
        })
        self.assertTrue(self.seen["secure"])
        self.assertEqual(self.seen["remote"], "9.9.9.9")

    def test_trust_proxy_without_headers_is_noop(self):
        """با TRUST_PROXY=1 ولی بدون هدر: هیچ اعتماد کوری رخ نمی‌دهد."""
        client = self._make(trust_proxy="1")
        client.get("/login")
        self.assertFalse(self.seen["secure"])

    def test_only_one_hop_trusted(self):
        """x_for=1 ⇒ از XFF زنجیره‌ای فقط آخرین hop (nginx خودمان) اعتماد می‌شود."""
        client = self._make(trust_proxy="1")
        client.get("/login", headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
        self.assertEqual(self.seen["remote"], "9.9.9.9")


class PollNowCsrfTests(unittest.TestCase):
    """قرارداد 400/200 روی /api/poll/now — مسیری که apiFetch برای retry استفاده می‌کند."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        os.environ.pop("TRUST_PROXY", None)  # این تست‌ها مستقل از proxy باشند
        from web import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        from models import User
        User.create(username="px_admin", email="px@test.io",
                    password="secret123", role="admin", is_verified=True)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = "1"
            sess["_fresh"] = True

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _csrf_token(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        return (r.get_json() or {}).get("csrf_token", "")

    def test_poll_now_with_invalid_csrf_returns_400_json(self):
        r = self.client.post("/api/poll/now", headers={"X-CSRFToken": "bogus-token"})
        self.assertEqual(r.status_code, 400)
        body = r.get_json() or {}
        self.assertEqual(body.get("error"), "csrf_token_invalid")

    def test_poll_now_with_fresh_token_from_status_succeeds(self):
        """توکن تازه از /api/status ⇒ 200. عین مسیر retry در window.apiFetch."""
        import core.poller as poller_mod
        calls = []
        orig = poller_mod.poll_all
        poller_mod.poll_all = lambda: calls.append(1)
        try:
            token = self._csrf_token()
            self.assertTrue(token)
            r = self.client.post("/api/poll/now", headers={"X-CSRFToken": token})
            self.assertEqual(r.status_code, 200)
            self.assertEqual((r.get_json() or {}).get("status"), "started")
        finally:
            poller_mod.poll_all = orig

    def test_poll_now_busy_when_polling_lock_held(self):
        import core.poller as poller_mod
        token = self._csrf_token()
        with poller_mod._polling_lock:
            r = self.client.post("/api/poll/now", headers={"X-CSRFToken": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json() or {}).get("status"), "busy")


if __name__ == "__main__":
    unittest.main()
