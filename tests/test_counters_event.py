import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod

# رویدادها را به‌جای دیتابیس در حافظه جمع می‌کنیم
recorded_events = []


def _fake_add_event(ip, etype, details):
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class CountersEventTests(unittest.TestCase):
    """تست‌های فیکس باگ لاگ شمارنده (بر اساس شواهد واقعی logs.db تولیدی)."""

    IP = "10.9.9.9"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        # ریست PrevStore (کش in-memory نیز باید پاک شود)
        store._prev._cache.clear()
        recorded_events.clear()
        # ریست MarkeStore
        with store.printers_lock:
            store.PRINTERS[:] = []
        self._orig_add_event = base_mod.add_event
        base_mod.add_event = _fake_add_event

    def tearDown(self):
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _seed_prev(self, **data):
        base = {
            "print_total": 10000,
            "full_color": 0,
            "black_white": 10000,
            "toner_level": 60,
            "alert_codes": [],
            "last_alert_codes": [],
            "uptime": 500_000_000,
        }
        base.update(data)
        store._prev.set(self.IP, base)
        recorded_events.clear()

    def _types(self):
        return [e["type"] for e in recorded_events]

    # ۱. شواهد واقعی: uptime=0 (خواندن ناموفق) نباید COUNTER_RESET بسازد
    def test_zero_uptime_is_not_reboot(self):
        self._seed_prev(print_total=160000, uptime=559_638_300, yield_per_page=4100)
        base_mod._counters_event(self.IP, 158000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=158000, uptime=0)
        self.assertNotIn("COUNTER_RESET", self._types())
        self.assertIn("COUNTER_ANOMALY", self._types())
        # snapshot باید حفظ شود
        self.assertEqual(store._prev.get(self.IP)["print_total"], 160000)

    # ۲. سناریوی واقعی تولیدی: total به‌صورت موقت صفر/ناخوانا + uptime=0
    #    قبلاً: COUNTER_RESET + پاک شدن yield=4100 و override → خرابی تونر
    def test_glitch_zero_total_and_zero_uptime_keeps_snapshot_and_yield(self):
        self._seed_prev(print_total=160000, uptime=559_638_300,
                        yield_per_page=4100, manual_override=1,
                        override_start_total=150000, override_start_toner=100,
                        override_color="black")
        base_mod._counters_event(self.IP, 0, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=0, uptime=0)
        self.assertNotIn("COUNTER_RESET", self._types())
        self.assertIn("SNMP_COUNTER_READ_ERROR", self._types())
        after = store._prev.get(self.IP)
        self.assertEqual(after["print_total"], 160000)
        self.assertEqual(after["yield_per_page"], 4100)
        self.assertEqual(after["manual_override"], 1)

    # ۳. بازگشت شمارنده بعد از glitch نباید PRINT تقلبی بسازد → pending → PRINT_GAP
    def test_counter_recovery_produces_gap_not_print(self):
        self._seed_prev(print_total=160000, uptime=559_638_300)
        prev = store._prev.get(self.IP)
        base_mod._counters_event(self.IP, 284000, prev, [], [],
                                 full_color=0, black_white=284000, uptime=559_640_000)
        self.assertIn("PRINT_OVERFLOW", self._types())
        self.assertNotIn("PRINT", self._types())
        self.assertNotIn("PRINT_GAP", self._types())
        # poll دوم با همان مقدار → تأیید → فقط PRINT_GAP (در آمار روزانه جمع نمی‌شود)
        base_mod._counters_event(self.IP, 284000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=284000, uptime=559_650_000)
        self.assertIn("PRINT_GAP", self._types())
        self.assertNotIn("PRINT", self._types())
        self.assertEqual(store._prev.get(self.IP)["print_total"], 284000)
        gap = next(e for e in recorded_events if e["type"] == "PRINT_GAP")
        self.assertEqual(gap["details"]["pages"], 124000)

    # ۴. چاپ عادی همچنان کار می‌کند
    def test_normal_print_logged(self):
        self._seed_prev(print_total=160000, uptime=559_638_300)
        base_mod._counters_event(self.IP, 160005, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=160005, uptime=559_644_000)
        self.assertIn("PRINT", self._types())
        prnt = next(e for e in recorded_events if e["type"] == "PRINT")
        self.assertEqual(prnt["details"]["pages"], 5)

    # ۵. ریست واقعی فقط با شواهد قوی + حفظ yield/override
    def test_real_reset_requires_strong_evidence_and_preserves_yield(self):
        self._seed_prev(print_total=160000, uptime=559_638_300,
                        yield_per_page=4100, manual_override=0)
        # total به مقدار خیلی کوچک افتاده + uptime کم شده → ریست واقعی
        base_mod._counters_event(self.IP, 300, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=300, uptime=1000)
        self.assertIn("COUNTER_RESET", self._types())
        after = store._prev.get(self.IP)
        self.assertEqual(after["print_total"], 300)
        self.assertEqual(after["yield_per_page"], 4100)  # yield نابود نشد!

    # ۶. کاهش شمارنده با ریبوت ولی بدون شواهد قوی (شمارنده هنوز بزرگ) → anomaly نه reset
    def test_mild_drop_with_reboot_is_anomaly(self):
        self._seed_prev(print_total=160000, uptime=559_638_300)
        base_mod._counters_event(self.IP, 120000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=120000, uptime=1000)
        self.assertNotIn("COUNTER_RESET", self._types())
        self.assertIn("COUNTER_ANOMALY", self._types())
        self.assertEqual(store._prev.get(self.IP)["print_total"], 160000)

    # ۷. یادگیری yield وقتی override دستی فعال است انجام نشود (ضد دایره‌ای)
    def test_no_circular_yield_learning_with_override(self):
        self._seed_prev(print_total=10000, uptime=500_000_000,
                        toner_level=100, yield_per_page=2000,
                        manual_override=1, override_start_total=9000,
                        override_start_toner=100, override_color="black")
        base_mod._counters_event(self.IP, 10100, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=10100,
                                 current_toner_level=90, uptime=500_006_000)
        # یادگیری نباید yield را با سطح ساختگی override به‌روز کند
        self.assertEqual(store._prev.get(self.IP)["yield_per_page"], 2000)


if __name__ == "__main__":
    unittest.main()
