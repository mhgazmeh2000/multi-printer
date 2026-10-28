# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_metrics_auth.py
# @brief:        Tests for bearer-token protection of /metrics (work 4, phase C).
# @details:      Responsibilities: Verify authorised and rejected requests.
#                Main API: MetricsTokenAuthTests
#                Side effects: none at import time
# @dependencies: internal: core, core.database, web | external: pytest
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Bearer token protection tests for /metrics (work 4 phase C).

Policy:
  • METRICS_TOKEN set + no header → 401
  • METRICS_TOKEN set + correct token → 200 with Prometheus body
  • METRICS_TOKEN set + wrong token → 401
  • METRICS_TOKEN not set → open (previous behavior, with warning in log)

Architectural note: /metrics is removed from ``public_endpoints'' and authentication in the view itself
is done; So the response without token should be 401 (not redirect to login).
"""
import os
import tempfile
import unittest
from unittest import mock

import pytest

from core import store
from core.database import init_db

TOKEN = "test-metrics-token-123"

# This file is only run with pytest (as part of CI test path 21).
# load_tests is empty ⇒ `python -m unittest discover` ignores this file and
# Tests are not counted twice.
pytestmark = pytest.mark.pytest_only


def load_tests(loader, tests, pattern):  # noqa: ARG001 - unittest protocol signature
    """Unittest protocol: This file is not detected/executed by unittest."""
    return unittest.TestSuite()



class MetricsTokenAuthTests(unittest.TestCase):

    """Tests for metrics token auth."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []

    def tearDown(self):
        """Release the resources that each test in this class created."""
        store._prev._cache.clear()
        store.printer_data = {}
        store.PRINTERS = []
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _client(self):
        """Helper: client."""
        from web import create_app
        app = create_app()
        app.config["TESTING"] = True
        app.config["LOGIN_DISABLED"] = True
        return app.test_client()

    # ── 1) No token (when METRICS_TOKEN is set) → 401 ──────────────────────
    def test_1_missing_token_is_rejected(self):
        """Verifies 1 missing token is rejected."""
        client = self._client()
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": TOKEN}):
            r = client.get("/metrics")                     # No Authorization header
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            body = r.get_data(as_text=True)
            self.assertNotIn("printer_online", body, "بدنه‌ی ۴۰۱ نباید متریک داشته باشد")

            r2 = client.get("/metrics", headers={"Authorization": "Basic abc"})
            self.assertEqual(r2.status_code, 401, "طرح احراز هویت غیر Bearer پذیرفته نشود")

        # Negative control (out of patch): when token is not set → open (previous behavior)
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": ""}):
            r3 = client.get("/metrics")
        self.assertEqual(r3.status_code, 200,
                         "بدون METRICS_TOKEN باید مثل قبل باز باشد")
        self.assertIn("scrape_timestamp_seconds", r3.get_data(as_text=True))

    # ── 2) Correct token → 200
    def test_2_valid_token_is_accepted(self):
        """Verifies 2 valid token is accepted."""
        client = self._client()
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": TOKEN}):
            r = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertIn("scrape_timestamp_seconds", body)
        self.assertIn("# HELP cartridge_changes_total", body)
        self.assertIn("text/plain", r.content_type)

    # ── 3) Invalid token → 401
    def test_3_wrong_token_is_rejected(self):
        """Verifies 3 wrong token is rejected."""
        client = self._client()
        with mock.patch.dict(os.environ, {"METRICS_TOKEN": TOKEN}):
            r = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
            self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
            # Extra spaces should not make the token valid
            r2 = client.get("/metrics", headers={"Authorization": "Bearer  wrong  "})
            self.assertEqual(r2.status_code, 401)
        self.assertNotIn("printer_online", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
