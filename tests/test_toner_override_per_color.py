# -*- coding: utf-8 -*-
"""تست‌های override دستی تونر per-color.

باگ اصلی: reset دستی سه رنگ پشت‌سرهم (black → cyan → yellow) فقط آخرین رنگ
را نگه می‌داشت؛ پس از یک poll، دو رنگ قبلی به مقدار خام SNMP برمی‌گشتند.
این فایل تضمین می‌کند:
  ۱) هر رنگ اسلات مستقل دارد و چند reset پشت‌سرهم همه حفظ می‌شوند.
  ۲) apply_toner_override برای «همه» رنگ‌های reset‌شده مقدار override می‌دهد.
  ۳) get_pages_since_last_reset جدیدترین (کمینه) reset را برمی‌گرداند.
  ۴) عقب‌رفتن شمارنده (سرویس/ریست) فقط اسلاتِ همان دستگاه را باطل می‌کند.
  ۵) ذخیره/بارگذاری دیتابیس (toner_overrides) رفت‌وبرگشت درست دارد.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.collectors.base import (  # noqa: E402
    _normalize_overrides,
    _overrides_valid_after_total,
    _mirror_legacy_override_keys,
    apply_toner_override,
    get_pages_since_last_reset,
)


class OverrideHelpersTests(unittest.TestCase):
    def test_normalize_legacy_single_slot(self):
        prev = {
            "manual_override": 1,
            "override_color": "black",
            "override_start_total": 5000,
            "override_start_toner": 90,
        }
        ov = _normalize_overrides(prev)
        self.assertEqual(ov, {"black": {"start_total": 5000, "start_toner": 90}})

    def test_normalize_keeps_per_color_slots(self):
        prev = {"toner_overrides": {
            "black": {"start_total": 5000, "start_toner": 90},
            "cyan": {"start_total": 5000, "start_toner": 100},
        }}
        self.assertEqual(_normalize_overrides(prev), prev["toner_overrides"])

    def test_normalize_empty(self):
        self.assertEqual(_normalize_overrides({}), {})
        self.assertEqual(_normalize_overrides({"manual_override": 0}), {})

    def test_expiry_only_invalidates_its_slot(self):
        ov = {
            "black": {"start_total": 5000, "start_toner": 90},
            "cyan": {"start_total": 4000, "start_toner": 100},
        }
        # شمارنده به 4500 برگشته: فقط black (شروع 5000) نامعتبر می‌شود
        out = _overrides_valid_after_total(ov, 4500)
        self.assertNotIn("black", out)
        self.assertIn("cyan", out)

    def test_mirror_legacy_keys_with_and_without_slots(self):
        new_prev = {}
        _mirror_legacy_override_keys(new_prev, {
            "black": {"start_total": 10, "start_toner": 80},
            "yellow": {"start_total": 12, "start_toner": 90},
        })
        self.assertEqual(new_prev["manual_override"], 1)
        self.assertEqual(new_prev["override_color"], "yellow")
        self.assertEqual(new_prev["override_start_toner"], 90)

        new_prev = {}
        _mirror_legacy_override_keys(new_prev, {})
        self.assertEqual(new_prev["manual_override"], 0)
        self.assertIsNone(new_prev["override_color"])


class PagesSinceResetTests(unittest.TestCase):
    def test_single_slot(self):
        prev = {"toner_overrides": {"black": {"start_total": 5000, "start_toner": 90}}}
        self.assertEqual(get_pages_since_last_reset(prev, 5100), 100)

    def test_multi_slot_returns_min(self):
        prev = {"toner_overrides": {
            "black": {"start_total": 4000, "start_toner": 90},
            "cyan": {"start_total": 5050, "start_toner": 100},
        }}
        # black: 1200 صفحه، cyan: 150 صفحه → جدیدترین reset برنده است
        self.assertEqual(get_pages_since_last_reset(prev, 5200), 150)

    def test_negative_slot_ignored(self):
        prev = {"toner_overrides": {
            "black": {"start_total": 6000, "start_toner": 90},  # عقب‌رفته
            "cyan": {"start_total": 5000, "start_toner": 100},
        }}
        self.assertEqual(get_pages_since_last_reset(prev, 5200), 200)

    def test_no_override_returns_none(self):
        self.assertIsNone(get_pages_since_last_reset({}, 100))
        self.assertIsNone(get_pages_since_last_reset({"toner_overrides": {}}, 100))


class ApplyOverrideTests(unittest.TestCase):
    def setUp(self):
        self._patcher = mock.patch("core.collectors.base.store._prev")
        self.fake_prev = self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_all_colors_keep_override(self):
        """سناریوی واقعی باگ: سه reset پشت‌سرهم → هر سه باید override بمانند."""
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {
                "black": {"start_total": 100000, "start_toner": 90},
                "cyan": {"start_total": 100000, "start_toner": 100},
                "yellow": {"start_total": 100000, "start_toner": 90},
            },
        }
        # 50 صفحه چاپ شده: افت 2% (round(50*100/2000))
        self.assertEqual(apply_toner_override("1.1.1.1", 100050, 12, color="black"), 88)
        self.assertEqual(apply_toner_override("1.1.1.1", 100050, 12, color="cyan"), 98)
        self.assertEqual(apply_toner_override("1.1.1.1", 100050, 12, color="yellow"), 88)

    def test_color_without_override_returns_none(self):
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {"black": {"start_total": 100000, "start_toner": 90}},
        }
        # magenta هرگز reset نشده → raw SNMP دست‌نخورده می‌ماند
        self.assertIsNone(apply_toner_override("1.1.1.1", 100050, 55, color="magenta"))

    def test_counter_regression_invalidates_slot(self):
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {"black": {"start_total": 100000, "start_toner": 90}},
        }
        # شمارنده به قبل از شروع override برگشته (سرویس) → مقدار خام
        self.assertEqual(apply_toner_override("1.1.1.1", 99000, 55, color="black"), 55)

    def test_zero_pages_returns_start_toner(self):
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {"black": {"start_total": 100000, "start_toner": 90}},
        }
        self.assertEqual(apply_toner_override("1.1.1.1", 100000, 55, color="black"), 90)

    def test_legacy_single_slot_still_works(self):
        """مهاجرت: state قدیمی (تک‌اسلات) بدون toner_overrides هم کار می‌کند."""
        self.fake_prev.get.return_value = {
            "manual_override": 1,
            "override_color": "black",
            "override_start_total": 100000,
            "override_start_toner": 90,
            "yield_per_page": 2000,
        }
        self.assertEqual(apply_toner_override("1.1.1.1", 100100, 12, color="black"), 85)
        self.assertIsNone(apply_toner_override("1.1.1.1", 100100, 12, color="cyan"))


class OverridePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.getcwd()
        os.chdir(self._tmp.name)
        from core.database import init_db
        init_db()

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()

    def test_toner_overrides_roundtrip(self):
        from core.database import save_printer_counters, load_printer_counters
        data = {
            "print_total": 100050,
            "manual_override": 1,
            "override_color": "yellow",
            "toner_overrides": {
                "black": {"start_total": 100000, "start_toner": 90},
                "cyan": {"start_total": 100010, "start_toner": 100},
                "yellow": {"start_total": 100050, "start_toner": 90},
            },
        }
        save_printer_counters("10.0.0.9", data)
        loaded = load_printer_counters("10.0.0.9")
        self.assertEqual(loaded["toner_overrides"], data["toner_overrides"])

    def test_toner_overrides_survive_prevstore_merge(self):
        """set بعدی بدون toner_overrides نباید اسلات‌ها را پاک کند."""
        from core import store as store_mod
        ps = store_mod.PrevStore()
        try:
            ps.set("10.0.0.8", {"print_total": 100, "toner_overrides": {
                "black": {"start_total": 50, "start_toner": 90}}})
            ps.set("10.0.0.8", {"print_total": 150})  # بدون toner_overrides
            got = ps.get("10.0.0.8")
            self.assertEqual(got["toner_overrides"], {
                "black": {"start_total": 50, "start_toner": 90}})
            self.assertEqual(got["print_total"], 150)
        finally:
            ps.delete("10.0.0.8")


if __name__ == "__main__":
    unittest.main()
