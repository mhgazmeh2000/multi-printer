# -*- coding: utf-8 -*-
"""
تست‌های فیکس باگ «سایز کاغذ اشتباه/نادرست در لاگ‌ها».

سناریوهای پوشش‌داده‌شده بر اساس بررسی ریشه‌ای کد:
۱) برچسب‌گذاری فقط وقتی معتبر است که دلتای شمارنده‌های کاغذ با دلتای شمارنده‌ی
   کل هم‌راستا باشد (هویت اثبات‌شده‌ی Toshiba: a3_total + a4_total == print_total).
۲) baseline مسموم با ۰ (مسیر legacy در خواندن ناموفق SNMP) نباید باعث دلتای
   غول‌آسا و برچسب اشتباه شود — بازتولید باگ تولیدی: «Large/Mixed برای چاپ خالص A4».
۳) خواندن ناموفق نباید PrevStore را با ۰ بازنویسی کند.
۴) در حالت Mixed تفکیک دقیق بزرگ/کوچک در جزئیات رویداد (paper_split) ثبت شود.
"""
import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod
from core.collectors.base import (
    attribute_paper_size,
    PAPER_SIZE_LARGE_LABEL,
    PAPER_SIZE_SMALL_LABEL,
    PAPER_SIZE_MIXED_LABEL,
)
import core.collectors.toshiba as toshiba_mod

recorded_events = []


def _fake_add_event(ip, etype, details):
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class AttributePaperSizeUnitTests(unittest.TestCase):
    """تست مستقیم تابع attribute_paper_size (بدون SNMP/دیتابیس)."""

    def test_aligned_small_only(self):
        size, split, reliable = attribute_paper_size(
            total=10005, prev_total=10000,
            a3_total=2000, prev_a3=2000,
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_SMALL_LABEL)
        self.assertEqual(split, {"large": 0, "small": 5})
        self.assertTrue(reliable)

    def test_aligned_large_only(self):
        size, split, reliable = attribute_paper_size(
            total=10003, prev_total=10000,
            a3_total=2003, prev_a3=2000,
            a4_total=8000, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_LARGE_LABEL)
        self.assertEqual(split, {"large": 3, "small": 0})
        self.assertTrue(reliable)

    def test_aligned_mixed_with_exact_split(self):
        size, split, reliable = attribute_paper_size(
            total=10007, prev_total=10000,
            a3_total=2002, prev_a3=2000,
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_MIXED_LABEL)
        self.assertEqual(split, {"large": 2, "small": 5})
        self.assertTrue(reliable)

    def test_stale_snapshot_is_rejected_not_mislabeled(self):
        # سناریوی تولیدی: baseline شاخه‌ی a3 کهنه است (خواندن ناموفق قبلی)،
        # دلتای a3 دو بازه را پوشش می‌دهد؛ جمع دلتاها ≠ دلتای کل.
        # قبلاً برچسب «Large» (یا Mixed) اشتباه ثبت می‌شد؛ حالا باید None شود.
        size, split, reliable = attribute_paper_size(
            total=10005, prev_total=10000,          # واقعاً فقط ۵ صفحه کوچک
            a3_total=2010, prev_a3=2000,            # دلتای قلابی ۱۰ (دو بازه)
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertFalse(reliable)

    def test_zero_poisoned_baseline_is_rejected(self):
        # baseline مسموم با ۰: دلتا = کل شمارنده. قبلاً «Mixed» ثبت می‌شد.
        size, split, reliable = attribute_paper_size(
            total=10005, prev_total=10000,
            a3_total=2000, prev_a3=0,
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertFalse(reliable)

    def test_negative_delta_is_rejected(self):
        size, split, reliable = attribute_paper_size(
            total=10002, prev_total=10000,
            a3_total=1500, prev_a3=2000,            # شمارنده عقب رفته!
            a4_total=8502, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertFalse(reliable)

    def test_missing_data_returns_unknown(self):
        for args in [
            dict(total=10005, prev_total=10000, a3_total=None, prev_a3=2000, a4_total=8005, prev_a4=8000),
            dict(total=10005, prev_total=10000, a3_total=2000, prev_a3=None, a4_total=8005, prev_a4=8000),
        ]:
            size, split, reliable = attribute_paper_size(ip="1.1.1.1", **args)
            self.assertIsNone(size)
            self.assertFalse(reliable)

    def test_no_change_is_silently_reliable(self):
        size, split, reliable = attribute_paper_size(
            total=10000, prev_total=10000,
            a3_total=2000, prev_a3=2000,
            a4_total=8000, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertTrue(reliable)

    def test_first_poll_without_total_baseline_still_labels(self):
        # بدون baseline شمارنده‌ی کل (اما با baseline کاغذ از DB)، برچسب بر اساس
        # دلتای خالص کاغذ مجاز است — ولی تفکیک همچنان دقیق گزارش می‌شود.
        size, split, reliable = attribute_paper_size(
            total=None, prev_total=None,
            a3_total=2004, prev_a3=2000,
            a4_total=8000, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_LARGE_LABEL)
        self.assertEqual(split, {"large": 4, "small": 0})


class LegacyToshibaPaperSizeTests(unittest.TestCase):
    """بازتولید باگ تولیدی در مسیر legacy collector (fallback)."""

    IP = "10.8.8.8"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        recorded_events.clear()
        with store.printers_lock:
            store.PRINTERS[:] = []

        self._orig_add_event = base_mod.add_event
        self._orig_bulk = toshiba_mod.snmp_get_bulk
        self._orig_scrape = toshiba_mod._scrape_toshiba_toners
        self._orig_walk = toshiba_mod._walk_toner_remaining
        base_mod.add_event = _fake_add_event
        toshiba_mod._scrape_toshiba_toners = lambda ip: {}
        toshiba_mod._walk_toner_remaining = lambda ip, community: None

        self.raw = {}

        def _fake_bulk(ip, oids, community):
            return dict(self.raw)

        toshiba_mod.snmp_get_bulk = _fake_bulk

    def tearDown(self):
        base_mod.add_event = self._orig_add_event
        toshiba_mod.snmp_get_bulk = self._orig_bulk
        toshiba_mod._scrape_toshiba_toners = self._orig_scrape
        toshiba_mod._walk_toner_remaining = self._orig_walk
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _base_raw(self, total, uptime=900_000_000, **extra):
        raw = {
            "print_total": total,
            "print_fc": 0,
            "print_bw": total,
            "uptime": uptime,
            "model": "e-STUDIO3540",
        }
        raw.update(extra)
        return raw

    def test_failed_read_does_not_poison_prev_then_labels_correctly(self):
        # baseline سالم: ۱۰٬۰۰۰ = ۲٬۰۰۰ بزرگ + ۸٬۰۰۰ کوچک
        store._prev.set(self.IP, {
            "print_total": 10000, "uptime": 800_000_000,
            "a3_total": 2000, "a4_total": 8000,
        })
        recorded_events.clear()

        # poll ۱: خواندن شمارنده‌های کاغذ fail می‌شود (کلیدها غایب‌اند)
        self.raw = self._base_raw(total=10000, uptime=800_006_000)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())

        prev_after_fail = store._prev.get(self.IP)
        # ✅ باگ قدیمی: اینجا a3_total/a4_total با ۰ بازنویسی می‌شدند
        self.assertEqual(prev_after_fail.get("a3_total"), 2000)
        self.assertEqual(prev_after_fail.get("a4_total"), 8000)

        # poll ۲: ۵ صفحه A4 خالص چاپ شده؛ شمارنده‌های کاغذ دوباره خوانده می‌شوند
        self.raw = self._base_raw(total=10005, uptime=800_012_000,
                                  a3_total=2000, a4_total=8005)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())

        prints = [e for e in recorded_events if e["type"] == "PRINT"]
        self.assertTrue(prints, "رویداد PRINT باید ثبت شود")
        # ✅ باگ قدیمی: paper_size == "Mixed" (به‌خاطر دلتای ۲۰۰۰ تایی قلابی) ثبت می‌شد
        self.assertEqual(prints[-1]["details"].get("pages"), 5)
        self.assertEqual(prints[-1]["details"].get("paper_size"), PAPER_SIZE_SMALL_LABEL)
        self.assertEqual(prints[-1]["details"].get("paper_split"), {"large": 0, "small": 5})

    def test_historically_poisoned_baseline_does_not_mislabel(self):
        # baseline از قبل با ۰ مسموم شده (داده‌های تاریخی DB)؛ نباید برچسب اشتباه بزند
        store._prev.set(self.IP, {
            "print_total": 10000, "uptime": 800_000_000,
            "a3_total": 0, "a4_total": 8000,
        })
        recorded_events.clear()

        self.raw = self._base_raw(total=10005, uptime=800_006_000,
                                  a3_total=2000, a4_total=8005)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())

        prints = [e for e in recorded_events if e["type"] == "PRINT"]
        self.assertTrue(prints, "رویداد PRINT باید ثبت شود")
        # ✅ دلتای a3+a4 = ۱۰٬۰۰۵ ≠ ۵ → نسبت‌دهی نامعتبر → سایز نامشخص (نه Mixed!)
        self.assertEqual(prints[-1]["details"].get("pages"), 5)
        self.assertIsNone(prints[-1]["details"].get("paper_size"))

        # poll بعدی: baseline اصلاح شده، برچسب دوباره معتبر می‌شود
        self.raw = self._base_raw(total=10007, uptime=800_012_000,
                                  a3_total=2000, a4_total=8007)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())
        prints = [e for e in recorded_events if e["type"] == "PRINT"]
        self.assertEqual(prints[-1]["details"].get("paper_size"), PAPER_SIZE_SMALL_LABEL)

    def test_mixed_print_records_exact_split_in_details(self):
        store._prev.set(self.IP, {
            "print_total": 10000, "uptime": 800_000_000,
            "a3_total": 2000, "a4_total": 8000,
        })
        recorded_events.clear()

        self.raw = self._base_raw(total=10007, uptime=800_006_000,
                                  a3_total=2002, a4_total=8005)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())

        prints = [e for e in recorded_events if e["type"] == "PRINT"]
        self.assertTrue(prints)
        d = prints[-1]["details"]
        self.assertEqual(d.get("paper_size"), PAPER_SIZE_MIXED_LABEL)
        # ✅ تفکیک دقیق برای حسابرسی لاگ
        self.assertEqual(d.get("paper_split"), {"large": 2, "small": 5})


if __name__ == "__main__":
    unittest.main()
