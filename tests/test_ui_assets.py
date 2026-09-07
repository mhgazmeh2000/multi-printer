# -*- coding: utf-8 -*-
"""
گاردهای ایستای سازگاری UI — جلوگیری از تکرار باگ ReferenceError در پنل‌ها.

باگ واقعی (کنسول مرورگر): پنل تنظیمات سیستم در users.html تابع showToast را
صدا می‌زد ولی فقط window.toast تعریف شده بود → saveSystemSettings/testSystemEmail
به‌طور کامل می‌شکستند. این تست تضمین می‌کند هر قالبی که نامی را صدا می‌زند،
همان نام را هم تعریف کرده باشد (یا فایل JS ارائه‌دهنده را لود کند).
"""
import glob
import os
import re
import unittest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "templates")
STATIC_CSS = os.path.join(os.path.dirname(__file__), "..", "web", "static", "css", "style.css")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class UiAssetConsistencyTests(unittest.TestCase):
    def test_showtoast_defined_wherever_called(self):
        """هر قالبی که showToast صدا می‌زند باید خودش تعریفش کند یا legacy-mode.js را لود کند."""
        offenders = []
        for path in glob.glob(os.path.join(TEMPLATES_DIR, "*.html")):
            content = _read(path)
            called = re.search(r"showToast\s*\(", content)
            defines = "function showToast" in content or "showToast =" in content
            bundled = "legacy-mode.js" in content
            if called and not (defines or bundled):
                offenders.append(os.path.basename(path))
        self.assertEqual(offenders, [],
                         f"قالب‌هایی که showToast را بدون تعریف صدا می‌زنند: {offenders}")

    def test_toast_element_and_styles_present(self):
        """المان #toast در قالب‌های استفاده‌کننده و استایلش در style.css موجود باشد."""
        css = _read(STATIC_CSS)
        self.assertIn("#toast{", css)
        self.assertIn("#toast.show", css)
        for name in ("users.html", "dashboard.html"):
            content = _read(os.path.join(TEMPLATES_DIR, name))
            if "function showToast" in content or "window.toast" in content:
                self.assertIn('id="toast"', content,
                              f"{name} توابع toast دارد ولی المان #toast ندارد")

    def test_showtoast_type_mapping_covers_error(self):
        """نگاشت showToast باید «error» را به کلاس e بفرستد (فراخوان‌های واقعی پنل)."""
        content = _read(os.path.join(TEMPLATES_DIR, "users.html"))
        m = re.search(r"function showToast\(message, type\) \{(.*?)\n\}", content, re.S)
        self.assertIsNotNone(m, "showToast آداپتور در users.html پیدا نشد")
        body = m.group(1)
        self.assertIn("error", body)
        self.assertRegex(body, r"error:\s*'e'")

    def test_cartridge_identity_display_uses_backend_value_only(self):
        content = _read(os.path.join(os.path.dirname(__file__), "..", "web", "static", "js", "dashboard.js"))
        self.assertIn("p.cartridge_identity_type", content)
        self.assertIn("const identityValue = hasValidIdentity ? String(cartChipId) : 'N/A';", content)
        self.assertIn("شناسه‌ی معتبر Chip ID/Serial برای این Cartridge در backend موجود نیست", content)
        self.assertNotIn("title=\"سریال دستگاه (Toshiba — بدون شناسه‌ی تراشه)\"", content)


if __name__ == "__main__":
    unittest.main()
