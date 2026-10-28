# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_proxy_fix.py
# @brief:        Tests for reverse-proxy handling (ProxyFix) and CSRF on system endpoints.
# @details:      Responsibilities: Verify that forwarded headers and CSRF checks behave correctly
#                behind a proxy.
#                Main API: ProxyProbeBase, ProxyFixTests, PollNowCsrfTests
#                Side effects: none at import time
# @dependencies: internal: core.database, core.poller, models, web | external: flask
# @notes:        Reads environment variables: TRUST_PROXY. Can be run directly as a script.
# =============================================================================

"""
reverse-proxy tests (ProxyFix) and CSRF route on system endpoints.

Coverage:
  1) Without TRUST_PROXY: X-Forwarded-* headers are ignored (anti-header forgery).
  2) With TRUST_PROXY=1: X-Forwarded-Proto/For from nginx are valid → request.is_secure
     It is fixed behind TLS-termination (prerequisite WTF_CSRF_SSL_STRICT and https redirects).
  3) With TRUST_PROXY=1 but no header: there is no blind trust.
  4) Only one hop is trusted (x_for=1) — in chained XFF, the last hop.
  5) POST /api/poll/now with invalid CSRF → 400 + {"error": "csrf_token_invalid"}
     (The same contract that the retry front end in apiFetch is based on).
  6) POST /api/poll/now with new token /api/status → 200 {"status": "started"}.
  7) poll cycle being busy → 200 {"status": "busy"} (keep the current behavior).
"""
import os
import tempfile
import unittest
from unittest import mock

from core.database import init_db


class ProxyProbeBase(unittest.TestCase):
    """Base: build app with/without TRUST_PROXY + register probe to see is_secure from inside."""

    def _make(self, trust_proxy=None):
        """Helper: make."""
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
            """Helper: probe."""
            seen["secure"] = request.is_secure
            seen["remote"] = request.remote_addr
            return resp

        return app.test_client()


class ProxyFixTests(ProxyProbeBase):
    """Tests for proxy fix."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_no_trust_proxy_ignores_forwarded_headers(self):
        """Default (development/direct access): Forged header should not override is_secure."""
        client = self._make(trust_proxy=None)
        client.get("/login", headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "9.9.9.9",
        })
        self.assertFalse(self.seen["secure"])
        self.assertNotEqual(self.seen["remote"], "9.9.9.9")

    def test_trust_proxy_honors_forwarded_proto_and_for(self):
        """With TRUST_PROXY=1 and nginx in front of the application: scheme and real IP are seen."""
        client = self._make(trust_proxy="1")
        client.get("/login", headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": "9.9.9.9",
        })
        self.assertTrue(self.seen["secure"])
        self.assertEqual(self.seen["remote"], "9.9.9.9")

    def test_trust_proxy_without_headers_is_noop(self):
        """With TRUST_PROXY=1 but no header: no blind trust occurs."""
        client = self._make(trust_proxy="1")
        client.get("/login")
        self.assertFalse(self.seen["secure"])

    def test_only_one_hop_trusted(self):
        """x_for=1 ⇒ From the XFF chain, only the last hop (our own nginx) is trusted."""
        client = self._make(trust_proxy="1")
        client.get("/login", headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
        self.assertEqual(self.seen["remote"], "9.9.9.9")


class PollNowCsrfTests(unittest.TestCase):
    """400/200 convention on /api/poll/now — the path apiFetch uses to retry."""

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        os.environ.pop("TRUST_PROXY", None)  # These tests are independent of the proxy
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
        """Release the resources that each test in this class created."""
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _csrf_token(self):
        """Helper: csrf token."""
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        return (r.get_json() or {}).get("csrf_token", "")

    def test_poll_now_with_invalid_csrf_returns_400_json(self):
        """Verifies poll now with invalid csrf returns 400 json."""
        r = self.client.post("/api/poll/now", headers={"X-CSRFToken": "bogus-token"})
        self.assertEqual(r.status_code, 400)
        body = r.get_json() or {}
        self.assertEqual(body.get("error"), "csrf_token_invalid")

    def test_poll_now_with_fresh_token_from_status_succeeds(self):
        """Fresh token from /api/status ⇒ 200. Same retry path in window.apiFetch."""
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
        """Verifies poll now busy when polling lock held."""
        import core.poller as poller_mod
        token = self._csrf_token()
        with poller_mod._polling_lock:
            r = self.client.post("/api/poll/now", headers={"X-CSRFToken": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json() or {}).get("status"), "busy")


if __name__ == "__main__":
    unittest.main()
