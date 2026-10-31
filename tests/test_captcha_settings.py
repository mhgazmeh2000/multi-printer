# =============================================================================
# @file:         tests/test_captcha_settings.py
# @brief:        Tests for the admin system-settings API that chooses the CAPTCHA provider.
# @details:      Covers: the default provider, masking of the reCAPTCHA secret, validation of
#                CAPTCHA_PROVIDER, keeping the stored secret when the masked value is sent back,
#                saving the provider to the config file, the disable-all case, and the
#                CAPTCHA_ENABLED=false and ALTCHA_ENABLED=false reported to the panel.
# @dependencies: internal: web (create_app), config.settings, web.routes.users, core.database
#                | external: flask
# @notes:        The config file is redirected to a temporary path, and every settings value the
#                API may change is patched, so nothing is written to the repository or leaks
#                into other tests.
# =============================================================================
"""Tests for the CAPTCHA provider choice in the admin system-settings API."""

import json
import os
import tempfile
import unittest
from unittest import mock

from config import settings

#: Settings the API can write; patched so every test restores them afterwards.
_PATCHED = (
    "CAPTCHA_PROVIDER", "RECAPTCHA_SITE_KEY", "RECAPTCHA_SECRET_KEY",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MAIL_SERVER", "MAIL_PORT",
    "MAIL_USE_TLS", "MAIL_USERNAME", "MAIL_PASSWORD", "CAPTCHA_ENABLED", "ALTCHA_ENABLED",
)


class CaptchaSettingsApiTests(unittest.TestCase):
    """Exercise /api/system-settings as an admin."""

    def setUp(self):
        """Create an isolated app, a temporary config file and an admin session."""
        from core.database import init_db
        from core.security_audit import init_security_audit
        from models import User
        from web import create_app
        from web.routes import users as users_routes

        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        init_security_audit()
        self.config_path = os.path.join(self.tmp.name, "system_config.json")
        self._patches = [mock.patch.object(settings, name, getattr(settings, name)) for name in _PATCHED]
        self._patches.append(mock.patch.object(users_routes, "_SYSTEM_CONFIG_FILE", self.config_path))
        for p in self._patches:
            p.start()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()
        User.create(username="cfg_admin", email="cfg@test.io", password="secret123",
                    role="admin", is_verified=True)
        # Log in with the CAPTCHA switched off, so the setup does not depend on a solved challenge.
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            resp = self.client.post("/login", data={"identifier": "cfg_admin", "password": "secret123"})
        self.assertEqual(resp.status_code, 302)

    def tearDown(self):
        """Stop the patches and restore the working directory."""
        for p in self._patches:
            p.stop()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _read_file(self) -> dict:
        """Return the saved system config, or an empty dict if nothing was saved yet."""
        if not os.path.exists(self.config_path):
            return {}
        with open(self.config_path, encoding="utf-8") as f:
            return json.load(f)

    def test_default_provider_is_altcha(self):
        """With no saved choice the panel shows ALTCHA."""
        body = self.client.get("/api/system-settings").get_json()
        self.assertEqual(body["settings"]["CAPTCHA_PROVIDER"], "altcha")
        self.assertFalse(body["captcha_env_disabled"])

    def test_recaptcha_secret_is_masked(self):
        """The reCAPTCHA secret is never returned in clear text."""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"RECAPTCHA_SECRET_KEY": "abcd1234567890wxyz"}, f)
        body = self.client.get("/api/system-settings").get_json()
        shown = body["settings"]["RECAPTCHA_SECRET_KEY"]
        self.assertNotIn("1234567890", shown)
        self.assertTrue(shown.startswith("abcd") and shown.endswith("wxyz"))

    def test_env_master_switch_is_reported(self):
        """The panel is told when CAPTCHA_ENABLED=false overrides every provider."""
        with mock.patch.object(settings, "CAPTCHA_ENABLED", False):
            body = self.client.get("/api/system-settings").get_json()
        self.assertTrue(body["captcha_env_disabled"])
        self.assertFalse(body["altcha_env_disabled"])

    def test_altcha_switch_is_reported_separately(self):
        """ALTCHA_ENABLED=false is reported as an ALTCHA-only switch."""
        with mock.patch.object(settings, "ALTCHA_ENABLED", False):
            body = self.client.get("/api/system-settings").get_json()
        self.assertTrue(body["altcha_env_disabled"])
        self.assertFalse(body["captcha_env_disabled"])

    def test_missing_altcha_secret_is_reported(self):
        """The panel is told when ALTCHA_HMAC_SECRET is empty so it can show a warning."""
        with mock.patch.object(settings, "ALTCHA_HMAC_SECRET", ""):
            body = self.client.get("/api/system-settings").get_json()
        self.assertTrue(body["altcha_secret_missing"])
        with mock.patch.object(settings, "ALTCHA_HMAC_SECRET", "s" * 40):
            body = self.client.get("/api/system-settings").get_json()
        self.assertFalse(body["altcha_secret_missing"])

    def test_saving_recaptcha_persists_the_choice(self):
        """Choosing reCAPTCHA is saved to the config file and applied to the running settings."""
        resp = self.client.post("/api/system-settings", json={
            "CAPTCHA_PROVIDER": "recaptcha", "RECAPTCHA_SITE_KEY": "site-1", "RECAPTCHA_SECRET_KEY": "secret-1",
        })
        self.assertEqual(resp.status_code, 200)
        saved = self._read_file()
        self.assertEqual(saved["CAPTCHA_PROVIDER"], "recaptcha")
        self.assertEqual(saved["RECAPTCHA_SITE_KEY"], "site-1")
        self.assertEqual(saved["RECAPTCHA_SECRET_KEY"], "secret-1")
        self.assertEqual(settings.CAPTCHA_PROVIDER, "recaptcha")

    def test_disable_all_saves_none(self):
        """The disable-all button stores the 'none' provider."""
        resp = self.client.post("/api/system-settings", json={"CAPTCHA_PROVIDER": "none"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._read_file()["CAPTCHA_PROVIDER"], "none")

    def test_unknown_provider_is_rejected(self):
        """Values other than altcha, recaptcha and none are refused and nothing is saved."""
        resp = self.client.post("/api/system-settings", json={"CAPTCHA_PROVIDER": "hcaptcha"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._read_file(), {})

    def test_masked_secret_keeps_the_stored_value(self):
        """Sending the masked secret back does not overwrite the stored secret."""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"RECAPTCHA_SECRET_KEY": "real-secret-value"}, f)
        masked = self.client.get("/api/system-settings").get_json()["settings"]["RECAPTCHA_SECRET_KEY"]
        resp = self.client.post("/api/system-settings", json={"RECAPTCHA_SECRET_KEY": masked,
                                                             "CAPTCHA_PROVIDER": "recaptcha"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._read_file()["RECAPTCHA_SECRET_KEY"], "real-secret-value")

    def test_anonymous_user_cannot_read_settings(self):
        """The settings API is not available without signing in."""
        resp = self.app.test_client().get("/api/system-settings")
        self.assertNotEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
