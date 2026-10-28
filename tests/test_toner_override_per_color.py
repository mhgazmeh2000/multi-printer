# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_toner_override_per_color.py
# @brief:        Tests for per-colour manual toner overrides.
# @details:      Responsibilities: Check override helpers, reset logic and persistence.
#                Main API: OverrideHelpersTests, PagesSinceResetTests, ApplyOverrideTests,
#                OverridePersistenceTests
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.database
# @notes:        Can be run directly as a script.
# =============================================================================

"""Per-color toner manual override tests.

The main bug: manual reset of three consecutive colors (black → cyan → yellow), only the last color
kept After a poll, the previous two colors revert to the raw SNMP value.
This file guarantees:
  1) Each color has an independent slot and several resets in a row are all saved.
  2) apply_toner_override gives an override value for "all" reset colors.
  3) get_pages_since_last_reset returns the most recent (minimum) reset.
  4) Going back the counter (service/reset) only invalidates the slot of the same device.
  5) Database saving/loading (toner_overrides) has correct round trip.
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
    """Tests for override helpers."""
    def test_normalize_legacy_single_slot(self):
        """Verifies normalize legacy single slot."""
        prev = {
            "manual_override": 1,
            "override_color": "black",
            "override_start_total": 5000,
            "override_start_toner": 90,
        }
        ov = _normalize_overrides(prev)
        self.assertEqual(ov, {"black": {"start_total": 5000, "start_toner": 90}})

    def test_normalize_keeps_per_color_slots(self):
        """Verifies normalize keeps per color slots."""
        prev = {"toner_overrides": {
            "black": {"start_total": 5000, "start_toner": 90},
            "cyan": {"start_total": 5000, "start_toner": 100},
        }}
        self.assertEqual(_normalize_overrides(prev), prev["toner_overrides"])

    def test_normalize_empty(self):
        """Verifies normalize empty."""
        self.assertEqual(_normalize_overrides({}), {})
        self.assertEqual(_normalize_overrides({"manual_override": 0}), {})

    def test_expiry_only_invalidates_its_slot(self):
        """Verifies expiry only invalidates its slot."""
        ov = {
            "black": {"start_total": 5000, "start_toner": 90},
            "cyan": {"start_total": 4000, "start_toner": 100},
        }
        # Counter is back to 4500: only black (starting at 5000) becomes invalid
        out = _overrides_valid_after_total(ov, 4500)
        self.assertNotIn("black", out)
        self.assertIn("cyan", out)

    def test_mirror_legacy_keys_with_and_without_slots(self):
        """Verifies mirror legacy keys with and without slots."""
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
    """Tests for pages since reset."""
    def test_single_slot(self):
        """Verifies single slot."""
        prev = {"toner_overrides": {"black": {"start_total": 5000, "start_toner": 90}}}
        self.assertEqual(get_pages_since_last_reset(prev, 5100), 100)

    def test_multi_slot_returns_min(self):
        """Verifies multi slot returns min."""
        prev = {"toner_overrides": {
            "black": {"start_total": 4000, "start_toner": 90},
            "cyan": {"start_total": 5050, "start_toner": 100},
        }}
        # black: 1200 pages, cyan: 150 pages → the most recent reset is the winner
        self.assertEqual(get_pages_since_last_reset(prev, 5200), 150)

    def test_negative_slot_ignored(self):
        """Verifies negative slot ignored."""
        prev = {"toner_overrides": {
            "black": {"start_total": 6000, "start_toner": 90},  # gone back
            "cyan": {"start_total": 5000, "start_toner": 100},
        }}
        self.assertEqual(get_pages_since_last_reset(prev, 5200), 200)

    def test_no_override_returns_none(self):
        """Verifies no override returns none."""
        self.assertIsNone(get_pages_since_last_reset({}, 100))
        self.assertIsNone(get_pages_since_last_reset({"toner_overrides": {}}, 100))


class ApplyOverrideTests(unittest.TestCase):
    """Tests for apply override."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self._patcher = mock.patch("core.collectors.base.store._prev")
        self.fake_prev = self._patcher.start()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        self._patcher.stop()

    def test_all_colors_keep_override(self):
        """The real scenario of the bug: three resets in a row → all three must remain override."""
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {
                "black": {"start_total": 100000, "start_toner": 90},
                "cyan": {"start_total": 100000, "start_toner": 100},
                "yellow": {"start_total": 100000, "start_toner": 90},
            },
        }
        # 50 printed pages: 2% drop (round(50*100/2000))
        self.assertEqual(apply_toner_override("1.1.1.1", 100050, 12, color="black"), 88)
        self.assertEqual(apply_toner_override("1.1.1.1", 100050, 12, color="cyan"), 98)
        self.assertEqual(apply_toner_override("1.1.1.1", 100050, 12, color="yellow"), 88)

    def test_color_without_override_returns_none(self):
        """Verifies color without override returns none."""
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {"black": {"start_total": 100000, "start_toner": 90}},
        }
        # magenta never reset → raw SNMP remains intact
        self.assertIsNone(apply_toner_override("1.1.1.1", 100050, 55, color="magenta"))

    def test_counter_regression_invalidates_slot(self):
        """Verifies counter regression invalidates slot."""
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {"black": {"start_total": 100000, "start_toner": 90}},
        }
        # The counter is returned to before the start of the override (service) → raw value
        self.assertEqual(apply_toner_override("1.1.1.1", 99000, 55, color="black"), 55)

    def test_zero_pages_returns_start_toner(self):
        """Verifies zero pages returns start toner."""
        self.fake_prev.get.return_value = {
            "yield_per_page": 2000,
            "toner_overrides": {"black": {"start_total": 100000, "start_toner": 90}},
        }
        self.assertEqual(apply_toner_override("1.1.1.1", 100000, 55, color="black"), 90)

    def test_legacy_single_slot_still_works(self):
        """Migration: old state (single slot) without toner_overrides also works."""
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
    """Tests for override persistence."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.getcwd()
        os.chdir(self._tmp.name)
        from core.database import init_db
        init_db()

    def tearDown(self):
        """Release the resources that each test in this class created."""
        os.chdir(self._old)
        self._tmp.cleanup()

    def test_toner_overrides_roundtrip(self):
        """Verifies toner overrides roundtrip."""
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
        """The next set without toner_overrides should not clear slots."""
        from core import store as store_mod
        ps = store_mod.PrevStore()
        try:
            ps.set("10.0.0.8", {"print_total": 100, "toner_overrides": {
                "black": {"start_total": 50, "start_toner": 90}}})
            ps.set("10.0.0.8", {"print_total": 150})  # No toner_overrides
            got = ps.get("10.0.0.8")
            self.assertEqual(got["toner_overrides"], {
                "black": {"start_total": 50, "start_toner": 90}})
            self.assertEqual(got["print_total"], 150)
        finally:
            ps.delete("10.0.0.8")


if __name__ == "__main__":
    unittest.main()
