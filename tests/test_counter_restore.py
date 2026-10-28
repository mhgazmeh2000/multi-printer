# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_counter_restore.py
# @brief:        Tests for page-counter recount after reset and reboot (bug #1).
# @details:      Responsibilities: Verify counter restore logic.
#                Main API: CounterRestoreTests
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.database
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Bug tests #1 — Recounting pages after Reset/Reboot.

Reference scenario (real fleet):
    20575 → Reboot → 0 → 21000 ⇒ Actual print = 21000 - 20575 = 425
Previous error: when return value was higher than restore_tol band, reset_ref far
was cast and the delta was calculated from zero (21,000 phantom pages).
"""
import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod

recorded_events = []


def _fake_add_event(ip, etype, details):
    """Helper: fake add event."""
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class CounterRestoreTests(unittest.TestCase):
    """Tests for counter restore."""
    IP = "10.8.8.8"
    UP = 500_000_000  # High uptime (hundreds of seconds)

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        recorded_events.clear()
        self._orig_add_event = base_mod.add_event
        base_mod.add_event = _fake_add_event

    def tearDown(self):
        """Release the resources that each test in this class created."""
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ─── helpers ───
    def _types(self):
        """Helper: types."""
        return [e["type"] for e in recorded_events]

    def _events(self, etype):
        """Helper: events."""
        return [e for e in recorded_events if e["type"] == etype]

    def _seed(self, total):
        """Helper: seed."""
        store._prev.set(self.IP, {
            "print_total": total,
            "full_color": 0,
            "black_white": total,
            "toner_level": 60,
            "alert_codes": [],
            "last_alert_codes": [],
            "uptime": self.UP,
        })
        recorded_events.clear()

    def _poll(self, total, uptime):
        """Helper: poll."""
        prev = store._prev.get(self.IP) or {}
        base_mod._counters_event(
            self.IP, total, prev, [], [],
            full_color=0, black_white=total,
            current_toner_level=60, prev_toner_level=60,
            uptime=uptime,
        )

    def _run_reboot(self, base_total, *restored_totals):
        """seed(base_total) → poll(0 with reboot) → restored_totals polls"""
        self._seed(base_total)
        self._poll(0, 10_000)          # Uptime dropped → real reboot
        for i, t in enumerate(restored_totals):
            self._poll(t, 20_000 + i * 10_000)

    def _total_pages(self):
        """Helper: total pages."""
        return sum(e["details"].get("pages", 0) or 0
                   for e in recorded_events
                   if e["type"] in ("PRINT", "PRINT_GAP"))

    def _no_phantom_pages(self, phantom):
        """Helper: no phantom pages."""
        for e in recorded_events:
            self.assertNotEqual(e["details"].get("pages"), phantom,
                                f"صفحه فانتوم {phantom} در {e['type']} ثبت شد")
            self.assertNotEqual(e["details"].get("delta"), phantom,
                                f"دلتای فانتوم {phantom} در {e['type']} ثبت شد")

    # ─── Main prompt scenario ───
    def test_20575_reboot_21000_counts_425_not_21000(self):
        """Verifies 20575 reboot 21000 counts 425 not 21000."""
        self._run_reboot(20575, 21000, 21000)   # Second poll to verify overflow
        self.assertIn("COUNTER_RESET", self._types())
        restored = self._events("COUNTER_RESTORED")
        self.assertEqual(len(restored), 1, "باید یک COUNTER_RESTORED اطلاعاتی ثبت شود")
        self.assertEqual(restored[0]["details"].get("real_delta"), 425)
        gaps = self._events("PRINT_GAP")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["details"]["pages"], 425)
        self.assertEqual(self._total_pages(), 425)
        self._no_phantom_pages(21000)
        self.assertEqual(store._prev.get(self.IP)["print_total"], 21000)

    # ─── table of mandatory scenarios ───
    def test_10000_reboot_10050_in_band_no_pages(self):
        """Verifies 10000 reboot 10050 in band no pages."""
        self._run_reboot(10000, 10050)
        self.assertIn("COUNTER_RESET", self._types())
        self.assertEqual(len(self._events("COUNTER_RESTORED")), 1)
        self.assertEqual(self._total_pages(), 0, "در باند بازگشت، چاپ ثبت نمی‌شود")
        self.assertEqual(store._prev.get(self.IP)["print_total"], 10050)
        self.assertIsNone(store._prev.get(self.IP).get("reset_prev_total"))

    def test_10000_reboot_12000_counts_2000_not_12000(self):
        """Verifies 10000 reboot 12000 counts 2000 not 12000."""
        self._run_reboot(10000, 12000, 12000)
        restored = self._events("COUNTER_RESTORED")
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["details"].get("real_delta"), 2000)
        self.assertEqual(self._total_pages(), 2000)
        self._no_phantom_pages(12000)
        self.assertEqual(store._prev.get(self.IP)["print_total"], 12000)

    def test_10000_reboot_exact_10000_no_pages(self):
        """Verifies 10000 reboot exact 10000 no pages."""
        self._run_reboot(10000, 10000)
        self.assertEqual(len(self._events("COUNTER_RESTORED")), 1)
        self.assertEqual(self._total_pages(), 0)
        self.assertEqual(store._prev.get(self.IP)["print_total"], 10000)

    def test_10000_reboot_500_real_reset_growth(self):
        """Actual reset of the counter (increasing from zero): exits from the restore logic."""
        self._run_reboot(10000, 500, 500)
        self.assertEqual(len(self._events("COUNTER_RESTORED")), 0,
                         "مقدار پایین باند نباید بازگشت NVRAM تلقی شود")
        self.assertEqual(self._total_pages(), 500)
        self._no_phantom_pages(10000)

    # ─── Regression: chain doesn't double count after modification ───
    def test_next_poll_after_restore_counts_only_new_delta(self):
        """Verifies next poll after restore counts only new delta."""
        self._run_reboot(20575, 21000, 21000)
        recorded_events.clear()
        self._poll(21050, 60_000)      # 50 pages of real new printing
        prints = self._events("PRINT")
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0]["details"]["pages"], 50)
        self._no_phantom_pages(21000)
        self.assertEqual(store._prev.get(self.IP)["print_total"], 21050)

    # ─── Regression: gradual entry into band = existing RESTORED meaning (not changed) ───
    def test_gradual_growth_into_band_is_restored_then_counts_normally(self):
        """The gradual growth into the return band has the same behavior as RESTORED;
        The next poll no longer has a flag and the delta is calculated from the new normal baseline."""
        self._seed(10000)
        self._poll(0, 10_000)       # Real reset
        self._poll(500, 20_000)     # Gradual growth after true reset
        self._poll(9995, 30_000)    # Enter the band → RESTORED (existing behavior)
        self.assertEqual(len(self._events("COUNTER_RESTORED")), 1)
        self.assertIsNone(store._prev.get(self.IP).get("reset_prev_total"))
        self.assertEqual(store._prev.get(self.IP)["print_total"], 9995)
        recorded_events.clear()
        self._poll(10055, 40_000)   # 60 pages of real printing next
        prints = self._events("PRINT")
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0]["details"]["pages"], 60)
        self._no_phantom_pages(10000)


if __name__ == "__main__":
    unittest.main()
