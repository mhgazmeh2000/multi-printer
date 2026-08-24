# -*- coding: utf-8 -*-
"""
تست‌های باگ شماره ۲ — ناهماهنگی baseline شمارنده‌های A3/A4 با print_total
در مسیر legacy/fallback توشیبا (core/collectors/toshiba.py).

قانون: فقط وقتی موتور رویداد snapshot را پذیرفته (print_total جلو رفته)
هیچ baseline دیگری — از جمله a3_total/a4_total — جلو می‌رود؛ دقیقاً مثل
printer_total/copy_total/fax_total/list_total.

SNMP در این تست‌ها کاملاً موک شده است؛ فقط منطق collector اجرا می‌شود.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

from core import store
from core.database import init_db
import core.collectors.base as base_mod
import core.collectors.toshiba as toshiba_mod

recorded_events = []


def _fake_add_event(ip, etype, details):
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class ToshibaFallbackSyncTests(unittest.TestCase):
    IP = "10.7.7.7"
    UP = 500_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        recorded_events.clear()
        self._orig_add_event = base_mod.add_event
        base_mod.add_event = _fake_add_event
        self._p_bulk = mock.patch.object(toshiba_mod, "snmp_get_bulk")
        self._p_scrape = mock.patch.object(toshiba_mod, "_scrape_toshiba_toners", return_value=None)
        self._p_walk = mock.patch.object(toshiba_mod, "_walk_toner_remaining", return_value=None)
        self.m_bulk = self._p_bulk.start()
        self._p_scrape.start()
        self._p_walk.start()

    def tearDown(self):
        self._p_bulk.stop()
        self._p_scrape.stop()
        self._p_walk.stop()
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ─── helpers ───
    def _raw(self, total, a3, a4, uptime):
        return {
            "print_total": total, "uptime": uptime,
            "print_fc": 0, "print_bw": total,
            "a3_total": a3, "a4_total": a4,
            "model": "e-STUDIO3015AC", "serial": "XYZ", "firmware": "T3",
        }

    def _seed_prev(self, total, a3, a4):
        store._prev.set(self.IP, {
            "print_total": total, "full_color": 0, "black_white": total,
            "a3_total": a3, "a4_total": a4,
            "a3_lagged": False, "a4_lagged": False,
            "toner_level": 60, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP,
        })
        recorded_events.clear()

    def _collect(self, total, a3, a4, uptime):
        self.m_bulk.return_value = self._raw(total, a3, a4, uptime)
        return toshiba_mod.collect_toshiba(self.IP, "Toshiba-Test", "public", time.time())

    def _prev(self):
        return store._prev.get(self.IP) or {}

    def _events(self, etype):
        return [e for e in recorded_events if e["type"] == etype]

    # ─── سناریو ۱ — poll عادی: همه baseline‌ها جلو می‌روند ───
    def test_normal_poll_advances_all_baselines(self):
        self._seed_prev(1000, 300, 700)
        self._collect(1005, 302, 703, self.UP + 6_000)
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1005)
        self.assertEqual(prev["a3_total"], 302)
        self.assertEqual(prev["a4_total"], 703)

    # ─── سناریو ۲ — anomaly: هیچ baseline‌ای (حتی A3/A4) جلو نمی‌رود ───
    def test_anomaly_poll_does_not_advance_a3_a4(self):
        self._seed_prev(1000, 300, 700)
        # افت شمارنده بدون ریبوت (uptime پایدار) → COUNTER_ANOMALY → snapshot رد
        self._collect(200, 350, 700, self.UP + 6_000)
        self.assertTrue(self._events("COUNTER_ANOMALY"))
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1000, "snapshot anomaly نباید جلو برود")
        self.assertEqual(prev["a3_total"], 300, "baseline A3 بیرون از پذیرش جلو رفته بود")
        self.assertEqual(prev["a4_total"], 700, "baseline A4 بیرون از پذیرش جلو رفته بود")
        self.assertFalse(prev.get("a3_lagged"))
        self.assertFalse(prev.get("a4_lagged"))

    # ─── سناریو ۳ — poll سالم بعدی: دلتاهای A3/A4 با print_total هم‌راستا ───
    def test_next_healthy_poll_deltas_aligned(self):
        self._seed_prev(1000, 300, 700)
        self._collect(200, 350, 700, self.UP + 6_000)      # anomaly (رد)
        recorded_events.clear()
        self._collect(1015, 303, 712, self.UP + 12_000)    # سالم: +15 = 3 بزرگ + 12 کوچک
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1015)
        self.assertEqual(prev["a3_total"], 303)
        self.assertEqual(prev["a4_total"], 712)
        prints = self._events("PRINT")
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0]["details"]["pages"], 15)
        split = prints[0]["details"].get("paper_split") or {}
        # دلتا A3 از baseline درست (300→303) محاسبه شود نه از مقدار anomaly (350)
        self.assertEqual(split.get("large"), 3)
        self.assertEqual(split.get("small"), 12)
        self.assertEqual(split.get("large", 0) + split.get("small", 0), 15)

    # ─── مکمل: overflow pending هم A3/A4 را فریز می‌کند ───
    def test_overflow_pending_does_not_advance_a3_a4(self):
        self._seed_prev(1000, 300, 700)
        # پرش غیرمعمول (بیش از حد منطقی) → PRINT_OVERFLOW pending → snapshot معلق
        self._collect(5000, 3200, 1800, self.UP + 6_000)
        self.assertTrue(self._events("PRINT_OVERFLOW"))
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1000)
        self.assertEqual(prev["a3_total"], 300)
        self.assertEqual(prev["a4_total"], 700)


if __name__ == "__main__":
    unittest.main()
