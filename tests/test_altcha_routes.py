# =============================================================================
# @file:         tests/test_altcha_routes.py
# @brief:        Route-level tests for the ALTCHA integration (login, challenge, contact).
# @details:      Uses the real Flask app in a temporary directory, like test_smoke_routes.py.
#                Covers: the public challenge endpoint, the widget on the login page, a
#                rejected CAPTCHA, a successful login with the Fail-Open switch off, and the
#                login guard on the sample contact form.
# @dependencies: internal: web (create_app), models, config.settings, core.db, core.security_audit
#                | external: flask, altcha
# @notes:        CSRF is disabled for these tests, as in test_smoke_routes.py.
# =============================================================================

"""Route tests for the self-hosted ALTCHA CAPTCHA integration."""

import os
import tempfile
import unittest
from unittest import mock

import altcha

from config import settings

SECRET = "route-test-secret-" + "y" * 40


class AltchaRouteTests(unittest.TestCase):
    """Exercise the ALTCHA routes through the Flask test client."""

    def setUp(self):
        """Create an isolated app, database and admin user for each test."""
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
            mock.patch.object(settings, "RECAPTCHA_SITE_KEY", "rc-site-key"),
            mock.patch.object(settings, "RECAPTCHA_SECRET_KEY", "rc-secret"),
        ]
        for p in self._patches:
            p.start()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        self.user = User.create(username="altcha_admin", email="altcha@test.io",
                                password="secret123", role="admin", is_verified=True)

    def tearDown(self):
        """Stop the patches and restore the working directory."""
        for p in self._patches:
            p.stop()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self, altcha_payload=None):
        """POST the login form, optionally with an ALTCHA payload."""
        data = {"identifier": "altcha_admin", "password": "secret123"}
        if altcha_payload is not None:
            data["altcha"] = altcha_payload
        return self.client.post("/login", data=data, follow_redirects=False)

    def test_challenge_endpoint_returns_widget_json(self):
        """GET /api/altcha/challenge is public and returns a challenge."""
        resp = self.client.get("/api/altcha/challenge")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("parameters", body)
        self.assertIn("signature", body)

    def test_challenge_endpoint_is_404_when_disabled(self):
        """With the Fail-Open switch off the challenge endpoint does not exist."""
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            resp = self.client.get("/api/altcha/challenge")
        self.assertEqual(resp.status_code, 404)

    def test_challenge_endpoint_is_404_without_secret(self):
        """Missing ALTCHA_HMAC_SECRET turns ALTCHA off (Fail-Open): no challenge is issued."""
        with mock.patch.object(settings, "ALTCHA_HMAC_SECRET", ""):
            resp = self.client.get("/api/altcha/challenge")
        self.assertEqual(resp.status_code, 404)

    def test_login_page_has_no_widget_without_secret(self):
        """Without a secret the login page renders no CAPTCHA, so users are not blocked."""
        with mock.patch.object(settings, "ALTCHA_HMAC_SECRET", ""):
            html = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn("<altcha-widget", html)

    def test_login_page_renders_widget_when_enabled(self):
        """The login page loads the self-hosted widget and points it at the challenge URL."""
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn("<altcha-widget", html)
        self.assertIn("/api/altcha/challenge", html)
        self.assertIn("vendor/altcha/altcha.min.js", html)
        self.assertNotIn("google.com/recaptcha", html)

    def test_login_page_has_no_widget_when_disabled(self):
        """With the Fail-Open switch off the widget is not rendered."""
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            html = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn("<altcha-widget", html)

    def test_login_without_captcha_is_rejected_when_enabled(self):
        """A correct password is not enough while the CAPTCHA is enabled."""
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("کپچا معتبر نیست".encode("utf-8"), resp.data)

    def test_login_succeeds_with_valid_captcha(self):
        """A solved challenge plus the right password logs the user in."""
        from web.captcha import create_challenge
        challenge = altcha.Challenge.from_dict(create_challenge())
        solution = altcha.solve_challenge(challenge)
        payload = altcha.Payload(challenge, solution).to_base64()
        resp = self._login(payload)
        self.assertEqual(resp.status_code, 302)

    def test_login_succeeds_without_captcha_when_fail_open(self):
        """Fail-Open: with the switch off the login works without any CAPTCHA field."""
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            resp = self._login()
        self.assertEqual(resp.status_code, 302)

    def test_contact_form_requires_login(self):
        """The sample contact form is not public."""
        resp = self.client.get("/contact")
        self.assertNotEqual(resp.status_code, 200)

    def test_contact_form_rejects_bad_captcha(self):
        """A logged-in user with a missing CAPTCHA gets the validation error."""
        self._login_session()
        resp = self.client.post("/contact", data={"email": "a@b.io", "message": "hi"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("کپچا معتبر نیست".encode("utf-8"), resp.data)
        self.assertNotIn("با موفقیت ارسال شد".encode("utf-8"), resp.data)

    def test_contact_form_accepts_when_fail_open(self):
        """With the switch off a valid contact form shows the success message."""
        self._login_session()
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            resp = self.client.post("/contact", data={"email": "a@b.io", "message": "hi"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("با موفقیت ارسال شد".encode("utf-8"), resp.data)

    def _login_session(self):
        """Log the test user in through the session (CAPTCHA disabled for setup only)."""
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            resp = self._login()
        self.assertEqual(resp.status_code, 302)

    def test_challenge_endpoint_is_404_for_recaptcha(self):
        """When reCAPTCHA is the provider, the ALTCHA challenge endpoint is not used."""
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "recaptcha"):
            resp = self.client.get("/api/altcha/challenge")
        self.assertEqual(resp.status_code, 404)

    def test_login_page_renders_recaptcha_when_selected(self):
        """With reCAPTCHA selected, the login page loads Google's widget with the site key."""
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "recaptcha"):
            html = self.client.get("/login").get_data(as_text=True)
        self.assertIn('class="g-recaptcha"', html)
        self.assertIn('data-sitekey="rc-site-key"', html)
        self.assertIn("https://www.google.com/recaptcha/api.js", html)
        self.assertNotIn("<altcha-widget", html)

    def test_recaptcha_login_checks_google_answer(self):
        """A reCAPTCHA login succeeds only when Google accepts the token."""
        accepted = mock.Mock()
        accepted.json.return_value = {"success": True}
        rejected = mock.Mock()
        rejected.json.return_value = {"success": False}
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "recaptcha"):
            with mock.patch("web.captcha.requests.post", return_value=accepted):
                ok = self.client.post("/login", data={"identifier": "altcha_admin", "password": "secret123",
                                                      "g-recaptcha-response": "good"})
            # A fresh client: the first login would otherwise leave the session signed in.
            with mock.patch("web.captcha.requests.post", return_value=rejected):
                bad = self.app.test_client().post("/login", data={"identifier": "altcha_admin", "password": "secret123",
                                                       "g-recaptcha-response": "bad"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(bad.status_code, 200)
        self.assertIn("کپچا معتبر نیست".encode("utf-8"), bad.data)

    def test_csp_allows_google_only_for_recaptcha(self):
        """Google hosts appear in the CSP only while reCAPTCHA is the active provider."""
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "altcha"):
            altcha_csp = self.client.get("/login").headers["Content-Security-Policy"]
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "recaptcha"):
            rc_csp = self.client.get("/login").headers["Content-Security-Policy"]
        self.assertNotIn("google.com", altcha_csp)
        self.assertIn("https://www.google.com", rc_csp)
        self.assertIn("frame-src https://www.google.com", rc_csp)
        self.assertIn("worker-src 'self' blob:", altcha_csp)



if __name__ == "__main__":
    unittest.main()
