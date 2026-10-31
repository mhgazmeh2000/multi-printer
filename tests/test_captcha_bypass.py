# =============================================================================
# @file:         tests/test_captcha_bypass.py
# @brief:        Tests for the per-network CAPTCHA bypass (direct LAN vs proxied public traffic).
# @details:      Uses the real Flask app in a temporary directory, like test_altcha_routes.py.
#                Covers: direct LAN clients skip CAPTCHA, reverse-proxy traffic follows the
#                admin panel, forwarded headers and the trusted proxy are never bypassed,
#                invalid entries are ignored, and the challenge endpoint answers 404 for LAN.
# @dependencies: internal: web (create_app), web.captcha, models, config.settings, core.database
#                | external: flask
# @notes:        CSRF is disabled for these tests, as in test_altcha_routes.py.
# =============================================================================

"""Tests for CAPTCHA_BYPASS_NETWORKS and CAPTCHA_TRUSTED_PROXIES."""

import os
import tempfile
import unittest
from unittest import mock

from config import settings
from web import captcha

SECRET = "bypass-test-secret-" + "z" * 40
LAN_CLIENT = "172.16.25.82"
PROXY_PEER = "172.16.25.50"
PUBLIC_CLIENT = "203.0.113.9"
LAN_NET = "172.16.25.0/24"


class CaptchaBypassTests(unittest.TestCase):
    """Exercise the bypass rule through the Flask test client and the request context."""

    def setUp(self):
        """Create an isolated app and database, with ALTCHA active and the LAN bypass set."""
        from core.database import init_db
        from core.security_audit import init_security_audit
        from models import User
        from web import create_app

        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        init_security_audit()
        self._patches = [
            mock.patch.object(settings, "ALTCHA_HMAC_SECRET", SECRET),
            mock.patch.object(settings, "CAPTCHA_ENABLED", True),
            mock.patch.object(settings, "ALTCHA_ENABLED", True),
            mock.patch.object(settings, "ALTCHA_COST", 500),
            mock.patch.object(settings, "CAPTCHA_PROVIDER", "altcha"),
            mock.patch.object(settings, "CAPTCHA_BYPASS_NETWORKS", LAN_NET),
            mock.patch.object(settings, "CAPTCHA_TRUSTED_PROXIES", PROXY_PEER),
        ]
        for p in self._patches:
            p.start()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        User.create(username="bypass_admin", email="bypass@test.io",
                    password="secret123", role="admin", is_verified=True)

    def tearDown(self):
        """Stop the patches and restore the working directory."""
        for p in self._patches:
            p.stop()
        captcha._parse_networks.cache_clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login_html(self, peer, headers=None):
        """Return the login page as seen from a given connection peer."""
        resp = self.client.get("/login", environ_base={"REMOTE_ADDR": peer}, headers=headers or {})
        return resp.get_data(as_text=True)

    def test_direct_lan_client_has_no_widget(self):
        """A direct LAN client sees the login form without CAPTCHA."""
        self.assertNotIn("<altcha-widget", self._login_html(LAN_CLIENT))

    def test_proxy_peer_gets_the_widget(self):
        """Traffic from the reverse proxy (domain traffic) keeps CAPTCHA."""
        self.assertIn("<altcha-widget", self._login_html(PROXY_PEER))

    def test_forwarded_header_is_never_bypassed(self):
        """A request with X-Forwarded-For is public traffic, even from a LAN address."""
        html = self._login_html(LAN_CLIENT, headers={"X-Forwarded-For": PUBLIC_CLIENT})
        self.assertIn("<altcha-widget", html)

    def test_real_ip_header_is_never_bypassed(self):
        """A request with X-Real-IP is treated the same way as X-Forwarded-For."""
        html = self._login_html(LAN_CLIENT, headers={"X-Real-IP": PUBLIC_CLIENT})
        self.assertIn("<altcha-widget", html)

    def test_public_client_gets_the_widget(self):
        """A client outside the bypass network always gets CAPTCHA."""
        self.assertIn("<altcha-widget", self._login_html(PUBLIC_CLIENT))

    def test_bypass_is_off_when_unset(self):
        """With CAPTCHA_BYPASS_NETWORKS empty, LAN clients keep CAPTCHA (default behaviour)."""
        with mock.patch.object(settings, "CAPTCHA_BYPASS_NETWORKS", ""):
            self.assertIn("<altcha-widget", self._login_html(LAN_CLIENT))

    def test_challenge_endpoint_is_404_for_lan_and_200_for_public(self):
        """The challenge is not issued to bypassed clients, and is issued to public ones."""
        lan = self.client.get("/api/altcha/challenge", environ_base={"REMOTE_ADDR": LAN_CLIENT})
        public = self.client.get("/api/altcha/challenge", environ_base={"REMOTE_ADDR": PUBLIC_CLIENT})
        self.assertEqual(lan.status_code, 404)
        self.assertEqual(public.status_code, 200)

    def test_verify_accepts_empty_form_only_for_bypassed_clients(self):
        """verify_captcha passes a LAN submission without a token, and still checks public ones."""
        with self.app.test_request_context("/login", environ_base={"REMOTE_ADDR": LAN_CLIENT}):
            self.assertTrue(captcha.verify_captcha({}))
        with self.app.test_request_context("/login", environ_base={"REMOTE_ADDR": PUBLIC_CLIENT}):
            self.assertFalse(captcha.verify_captcha({}))

    def test_invalid_network_entry_is_ignored(self):
        """A malformed bypass entry does not open CAPTCHA for everyone."""
        captcha._parse_networks.cache_clear()
        with (
            mock.patch.object(settings, "CAPTCHA_BYPASS_NETWORKS", "not-a-network"),
            self.assertLogs("web.captcha", level="ERROR"),
        ):
            html = self._login_html(LAN_CLIENT)
        self.assertIn("<altcha-widget", html)

    def test_outside_request_context_is_not_bypassed(self):
        """Calls made outside a request (for example from scripts) are never bypassed."""
        self.assertFalse(captcha.request_bypasses_captcha())


if __name__ == "__main__":
    unittest.main()
