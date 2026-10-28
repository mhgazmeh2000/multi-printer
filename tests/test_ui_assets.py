# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_ui_assets.py
# @brief:        Static guards that keep the web UI scripts free of known ReferenceError bugs.
# @details:      Responsibilities: Check that UI assets reference only defined functions.
#                Main API: UiAssetConsistencyTests
#                Side effects: file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Static UI compatibility guards — prevent the ReferenceError bug from recurring in panels.

Actual bug (browser console): system settings panel in users.html showToast function
It called, but only window.toast was defined → saveSystemSettings/testSystemEmail
They broke completely. This test ensures that any format that calls a name,
define the same name (or load the provider's JS file).
"""
import glob
import os
import re
import unittest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "templates")
STATIC_CSS = os.path.join(os.path.dirname(__file__), "..", "web", "static", "css", "style.css")


def _read(path):
    """Helper: read."""
    with open(path, encoding="utf-8") as f:
        return f.read()


class UiAssetConsistencyTests(unittest.TestCase):
    """Tests for ui asset consistency."""
    def test_showtoast_defined_wherever_called(self):
        """Any mode that calls showToast must define itself or load legacy-mode.js."""
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
        """The #toast element should be available in user templates and its style in style.css."""
        css = _read(STATIC_CSS)
        self.assertIn("#toast{", css)
        self.assertIn("#toast.show", css)
        for name in ("users.html", "dashboard.html"):
            content = _read(os.path.join(TEMPLATES_DIR, name))
            if "function showToast" in content or "window.toast" in content:
                self.assertIn('id="toast"', content,
                              f"{name} توابع toast دارد ولی المان #toast ندارد")

    def test_showtoast_type_mapping_covers_error(self):
        """The showToast mapping should send "error" to the e class (the actual panel calls)."""
        content = _read(os.path.join(TEMPLATES_DIR, "users.html"))
        m = re.search(r"function showToast\(message, type\) \{(.*?)\n\}", content, re.S)
        self.assertIsNotNone(m, "showToast آداپتور در users.html پیدا نشد")
        body = m.group(1)
        self.assertIn("error", body)
        self.assertRegex(body, r"error:\s*'e'")

    def test_cartridge_identity_display_uses_backend_value_only(self):
        """Verifies cartridge identity display uses backend value only."""
        content = _read(os.path.join(os.path.dirname(__file__), "..", "web", "static", "js", "dashboard.js"))
        self.assertIn("p.cartridge_identity_type", content)
        self.assertIn("const identityValue = hasValidIdentity ? String(cartChipId) : 'N/A';", content)
        self.assertIn("شناسه‌ی معتبر Chip ID/Serial برای این Cartridge در backend موجود نیست", content)
        self.assertNotIn("title=\"سریال دستگاه (Toshiba — بدون شناسه‌ی تراشه)\"", content)


if __name__ == "__main__":
    unittest.main()
