# =============================================================================
# @file:         tests/test_altcha_captcha.py
# @brief:        Tests for the CAPTCHA helpers in web/captcha.py (ALTCHA and reCAPTCHA).
# @details:      Covers: provider selection (altcha, recaptcha, none, the CAPTCHA_ENABLED master
#                switch and the ALTCHA_ENABLED switch), ALTCHA solutions (accepted, tampered, expired, replayed, missing
#                secret), reCAPTCHA verification with Google's response mocked (accepted,
#                rejected, empty token, missing secret, Fail-Open on network failure), and the
#                environment parsing of both switches. ALTCHA solutions come from the real
#                altcha library. No test contacts Google.
# @dependencies: internal: web.captcha, config.settings | external: altcha, requests
# @notes:        Cost is lowered for speed; the verification logic is the same.
# =============================================================================
"""Unit tests for web/captcha.py (ALTCHA and optional reCAPTCHA)."""

import base64
import importlib
import json
import os
import unittest
from unittest import mock

import altcha
import requests

from config import settings
from web import captcha

SECRET = "test-secret-" + "x" * 40
TEST_COST = 500


def _solve(challenge_dict: dict) -> str:
    """Solve a challenge the way the widget does and return the base64 payload."""
    challenge = altcha.Challenge.from_dict(challenge_dict)
    solution = altcha.solve_challenge(challenge)
    assert solution is not None, "solver returned no solution"
    return altcha.Payload(challenge, solution).to_base64()


def _form(altcha_payload=None, recaptcha_token=None) -> dict:
    """Build a form mapping with the fields a submitted page would contain."""
    form = {}
    if altcha_payload is not None:
        form[captcha.FIELD_NAME] = altcha_payload
    if recaptcha_token is not None:
        form[captcha.RECAPTCHA_FIELD] = recaptcha_token
    return form


class _AltchaBase(unittest.TestCase):
    """Patch the settings that web/captcha.py reads, for every test."""

    provider = "altcha"

    def setUp(self):
        """Patch the settings used by web/captcha.py for each test."""
        self._patches = [
            mock.patch.object(settings, "ALTCHA_HMAC_SECRET", SECRET),
            mock.patch.object(settings, "CAPTCHA_ENABLED", True),
            mock.patch.object(settings, "ALTCHA_ENABLED", True),
            mock.patch.object(settings, "CAPTCHA_PROVIDER", self.provider),
            mock.patch.object(settings, "ALTCHA_COST", TEST_COST),
            mock.patch.object(settings, "ALTCHA_CHALLENGE_TTL_SECONDS", 300),
            mock.patch.object(settings, "RECAPTCHA_SECRET_KEY", "recaptcha-secret"),
            mock.patch.object(settings, "RECAPTCHA_SITE_KEY", "recaptcha-site"),
        ]
        for p in self._patches:
            p.start()
        captcha._accepted_signatures.clear()

    def tearDown(self):
        """Undo the settings patches."""
        for p in self._patches:
            p.stop()
        captcha._accepted_signatures.clear()


class AltchaCaptchaTests(_AltchaBase):
    """ALTCHA verification with the altcha provider active."""

    def test_solved_challenge_is_accepted(self):
        """A correctly solved, untampered challenge passes."""
        payload = _solve(captcha.create_challenge())
        self.assertTrue(captcha.verify_captcha(_form(payload)))

    def test_challenge_has_widget_shape(self):
        """The challenge JSON has the parameters and signature fields the widget expects."""
        data = captcha.create_challenge()
        self.assertIn("parameters", data)
        self.assertIn("signature", data)
        self.assertIn("expiresAt", data["parameters"])

    def test_empty_or_missing_payload_is_rejected(self):
        """No payload means the CAPTCHA was not solved."""
        self.assertFalse(captcha.verify_captcha(_form("")))
        self.assertFalse(captcha.verify_captcha({}))

    def test_garbage_payload_is_rejected(self):
        """A payload that is not valid base64 JSON is rejected without raising."""
        self.assertFalse(captcha.verify_captcha(_form("not-a-real-payload")))

    def test_tampered_signature_is_rejected(self):
        """Changing the challenge signature invalidates the payload."""
        payload = _solve(captcha.create_challenge())
        decoded = json.loads(base64.b64decode(payload))
        decoded["challenge"]["signature"] = "0" * 64
        tampered = base64.b64encode(json.dumps(decoded).encode()).decode()
        self.assertFalse(captcha.verify_captcha(_form(tampered)))

    def test_replay_is_rejected(self):
        """The same accepted payload cannot be used a second time."""
        payload = _solve(captcha.create_challenge())
        self.assertTrue(captcha.verify_captcha(_form(payload)))
        self.assertFalse(captcha.verify_captcha(_form(payload)))

    def test_expired_challenge_is_rejected(self):
        """A challenge whose expiry has passed is rejected."""
        with mock.patch.object(settings, "ALTCHA_CHALLENGE_TTL_SECONDS", -10):
            payload = _solve(captcha.create_challenge())
        self.assertFalse(captcha.verify_captcha(_form(payload)))

    def test_missing_secret_fails_open(self):
        """Enabled ALTCHA without a secret turns CAPTCHA off (Fail-Open) instead of locking users out."""
        with mock.patch.object(settings, "ALTCHA_HMAC_SECRET", ""):
            with self.assertRaises(RuntimeError):
                captcha.create_challenge()
            self.assertEqual(captcha.active_provider(), captcha.PROVIDER_NONE)
            self.assertFalse(captcha.captcha_enabled())
            self.assertTrue(captcha.verify_captcha(_form("anything")))

    def test_missing_secret_is_logged_once(self):
        """The missing-secret error is logged once per process, not on every request."""
        captcha._missing_secret_logged = False
        with mock.patch.object(settings, "ALTCHA_HMAC_SECRET", ""):
            with self.assertLogs("web.captcha", level="ERROR") as logs:
                captcha.active_provider()
                captcha.active_provider()
                captcha.verify_captcha(_form("x"))
        self.assertEqual(len(logs.records), 1)

    def test_recaptcha_field_is_ignored_for_altcha(self):
        """A reCAPTCHA token does not satisfy the ALTCHA provider."""
        self.assertFalse(captcha.verify_captcha(_form(recaptcha_token="token")))


class ProviderSelectionTests(_AltchaBase):
    """active_provider() and captcha_enabled() follow the settings."""

    def test_default_provider_is_altcha(self):
        """The ALTCHA provider is active when it is selected."""
        self.assertEqual(captcha.active_provider(), "altcha")
        self.assertTrue(captcha.captcha_enabled())

    def test_master_switch_turns_every_provider_off(self):
        """CAPTCHA_ENABLED=false disables CAPTCHA regardless of the provider."""
        for provider in ("altcha", "recaptcha", "none"):
            with mock.patch.object(settings, "CAPTCHA_PROVIDER", provider), \
                    mock.patch.object(settings, "CAPTCHA_ENABLED", False):
                self.assertEqual(captcha.active_provider(), "none", provider)
                self.assertFalse(captcha.captcha_enabled())
                self.assertTrue(captcha.verify_captcha({}))

    def test_altcha_switch_only_turns_off_altcha(self):
        """ALTCHA_ENABLED=false turns off ALTCHA but leaves reCAPTCHA active."""
        with mock.patch.object(settings, "ALTCHA_ENABLED", False):
            with mock.patch.object(settings, "CAPTCHA_PROVIDER", "altcha"):
                self.assertEqual(captcha.active_provider(), "none")
                self.assertTrue(captcha.verify_captcha({}))
            with mock.patch.object(settings, "CAPTCHA_PROVIDER", "recaptcha"):
                self.assertEqual(captcha.active_provider(), "recaptcha")

    def test_none_provider_skips_every_check(self):
        """With provider none, even an empty form passes (Fail-Open)."""
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "none"):
            self.assertFalse(captcha.captcha_enabled())
            self.assertTrue(captcha.verify_captcha({}))
            self.assertTrue(captcha.verify_captcha(_form("garbage")))

    def test_provider_name_is_case_insensitive(self):
        """'ReCAPTCHA ' is read as recaptcha."""
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", " ReCAPTCHA "):
            self.assertEqual(captcha.active_provider(), "recaptcha")

    def test_unknown_provider_falls_back_to_altcha(self):
        """An unknown value does not turn CAPTCHA off; ALTCHA is used."""
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "hcaptcha"):
            self.assertEqual(captcha.active_provider(), "altcha")

    def test_context_exposes_site_key_only_for_recaptcha(self):
        """Templates get the reCAPTCHA site key only when reCAPTCHA is active."""
        self.assertEqual(captcha.captcha_context()["captcha_site_key"], "")
        with mock.patch.object(settings, "CAPTCHA_PROVIDER", "recaptcha"):
            ctx = captcha.captcha_context()
        self.assertEqual(ctx["captcha_provider"], "recaptcha")
        self.assertEqual(ctx["captcha_site_key"], "recaptcha-site")


class RecaptchaVerificationTests(_AltchaBase):
    """reCAPTCHA verification with Google's siteverify response mocked."""

    provider = "recaptcha"

    def _response(self, payload):
        """Build a fake requests.Response whose JSON is the given payload."""
        resp = mock.Mock()
        resp.json.return_value = payload
        return resp

    def test_google_success_is_accepted(self):
        """A token that Google accepts passes, and the secret and client IP are sent."""
        with mock.patch.object(captcha.requests, "post", return_value=self._response({"success": True})) as post:
            ok = captcha.verify_captcha(_form(recaptcha_token="tok"), remote_ip="10.1.2.3")
        self.assertTrue(ok)
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["secret"], "recaptcha-secret")
        self.assertEqual(sent["response"], "tok")
        self.assertEqual(sent["remoteip"], "10.1.2.3")
        self.assertEqual(post.call_args.args[0], captcha.RECAPTCHA_VERIFY_URL)

    def test_google_rejection_is_refused(self):
        """A token that Google rejects does not pass."""
        with mock.patch.object(captcha.requests, "post",
                               return_value=self._response({"success": False, "error-codes": ["invalid-input-response"]})):
            self.assertFalse(captcha.verify_captcha(_form(recaptcha_token="bad")))

    def test_empty_token_is_refused_without_calling_google(self):
        """A missing token is rejected before any request is made."""
        with mock.patch.object(captcha.requests, "post") as post:
            self.assertFalse(captcha.verify_captcha(_form(recaptcha_token="")))
            self.assertFalse(captcha.verify_captcha({}))
        post.assert_not_called()

    def test_missing_secret_fails_closed(self):
        """Without RECAPTCHA_SECRET_KEY, reCAPTCHA rejects and does not call Google."""
        with mock.patch.object(settings, "RECAPTCHA_SECRET_KEY", ""), \
                mock.patch.object(captcha.requests, "post") as post:
            self.assertFalse(captcha.verify_captcha(_form(recaptcha_token="tok")))
        post.assert_not_called()

    def test_network_failure_fails_open(self):
        """If Google cannot be reached, the submission is allowed (Fail-Open)."""
        with mock.patch.object(captcha.requests, "post", side_effect=requests.ConnectionError("blocked")):
            self.assertTrue(captcha.verify_captcha(_form(recaptcha_token="tok")))

    def test_non_json_answer_fails_open(self):
        """A non-JSON answer (for example a blocked-page HTML) is treated as an outage."""
        resp = mock.Mock()
        resp.json.side_effect = ValueError("not json")
        with mock.patch.object(captcha.requests, "post", return_value=resp):
            self.assertTrue(captcha.verify_captcha(_form(recaptcha_token="tok")))


class CaptchaSettingsEnvironmentTests(unittest.TestCase):
    """config/settings.py reads CAPTCHA_ENABLED and ALTCHA_ENABLED from the environment."""

    def _load_flag(self, name: str, raw: str) -> bool:
        """Reload config.settings with the given environment value and return the flag."""
        import config.settings as cfg

        with mock.patch.dict(os.environ, {name: raw}):
            importlib.reload(cfg)
            value = getattr(cfg, name)
        importlib.reload(cfg)  # restore the module state for other tests
        return value

    def test_false_spellings_disable(self):
        """'false', '0', 'no' and 'off' turn each switch off."""
        for name in ("CAPTCHA_ENABLED", "ALTCHA_ENABLED"):
            for raw in ("false", "False", "0", "no", "off"):
                self.assertFalse(self._load_flag(name, raw), (name, raw))

    def test_true_spellings_keep_on(self):
        """'true', '1', 'yes' and 'on' keep each switch on."""
        for name in ("CAPTCHA_ENABLED", "ALTCHA_ENABLED"):
            for raw in ("true", "1", "yes", "on"):
                self.assertTrue(self._load_flag(name, raw), (name, raw))

    def test_altcha_cost_default_is_5000(self):
        """The default ALTCHA cost is the recommended 5000 when nothing is set."""
        import config.settings as cfg

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALTCHA_COST", None)
            importlib.reload(cfg)
            value = cfg.ALTCHA_COST
        importlib.reload(cfg)
        self.assertEqual(value, 5000)


if __name__ == "__main__":
    unittest.main()
