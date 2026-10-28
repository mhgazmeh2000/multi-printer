# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_paper_size.py
# @brief:        Tests for the wrong paper-size logging bug fix.
# @details:      Responsibilities: Verify paper-size attribution for Toshiba and generic paths.
#                Main API: AttributePaperSizeUnitTests, LegacyToshibaPaperSizeTests
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.collectors.toshiba, core.database
# @notes:        Can be run directly as a script.
# =============================================================================

"""
"Wrong/incorrect paper size in logs" bug fix tests.

Scenarios covered based on code root review:
1) Labeling is only valid when the delta of the paper counters is the same as the delta of the counter
   The total should be aligned (Toshiba's proven identity: a3_total + a4_total == print_total).
2) baseline poisoned with 0 (legacy path in failed SNMP read) should not cause delta
   Get giant and label wrong — Reproduce production bug: "Large/Mixed for pure A4 printing".
3) A failed read should not overwrite PrevStore with 0.
4) In the Mixed mode, the detailed separation of large/small should be recorded in the event details (paper_split).
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
    """Helper: fake add event."""
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class AttributePaperSizeUnitTests(unittest.TestCase):
    """Direct test of attribute_paper_size function (no SNMP/database)."""

    def test_aligned_small_only(self):
        """Verifies aligned small only."""
        size, split, reliable = attribute_paper_size(
            total=10005, prev_total=10000,
            a3_total=2000, prev_a3=2000,
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_SMALL_LABEL)
        self.assertEqual(split, {"large": 0, "small": 5})
        self.assertTrue(reliable)

    def test_aligned_large_only(self):
        """Verifies aligned large only."""
        size, split, reliable = attribute_paper_size(
            total=10003, prev_total=10000,
            a3_total=2003, prev_a3=2000,
            a4_total=8000, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_LARGE_LABEL)
        self.assertEqual(split, {"large": 3, "small": 0})
        self.assertTrue(reliable)

    def test_aligned_mixed_with_exact_split(self):
        """Verifies aligned mixed with exact split."""
        size, split, reliable = attribute_paper_size(
            total=10007, prev_total=10000,
            a3_total=2002, prev_a3=2000,
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_MIXED_LABEL)
        self.assertEqual(split, {"large": 2, "small": 5})
        self.assertTrue(reliable)

    def test_stale_snapshot_is_rejected_not_mislabeled(self):
        # Production scenario: baseline of branch a3 is stale (previous failed read),
        # Delta A3 covers two ranges; Sum of deltas ≠ total delta.
        # Previously, the "Large" (or Mixed) label was incorrectly registered; Now it should be None.
        """Verifies stale snapshot is rejected not mislabeled."""
        size, split, reliable = attribute_paper_size(
            total=10005, prev_total=10000,          # Really just 5 small pages
            a3_total=2010, prev_a3=2000,            # Fake delta 10 (two intervals)
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertFalse(reliable)

    def test_zero_poisoned_baseline_is_rejected(self):
        # baseline poisoned with 0: delta = total counter. It used to be recorded as "Mixed".
        """Verifies zero poisoned baseline is rejected."""
        size, split, reliable = attribute_paper_size(
            total=10005, prev_total=10000,
            a3_total=2000, prev_a3=0,
            a4_total=8005, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertFalse(reliable)

    def test_negative_delta_is_rejected(self):
        """Verifies negative delta is rejected."""
        size, split, reliable = attribute_paper_size(
            total=10002, prev_total=10000,
            a3_total=1500, prev_a3=2000,            # The counter has gone back!
            a4_total=8502, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertFalse(reliable)

    def test_missing_data_returns_unknown(self):
        """Verifies missing data returns unknown."""
        for args in [
            dict(total=10005, prev_total=10000, a3_total=None, prev_a3=2000, a4_total=8005, prev_a4=8000),
            dict(total=10005, prev_total=10000, a3_total=2000, prev_a3=None, a4_total=8005, prev_a4=8000),
        ]:
            size, split, reliable = attribute_paper_size(ip="1.1.1.1", **args)
            self.assertIsNone(size)
            self.assertFalse(reliable)

    def test_no_change_is_silently_reliable(self):
        """Verifies no change is silently reliable."""
        size, split, reliable = attribute_paper_size(
            total=10000, prev_total=10000,
            a3_total=2000, prev_a3=2000,
            a4_total=8000, prev_a4=8000, ip="1.1.1.1")
        self.assertIsNone(size)
        self.assertTrue(reliable)

    def test_first_poll_without_total_baseline_still_labels(self):
        # No total counter baseline (but with paper baseline from DB), label based
        # Net paper deltas are allowed—but separations are still accurately reported.
        """Verifies first poll without total baseline still labels."""
        size, split, reliable = attribute_paper_size(
            total=None, prev_total=None,
            a3_total=2004, prev_a3=2000,
            a4_total=8000, prev_a4=8000, ip="1.1.1.1")
        self.assertEqual(size, PAPER_SIZE_LARGE_LABEL)
        self.assertEqual(split, {"large": 4, "small": 0})


class LegacyToshibaPaperSizeTests(unittest.TestCase):
    """Reproduction of the production bug in the path of legacy collector (fallback)."""

    IP = "10.8.8.8"

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
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
            """Helper: fake bulk."""
            return dict(self.raw)

        toshiba_mod.snmp_get_bulk = _fake_bulk

    def tearDown(self):
        """Release the resources that each test in this class created."""
        base_mod.add_event = self._orig_add_event
        toshiba_mod.snmp_get_bulk = self._orig_bulk
        toshiba_mod._scrape_toshiba_toners = self._orig_scrape
        toshiba_mod._walk_toner_remaining = self._orig_walk
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _base_raw(self, total, uptime=900_000_000, **extra):
        """Helper: base raw."""
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
        # Healthy baseline: 10,000 = 2,000 large + 8,000 small
        """Verifies failed read does not poison prev then labels correctly."""
        store._prev.set(self.IP, {
            "print_total": 10000, "uptime": 800_000_000,
            "a3_total": 2000, "a4_total": 8000,
        })
        recorded_events.clear()

        # poll 1: reading paper counters fails (keys are missing)
        self.raw = self._base_raw(total=10000, uptime=800_006_000)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())

        prev_after_fail = store._prev.get(self.IP)
        # ✅ Old bug: here a3_total/a4_total were overwritten with 0
        self.assertEqual(prev_after_fail.get("a3_total"), 2000)
        self.assertEqual(prev_after_fail.get("a4_total"), 8000)

        # poll 2: 5 printed pure A4 pages; Paper counters are read again
        self.raw = self._base_raw(total=10005, uptime=800_012_000,
                                  a3_total=2000, a4_total=8005)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())

        prints = [e for e in recorded_events if e["type"] == "PRINT"]
        self.assertTrue(prints, "رویداد PRINT باید ثبت شود")
        # ✅ Old bug: paper_size == "Mixed" (due to fake delta of 2000) was registered
        self.assertEqual(prints[-1]["details"].get("pages"), 5)
        self.assertEqual(prints[-1]["details"].get("paper_size"), PAPER_SIZE_SMALL_LABEL)
        self.assertEqual(prints[-1]["details"].get("paper_split"), {"large": 0, "small": 5})

    def test_historically_poisoned_baseline_does_not_mislabel(self):
        # baseline already poisoned with 0 (historical DB data); It should not be labeled wrong
        """Verifies historically poisoned baseline does not mislabel."""
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
        # ✅ Delta a3+a4 = 10,005 ≠ 5 → invalid ratio → unspecified size (not mixed!)
        self.assertEqual(prints[-1]["details"].get("pages"), 5)
        self.assertIsNone(prints[-1]["details"].get("paper_size"))

        # Next poll: baseline modified, tag becomes valid again
        self.raw = self._base_raw(total=10007, uptime=800_012_000,
                                  a3_total=2000, a4_total=8007)
        toshiba_mod.collect_toshiba(self.IP, "t1", "public", __import__("time").time())
        prints = [e for e in recorded_events if e["type"] == "PRINT"]
        self.assertEqual(prints[-1]["details"].get("paper_size"), PAPER_SIZE_SMALL_LABEL)

    def test_mixed_print_records_exact_split_in_details(self):
        """Verifies mixed print records exact split in details."""
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
        # ✅ Detailed breakdown for log audit
        self.assertEqual(d.get("paper_split"), {"large": 2, "small": 5})


if __name__ == "__main__":
    unittest.main()
