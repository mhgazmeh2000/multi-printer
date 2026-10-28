# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_toshiba_fallback_sync.py
# @brief:        Tests for the A3/A4 counter baseline consistency on the Toshiba fallback path
#                (bug #2).
# @details:      Responsibilities: Verify that baselines match the print total.
#                Main API: ToshibaFallbackSyncTests
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.collectors.toshiba, core.database
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Bug tests #2 — A3/A4 counters baseline inconsistency with print_total
In the Toshiba legacy/fallback path (core/collectors/toshiba.py).

Rule: only when the engine accepts the snapshot event (print_total is advanced)
No other baseline — including a3_total/a4_total — is advanced; exactly like
printer_total/copy_total/fax_total/list_total.

SNMP is completely mocked in these tests; Only collector logic is executed.
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
    """Helper: fake add event."""
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class ToshibaFallbackSyncTests(unittest.TestCase):
    """Tests for toshiba fallback sync."""
    IP = "10.7.7.7"
    UP = 500_000_000

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
        self._p_bulk = mock.patch.object(toshiba_mod, "snmp_get_bulk")
        self._p_scrape = mock.patch.object(toshiba_mod, "_scrape_toshiba_toners", return_value=None)
        self._p_walk = mock.patch.object(toshiba_mod, "_walk_toner_remaining", return_value=None)
        self.m_bulk = self._p_bulk.start()
        self._p_scrape.start()
        self._p_walk.start()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self._p_bulk.stop()
        self._p_scrape.stop()
        self._p_walk.stop()
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ─── helpers ───
    def _raw(self, total, a3, a4, uptime):
        """Helper: raw."""
        return {
            "print_total": total, "uptime": uptime,
            "print_fc": 0, "print_bw": total,
            "a3_total": a3, "a4_total": a4,
            "model": "e-STUDIO3015AC", "serial": "XYZ", "firmware": "T3",
        }

    def _seed_prev(self, total, a3, a4):
        """Helper: seed prev."""
        store._prev.set(self.IP, {
            "print_total": total, "full_color": 0, "black_white": total,
            "a3_total": a3, "a4_total": a4,
            "a3_lagged": False, "a4_lagged": False,
            "toner_level": 60, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP,
        })
        recorded_events.clear()

    def _collect(self, total, a3, a4, uptime):
        """Helper: collect."""
        self.m_bulk.return_value = self._raw(total, a3, a4, uptime)
        return toshiba_mod.collect_toshiba(self.IP, "Toshiba-Test", "public", time.time())

    def _prev(self):
        """Helper: prev."""
        return store._prev.get(self.IP) or {}

    def _events(self, etype):
        """Helper: events."""
        return [e for e in recorded_events if e["type"] == etype]

    # ─── Scenario 1 — Normal Poll: All baselines advance ───
    def test_normal_poll_advances_all_baselines(self):
        """Verifies normal poll advances all baselines."""
        self._seed_prev(1000, 300, 700)
        self._collect(1005, 302, 703, self.UP + 6_000)
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1005)
        self.assertEqual(prev["a3_total"], 302)
        self.assertEqual(prev["a4_total"], 703)

    # ─── Scenario 2 — anomaly: no baseline (even A3/A4) moves forward ───
    def test_anomaly_poll_does_not_advance_a3_a4(self):
        """Verifies anomaly poll does not advance a 3 a 4."""
        self._seed_prev(1000, 300, 700)
        # Counter drop without reboot (stable uptime) → COUNTER_ANOMALY → reject snapshot
        self._collect(200, 350, 700, self.UP + 6_000)
        self.assertTrue(self._events("COUNTER_ANOMALY"))
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1000, "snapshot anomaly نباید جلو برود")
        self.assertEqual(prev["a3_total"], 300, "baseline A3 بیرون از پذیرش جلو رفته بود")
        self.assertEqual(prev["a4_total"], 700, "baseline A4 بیرون از پذیرش جلو رفته بود")
        self.assertFalse(prev.get("a3_lagged"))
        self.assertFalse(prev.get("a4_lagged"))

    # ─── Scenario 3 — Next healthy poll: A3/A4 deltas aligned with print_total ───
    def test_next_healthy_poll_deltas_aligned(self):
        """Verifies next healthy poll deltas aligned."""
        self._seed_prev(1000, 300, 700)
        self._collect(200, 350, 700, self.UP + 6_000)      # anomaly
        recorded_events.clear()
        self._collect(1015, 303, 712, self.UP + 12_000)    # Healthy: +15 = 3 large + 12 small
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1015)
        self.assertEqual(prev["a3_total"], 303)
        self.assertEqual(prev["a4_total"], 712)
        prints = self._events("PRINT")
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0]["details"]["pages"], 15)
        split = prints[0]["details"].get("paper_split") or {}
        # Delta A3 should be calculated from the correct baseline (300→303) and not from the anomaly value (350).
        self.assertEqual(split.get("large"), 3)
        self.assertEqual(split.get("small"), 12)
        self.assertEqual(split.get("large", 0) + split.get("small", 0), 15)

    # ─── Supplement: overflow pending also freezes A3/A4 ───
    def test_overflow_pending_does_not_advance_a3_a4(self):
        """Verifies overflow pending does not advance a 3 a 4."""
        self._seed_prev(1000, 300, 700)
        # Unusual jump (too logical) → PRINT_OVERFLOW pending → pending snapshot
        self._collect(5000, 3200, 1800, self.UP + 6_000)
        self.assertTrue(self._events("PRINT_OVERFLOW"))
        prev = self._prev()
        self.assertEqual(prev["print_total"], 1000)
        self.assertEqual(prev["a3_total"], 300)
        self.assertEqual(prev["a4_total"], 700)


if __name__ == "__main__":
    unittest.main()
