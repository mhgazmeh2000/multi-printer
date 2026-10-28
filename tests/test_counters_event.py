# =============================================================================
# @file:         tests/test_counters_event.py
# @brief:        Tests for the counters event recorded by the poller.
# @details:      Responsibilities: Check that counter events are written with the right values.
#                Main API: CountersEventTests
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.database
# @notes:        Can be run directly as a script.
# =============================================================================

"""Tests for the counter events that the poller records, checking that they carry the right values."""

import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod

# We collect events in memory instead of database
recorded_events = []


def _fake_add_event(ip, etype, details):
    """Helper: fake add event."""
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class CountersEventTests(unittest.TestCase):
    """Fix log counter bug tests (based on real production logs.db evidence)."""

    IP = "10.9.9.9"

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        # PrevStore reset (in-memory cache must also be cleared)
        store._prev._cache.clear()
        recorded_events.clear()
        # Reset MarkeStore
        with store.printers_lock:
            store.PRINTERS[:] = []
        self._orig_add_event = base_mod.add_event
        base_mod.add_event = _fake_add_event

    def tearDown(self):
        """Release the resources that each test in this class created."""
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _seed_prev(self, **data):
        """Helper: seed prev."""
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
        """Helper: types."""
        return [e["type"] for e in recorded_events]

    # 1. Actual evidence: uptime=0 (read failed) should not generate COUNTER_RESET
    def test_zero_uptime_is_not_reboot(self):
        """Verifies zero uptime is not reboot."""
        self._seed_prev(print_total=160000, uptime=559_638_300, yield_per_page=4100)
        base_mod._counters_event(self.IP, 158000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=158000, uptime=0)
        self.assertNotIn("COUNTER_RESET", self._types())
        self.assertIn("COUNTER_ANOMALY", self._types())
        # The snapshot must be saved
        self.assertEqual(store._prev.get(self.IP)["print_total"], 160000)

    # 2. Actual production scenario: total temporarily zero/unreadable + uptime=0
    # Previously: COUNTER_RESET + erase yield=4100 and override → toner failure
    def test_glitch_zero_total_and_zero_uptime_keeps_snapshot_and_yield(self):
        """Verifies glitch zero total and zero uptime keeps snapshot and yield."""
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

    # 3. Counter return after glitch should not make fake PRINT → pending → PRINT_GAP
    def test_counter_recovery_produces_gap_not_print(self):
        """Verifies counter recovery produces gap not print."""
        self._seed_prev(print_total=160000, uptime=559_638_300)
        prev = store._prev.get(self.IP)
        base_mod._counters_event(self.IP, 284000, prev, [], [],
                                 full_color=0, black_white=284000, uptime=559_640_000)
        self.assertIn("PRINT_OVERFLOW", self._types())
        self.assertNotIn("PRINT", self._types())
        self.assertNotIn("PRINT_GAP", self._types())
        # Second poll with the same value → Confirm → PRINT_GAP only (does not add up in daily statistics)
        base_mod._counters_event(self.IP, 284000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=284000, uptime=559_650_000)
        self.assertIn("PRINT_GAP", self._types())
        self.assertNotIn("PRINT", self._types())
        self.assertEqual(store._prev.get(self.IP)["print_total"], 284000)
        gap = next(e for e in recorded_events if e["type"] == "PRINT_GAP")
        self.assertEqual(gap["details"]["pages"], 124000)

    # 4. Normal printing still works
    def test_normal_print_logged(self):
        """Verifies normal print logged."""
        self._seed_prev(print_total=160000, uptime=559_638_300)
        base_mod._counters_event(self.IP, 160005, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=160005, uptime=559_644_000)
        self.assertIn("PRINT", self._types())
        prnt = next(e for e in recorded_events if e["type"] == "PRINT")
        self.assertEqual(prnt["details"]["pages"], 5)

    # 5. True reset only with strong evidence + preserve yield/override
    def test_real_reset_requires_strong_evidence_and_preserves_yield(self):
        """Verifies real reset requires strong evidence and preserves yield."""
        self._seed_prev(print_total=160000, uptime=559_638_300,
                        yield_per_page=4100, manual_override=0)
        # total is too small + uptime is reduced → real reset
        base_mod._counters_event(self.IP, 300, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=300, uptime=1000)
        self.assertIn("COUNTER_RESET", self._types())
        after = store._prev.get(self.IP)
        self.assertEqual(after["print_total"], 300)
        self.assertEqual(after["yield_per_page"], 4100)  # yield was not destroyed!

    # 6. Counter decrease with reboot but no strong evidence (counter still big) → anomaly not reset
    def test_mild_drop_with_reboot_is_anomaly(self):
        """Verifies mild drop with reboot is anomaly."""
        self._seed_prev(print_total=160000, uptime=559_638_300)
        base_mod._counters_event(self.IP, 120000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=120000, uptime=1000)
        self.assertNotIn("COUNTER_RESET", self._types())
        self.assertIn("COUNTER_ANOMALY", self._types())
        self.assertEqual(store._prev.get(self.IP)["print_total"], 160000)

    # 7. Don't learn yield when manual override is active (anti-circular)
    def test_no_circular_yield_learning_with_override(self):
        """Verifies no circular yield learning with override."""
        self._seed_prev(print_total=10000, uptime=500_000_000,
                        toner_level=100, yield_per_page=2000,
                        manual_override=1, override_start_total=9000,
                        override_start_toner=100, override_color="black")
        base_mod._counters_event(self.IP, 10100, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=10100,
                                 current_toner_level=90, uptime=500_006_000)
        # Learning should not update the yield with the override dummy level
        self.assertEqual(store._prev.get(self.IP)["yield_per_page"], 2000)


if __name__ == "__main__":
    unittest.main()
