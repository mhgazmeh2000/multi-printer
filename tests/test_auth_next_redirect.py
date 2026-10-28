# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_auth_next_redirect.py
# @brief:        Tests for safe 'next' redirects in the authentication flow (TD-002).
# @details:      Responsibilities: Verify same-site validation of redirect targets.
#                Main API: SafeNextUrlValidationTests, _AppMixin, SameSiteTargetTests,
#                UnauthorizedResponseTests, LoginReturnTargetTests
#                Side effects: database access, network I/O
# @dependencies: internal: core.database, core.db, models, web, web.auth
# @notes:        Can be run directly as a script.
# =============================================================================

r"""
Tests E.6.6 (TD-002) — `next' in authentication path: same site, and both sides validation.

Three claims are locked
------------------
1. **minimal reflection:** 401 response should not return full `request.url` (scheme+host).
   Before E.6.6, `login_url` was:
   `/login?next=http://localhost/api/export/logs?fmt%3Dcsv`
   And now `/login?next=/api/export/logs?fmt=csv` — same UX (back to same page),
   No host address, no duplicate content in logs.

2. **consumer guard:** `_is_safe_next_url` is the only return gateway after login and
   Until now, he had **no test**; It means real security property (not open-redirect)
   It was not locked. Here it is tested with a state table — including `/evil.com' (slash
   binary = protocol-relative), `/\evil.com`, `javascript:` and the full address.

3. **Guaranteed structure:** `next' is either omitted or is something that the same guard accepts;
   So the anonymous user never sees `next' which is ignored after login.
"""
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from core.db import BACKEND_SQLITE, ENV_PG_DSN
from web.auth import (_is_safe_next_url, _same_site_next_target, login_redirect_url,
                      same_site_next_target)


def _pin_sqlite_env(test):
    """The tests in this file work with temporary SQLite DB (PG environment should not affect)."""
    env = mock.patch.dict(os.environ, {"DB_BACKEND": BACKEND_SQLITE, ENV_PG_DSN: ""})
    env.start()
    test.addCleanup(env.stop)


# ─────────────────────────────────────────────────────────────────────
# 1) The validator itself — state table (no app, no DB)
# ─────────────────────────────────────────────────────────────────────
class SafeNextUrlValidationTests(unittest.TestCase):
    """`_is_safe_next_url` only accepts same-site path (locked back after login)."""

    ACCEPTED = (
        "/logs",
        "/logs?page=2",
        "/api/export/logs?format=csv&ip=10.0.0.1",
        "/dashboard/x",
    )
    REJECTED = (
        "",                                  # Absence of next ⇒ default
        "//evil.example.com/x",               # protocol-relative: it is dangerous without a scheme
        "/\\evil.example.com",                # Some browsers read `\` as `/`
        "http://evil.example.com",            # has a scheme
        "https://evil.example.com/x",
        "HTTPS://evil.example.com",           # Uppercase/lowercase letters are ineffective
        "javascript:alert(1)",                # A dangerous scheme
        "http:/evil.example.com",
    )

    def test_accepted_targets(self):
        """Verifies accepted targets."""
        for target in self.ACCEPTED:
            with self.subTest(next=target):
                self.assertTrue(_is_safe_next_url(target),
                                f"{target} باید پذیرفته شود (مسیر هم‌سایت با query)")

    def test_rejected_targets(self):
        """Verifies rejected targets."""
        for target in self.REJECTED:
            with self.subTest(next=target):
                self.assertFalse(_is_safe_next_url(target),
                                 f"{target} نباید به‌عنوان بازگشت پذیرفته شود")


# ─────────────────────────────────────────────────────────────────────
# 2) ``next'' construction helper + real app answers
# ─────────────────────────────────────────────────────────────────────
class _AppMixin(unittest.TestCase):

    """Helper class: app mixin."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        _pin_sqlite_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: os.chdir(self.old_cwd))
        from core.database import init_db
        from web import create_app
        init_db()
        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()

    def _make_admin(self, username="next_admin"):
        """Helper: make admin."""
        from models import User
        return User.create(username=username, email=f"{username}@test.io",
                           password="secret123", role="admin", is_verified=True)


class SameSiteTargetTests(_AppMixin):

    """Tests for same site target."""
    def test_target_keeps_path_and_query_and_drops_authority(self):
        """Verifies target keeps path and query and drops authority."""
        with self.app.test_request_context("/api/export/logs?format=csv&ip=10.0.0.1"):
            self.assertEqual(same_site_next_target(), "/api/export/logs?format=csv&ip=10.0.0.1")
            url = login_redirect_url()
        self.assertTrue(url.startswith("/login?next="), url)
        self.assertNotIn("://", url, "scheme+host نباید در بازتاب برود")
        self.assertNotIn("localhost", url)

    def test_root_path_without_query(self):
        """Verifies root path without query."""
        with self.app.test_request_context("/logs"):
            self.assertEqual(same_site_next_target(), "/logs")

    def test_unsafe_path_omits_next_entirely(self):
        """
        The protocol-relative path ⇒ `next` is not sent (instead of sending and
        ignoring it after logging in). Two layers are tested: pure function with raw input,
        and the actual request path (which Werkzeug may normalize ⇒ in both cases
        The result must be either None or Accepted; never an invalid `next').
        """
        self.assertIsNone(_same_site_next_target("//evil.example.com/x", ""))
        self.assertIsNone(_same_site_next_target("/\\evil.example.com", ""))
        self.assertEqual(_same_site_next_target("/logs", "page=2"), "/logs?page=2")
        with self.app.test_request_context("//evil.example.com/x"):
            target = same_site_next_target()
            self.assertTrue(target is None or _is_safe_next_url(target), repr(target))
            url = login_redirect_url()
            self.assertNotIn("://", urlparse(url).query or "", url)
            self.assertNotIn("//evil", url, "نشانی بیرونی نباید در URL بازگشت برود")

    def test_generated_next_is_always_accepted_by_the_guard(self):
        """Duso's guarantee: what we make, the consumer will accept."""
        for raw in ("/logs?page=2", "/a/b?x=1&y=2", "/metrics?ip=10.0.0.1"):
            with self.app.test_request_context(raw):
                target = same_site_next_target()
                with self.subTest(path=raw):
                    self.assertTrue(_is_safe_next_url(target), target)


class UnauthorizedResponseTests(_AppMixin):

    """Tests for unauthorized response."""
    def test_api_401_body_has_same_site_next(self):
        """Verifies api 401 body has same site next."""
        resp = self.client.get("/api/export/logs?format=csv")
        self.assertEqual(resp.status_code, 401)
        payload = resp.get_json()
        self.assertEqual(set(payload), {"error", "login_url"})
        parsed = urlparse(payload["login_url"])
        self.assertEqual(parsed.path, "/login", payload["login_url"])
        next_value = parse_qs(parsed.query).get("next", [""])[0]
        self.assertEqual(next_value, "/api/export/logs?format=csv")
        body = resp.get_data(as_text=True)
        self.assertNotIn("://", body, "هیچ نشانی کاملی نباید در بدنه باشد")
        self.assertLess(len(body), 300, "بدنه باید هنوز کوچک بماند")

    def test_page_401_redirect_location_has_same_site_next(self):
        """Verifies page 401 redirect location has same site next."""
        resp = self.client.get("/logs?page=3")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertTrue(location.startswith("/login?next="), location)
        self.assertNotIn("%3A%2F%2F", location, "scheme+host کدگذاری‌شده هم نباید باشد")
        self.assertEqual(parse_qs(urlparse(location).query)["next"][0], "/logs?page=3")


class LoginReturnTargetTests(_AppMixin):
    """Return behavior after login: `next` valid respected, external `next` ignored (not open-redirect)."""

    def _login(self, next_value):
        """Helper: login."""
        data = {"identifier": "next_admin", "password": "secret123"}
        if next_value is not None:
            data["next"] = next_value
        return self.client.post("/login", data=data)

    def test_safe_next_is_used(self):
        """Verifies safe next is used."""
        self._make_admin()
        resp = self._login("/logs?page=2")
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(urlparse(resp.headers["Location"]).path, "/logs")
        self.assertEqual(parse_qs(urlparse(resp.headers["Location"]).query).get("page"), ["2"])

    def test_absolute_next_is_ignored(self):
        """Verifies absolute next is ignored."""
        self._make_admin()
        resp = self._login("https://evil.example.com/steal")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/",
                         "next بیرونی باید دور ریخته شود و کاربر به خانه برود")

    def test_protocol_relative_next_is_ignored(self):
        """Verifies protocol relative next is ignored."""
        self._make_admin()
        resp = self._login("//evil.example.com/steal")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/",
                         "`//host` همان `/host` به‌نظر می‌رسد ولی بیرون از سایت است")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
