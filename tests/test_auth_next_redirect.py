# -*- coding: utf-8 -*-
r"""
تست‌های E.6.6 (TD-002) — `next` در مسیر احراز هویت: هم‌سایت، و در هر دو سو اعتبارسنجی.

سه ادعا قفل می‌شود
------------------
۱. **بازتابِ کمینه:** پاسخ ۴۰۱ نباید `request.url` کامل (scheme+host) را برگرداند.
   پیش از E.6.6، `login_url` چنین بود:
   `/login?next=http://localhost/api/export/logs?fmt%3Dcsv`
   و حالا `/login?next=/api/export/logs?fmt=csv` — همان UX (بازگشت به همان صفحه)،
   بدون نشانی میزبان، بدون محتوای تکراری در لاگ‌ها.

۲. **guard مصرف‌کننده:** `_is_safe_next_url` تنها دروازهٔ بازگشت پس از ورود است و
   تا پیش از این **هیچ تستی نداشت**؛ یعنی خاصیت امنیتیِ واقعی (open-redirect نبودن)
   قفل‌نشده بود. این‌جا با یک جدول حالت آزموده می‌شود — شامل `//evil.com` (اسلاش
   دوتایی = protocol-relative)، `/\evil.com`، `javascript:` و آدرس کامل.

۳. **ساختار تضمین‌شده:** `next` یا حذف می‌شود یا چیزی است که همان guard می‌پذیرد؛
   پس کاربر بی‌هویت هرگز `next`-ای نمی‌بیند که بعد از ورود نادیده گرفته شود.
"""
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

from core.db import BACKEND_SQLITE, ENV_PG_DSN

from web.auth import (_is_safe_next_url, _same_site_next_target, login_redirect_url,
                      same_site_next_target)


def _pin_sqlite_env(test):
    """تست‌های این فایل با DB موقت SQLite کار می‌کنند (محیط PG نباید اثر بگذارد)."""
    env = mock.patch.dict(os.environ, {"DB_BACKEND": BACKEND_SQLITE, ENV_PG_DSN: ""})
    env.start()
    test.addCleanup(env.stop)


# ─────────────────────────────────────────────────────────────────────
# ۱) خودِ validator — جدول حالت (بدون app، بدون DB)
# ─────────────────────────────────────────────────────────────────────
class SafeNextUrlValidationTests(unittest.TestCase):
    """`_is_safe_next_url` فقط مسیر هم‌سایت را می‌پذیرد (قفلِ بازگشت پس از ورود)."""

    ACCEPTED = (
        "/logs",
        "/logs?page=2",
        "/api/export/logs?format=csv&ip=10.0.0.1",
        "/dashboard/x",
    )
    REJECTED = (
        "",                                  # نبودِ next ⇒ پیش‌فرض
        "//evil.example.com/x",               # protocol-relative: بدون scheme خطرناک است
        "/\\evil.example.com",                # برخی مرورگرها `\` را `/` می‌خوانند
        "http://evil.example.com",            # scheme‌دار
        "https://evil.example.com/x",
        "HTTPS://evil.example.com",           # بزرگ/کوچک‌حرف بی‌اثر
        "javascript:alert(1)",                # scheme‌دارِ خطرناک
        "http:/evil.example.com",
    )

    def test_accepted_targets(self):
        for target in self.ACCEPTED:
            with self.subTest(next=target):
                self.assertTrue(_is_safe_next_url(target),
                                f"{target} باید پذیرفته شود (مسیر هم‌سایت با query)")

    def test_rejected_targets(self):
        for target in self.REJECTED:
            with self.subTest(next=target):
                self.assertFalse(_is_safe_next_url(target),
                                 f"{target} نباید به‌عنوان بازگشت پذیرفته شود")


# ─────────────────────────────────────────────────────────────────────
# ۲) هلپر ساخت `next` + پاسخ‌های واقعی اپ
# ─────────────────────────────────────────────────────────────────────
class _AppMixin(unittest.TestCase):

    def setUp(self):
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
        from models import User
        return User.create(username=username, email=f"{username}@test.io",
                           password="secret123", role="admin", is_verified=True)


class SameSiteTargetTests(_AppMixin):

    def test_target_keeps_path_and_query_and_drops_authority(self):
        with self.app.test_request_context("/api/export/logs?format=csv&ip=10.0.0.1"):
            self.assertEqual(same_site_next_target(), "/api/export/logs?format=csv&ip=10.0.0.1")
            url = login_redirect_url()
        self.assertTrue(url.startswith("/login?next="), url)
        self.assertNotIn("://", url, "scheme+host نباید در بازتاب برود")
        self.assertNotIn("localhost", url)

    def test_root_path_without_query(self):
        with self.app.test_request_context("/logs"):
            self.assertEqual(same_site_next_target(), "/logs")

    def test_unsafe_path_omits_next_entirely(self):
        """
        مسیر protocol-relative ⇒ `next` فرستاده نمی‌شود (به‌جای فرستادن و
        نادیده‌گرفتنش پس از ورود). دو لایه آزموده می‌شود: تابع خالص با ورودی خام،
        و مسیر درخواست واقعی (که Werkzeug ممکن است نرمال کند ⇒ در هر دو حالت
        نتیجه باید یا None باشد یا پذیرفته‌شده؛ هیچ‌گاه یک `next` بی‌اعتبار نه).
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
        """تضمینِ دوسو: چه می‌سازیم، همان را مصرف‌کننده می‌پذیرد."""
        for raw in ("/logs?page=2", "/a/b?x=1&y=2", "/metrics?ip=10.0.0.1"):
            with self.app.test_request_context(raw):
                target = same_site_next_target()
                with self.subTest(path=raw):
                    self.assertTrue(_is_safe_next_url(target), target)


class UnauthorizedResponseTests(_AppMixin):

    def test_api_401_body_has_same_site_next(self):
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
        resp = self.client.get("/logs?page=3")
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertTrue(location.startswith("/login?next="), location)
        self.assertNotIn("%3A%2F%2F", location, "scheme+host کدگذاری‌شده هم نباید باشد")
        self.assertEqual(parse_qs(urlparse(location).query)["next"][0], "/logs?page=3")


class LoginReturnTargetTests(_AppMixin):
    """رفتار بازگشت پس از ورود: `next` معتبر محترم، `next` بیرونی نادیده (open-redirect نه)."""

    def _login(self, next_value):
        data = {"identifier": "next_admin", "password": "secret123"}
        if next_value is not None:
            data["next"] = next_value
        return self.client.post("/login", data=data)

    def test_safe_next_is_used(self):
        self._make_admin()
        resp = self._login("/logs?page=2")
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        self.assertEqual(urlparse(resp.headers["Location"]).path, "/logs")
        self.assertEqual(parse_qs(urlparse(resp.headers["Location"]).query).get("page"), ["2"])

    def test_absolute_next_is_ignored(self):
        self._make_admin()
        resp = self._login("https://evil.example.com/steal")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/",
                         "next بیرونی باید دور ریخته شود و کاربر به خانه برود")

    def test_protocol_relative_next_is_ignored(self):
        self._make_admin()
        resp = self._login("//evil.example.com/steal")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/",
                         "`//host` همان `/host` به‌نظر می‌رسد ولی بیرون از سایت است")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
