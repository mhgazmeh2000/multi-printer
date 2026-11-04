# =============================================================================
# @file:         tests/test_yield_confidence_gates.py
# @brief:        Tests for the confidence gates (Fix 1) and the reference-scaled 1% gate (Fix 2).
# @details:      Responsibilities: a single closed cycle is never labelled high; a shared profile is written and
#                inherited only with enough samples (4 black, 8 CMY); the coefficient of variation of recent accepted
#                estimates caps confidence; a 1% step needs pages scaled by the reference capacity.
#                Main API: ConfidenceGateTests
#                Side effects: file I/O (temporary database and catalog)
# @dependencies: internal: core.yield_engine, core.database
# @notes:        Reads environment variables: CARTRIDGE_YIELD_CATALOG. Each test runs in its own temporary
#                working directory. The catalog is empty, so only device_capacity_pages sets a reference.
# =============================================================================

"""Tests for Fix 1 (confidence gates) and Fix 2 (reference-scaled 1% quantization gate)."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core import yield_engine as ye
from core.database import db_connection

IP = "10.8.8.8"
COLOR = "black"
MODEL = "Gate Test"
CARTRIDGE = "Gate Toner"


def _ts(minute):
    """Returns an ISO timestamp that increases with minute, so every poll has a distinct time."""
    return (datetime(2026, 4, 1) + timedelta(minutes=minute)).isoformat()


class ConfidenceGateTests(unittest.TestCase):
    """Behaviour tests for the Fix 1 and Fix 2 gates."""

    def setUp(self):
        """Prepare a temporary working directory and an empty catalog."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_catalog_env = os.environ.get("CARTRIDGE_YIELD_CATALOG")
        os.chdir(self.tmp.name)
        catalog_path = os.path.join(self.tmp.name, "catalog.json")
        with open(catalog_path, "w", encoding="utf-8") as f:
            f.write('{"entries": []}')
        os.environ["CARTRIDGE_YIELD_CATALOG"] = catalog_path

    def tearDown(self):
        """Restore the environment and remove the temporary files."""
        if self.old_catalog_env is None:
            os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
        else:
            os.environ["CARTRIDGE_YIELD_CATALOG"] = self.old_catalog_env
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _poll(self, level, total, minute, ip=IP, capacity=None, color=COLOR, model=MODEL):
        """Runs one poll for the given slot and returns the engine metadata."""
        return ye.process_cartridge_snapshot(
            ip=ip, color=color, printer_model=model, cartridge_name=CARTRIDGE,
            level=level, counters={"total": total}, device_type="mono",
            device_capacity_pages=capacity, timestamp=_ts(minute),
        )

    def _last_reject(self, ip=IP):
        """Returns the reject_reason of the most recent snapshot row written for the printer."""
        with db_connection() as conn:
            row = conn.execute(
                "SELECT reject_reason FROM toner_snapshots_v2 WHERE printer_ip = ? ORDER BY id DESC LIMIT 1",
                (ip,),
            ).fetchone()
        return row[0]

    # ── Fix 1: single closed cycle and shared profiles ──────────────────────────────────────────────────
    def test_single_closed_cycle_is_not_high_and_writes_no_profile(self):
        """One closed cycle (1 sample) must not be labelled high and must not write a shared profile."""
        ip = "10.8.8.9"
        self._poll(level=100, total=1000, minute=0, ip=ip)
        with db_connection() as conn:
            state = ye._load_state(conn, ip, COLOR)
            key = state["cartridge_key"]
            state.update({"cycle_start_counter": 1000, "cycle_start_level": 100, "sample_count": 0,
                          "total_weight": 0.0})
            result = ye._maybe_close_cycle(
                conn, state, ip=ip, color=COLOR, printer_model=MODEL, cartridge_name=CARTRIDGE,
                cartridge_key=key, usage_counter=1000 + 2820, timestamp=_ts(5),
                counter_basis="total_for_black", source="cycle_auto_refill", rated_capacity=None,
            )
            self.assertEqual(result["yield_source"], "cycle_learn")
            self.assertEqual(result["sample_count"], 1)
            self.assertNotEqual(result["confidence"], "high")
            state.update(result)
            ye._upsert_profile_if_high(conn, state)
            self.assertIsNone(ye._load_profile(conn, key))

    def test_profile_needs_min_samples_before_it_is_written(self):
        """A high state with 1 sample is not promoted; the same state with 4 black samples is."""
        ip = "10.8.8.10"
        self._poll(level=100, total=1000, minute=0, ip=ip)
        with db_connection() as conn:
            state = ye._load_state(conn, ip, COLOR)
            key = state["cartridge_key"]
            state.update({"confidence": "high", "yield_per_page": 2820, "sample_count": 1, "total_weight": 4.0})
            ye._upsert_profile_if_high(conn, state)
            self.assertIsNone(ye._load_profile(conn, key))
            state.update({"sample_count": 4, "total_weight": 8.0})
            ye._upsert_profile_if_high(conn, state)
            self.assertIsNotNone(ye._load_profile(conn, key))

    def test_inherited_profile_with_one_sample_is_not_used(self):
        """A stored high profile built from 1 sample is not inherited by a new cartridge (other printer)."""
        with db_connection() as conn:
            self._poll(level=100, total=1000, minute=0, ip="10.8.8.11")  # create the schema
            key = ye.build_cartridge_key(MODEL, CARTRIDGE, COLOR)
            conn.execute(
                "INSERT INTO cartridge_yield_profiles (cartridge_key, printer_model, cartridge_name, color, "
                "yield_per_page, confidence, sample_count, total_weight, source_printer_ip, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, MODEL, CARTRIDGE, COLOR, 2820, "high", 1, 4.0, "10.8.8.99", _ts(0)),
            )
            conn.commit()
        meta = self._poll(level=100, total=5000, minute=1, ip="10.8.8.12")
        self.assertNotEqual(meta["yield_source"], "shared_profile")

    # ── Fix 1: variance cap ─────────────────────────────────────────────────────────────────────────────
    def test_sample_cv_helper(self):
        """CV is zero for equal values, None with fewer than 3 values, and positive for spread values."""
        self.assertEqual(ye._sample_cv([1000, 1000, 1000]), 0.0)
        self.assertIsNone(ye._sample_cv([1000, 2000]))
        self.assertGreater(ye._sample_cv([1000, 2000, 500]), 0.40)

    def test_variance_cap_thresholds(self):
        """CV above 0.20 caps at medium, above 0.40 caps at low, and the cap never raises confidence."""
        self.assertEqual(ye._cap_confidence_by_variance("high", [1000, 1000, 1000]), "high")
        self.assertEqual(ye._cap_confidence_by_variance("high", [1000, 1300, 700]), "medium")  # CV 0.30
        self.assertEqual(ye._cap_confidence_by_variance("high", [1000, 2000, 500]), "low")    # CV 0.66
        self.assertEqual(ye._cap_confidence_by_variance("low", [1000, 1000, 1000]), "low")
        self.assertEqual(ye._cap_confidence_by_variance("medium", [1000, 2000]), "medium")    # n < 3

    def test_wildly_varying_samples_are_not_high(self):
        """Four accepted samples that disagree by 4x are labelled low, not high."""
        self._poll(level=100, total=1000, minute=0)
        meta = self._poll(level=98, total=1100, minute=1)    # 2 points, 100 pages -> 5000
        meta = self._poll(level=96, total=1500, minute=2)    # 2 points, 400 pages -> 20000
        meta = self._poll(level=94, total=1600, minute=3)    # 2 points, 100 pages -> 5000
        meta = self._poll(level=92, total=2000, minute=4)    # 2 points, 400 pages -> 20000
        self.assertEqual(meta["sample_count"], 4)
        self.assertEqual(meta["confidence"], "low")
        # The gates before Fix 1 would have labelled the same four samples high:
        self.assertEqual(ye._derive_confidence(4, 8.0, "total_for_black"), "high")

    def test_consistent_samples_stay_high(self):
        """Four consistent accepted samples keep high confidence (no regression for good data)."""
        self._poll(level=100, total=1000, minute=0)
        for i, (lvl, tot) in enumerate([(98, 1200), (96, 1400), (94, 1600), (92, 1800)], start=1):
            meta = self._poll(level=lvl, total=tot, minute=i)  # 2 points, 200 pages -> 10000 each
        self.assertEqual(meta["sample_count"], 4)
        self.assertEqual(meta["confidence"], "high")

    # ── Fix 2: reference-scaled 1% gate ─────────────────────────────────────────────────────────────────
    def test_min_pages_helper(self):
        """The 1% minimum is 300 without a reference, the 150 floor for small cartridges, and 1.5% otherwise."""
        self.assertEqual(ye._quant_min_pages_1pct(None), 300)
        self.assertEqual(ye._quant_min_pages_1pct(2000), 150)
        self.assertEqual(ye._quant_min_pages_1pct(23800), 357)
        self.assertEqual(ye._quant_min_pages_1pct(34700), 521)

    def test_1pct_gate_scales_with_reference(self):
        """With a 23,800 reference a 1% step with 300 pages is rejected; with 400 pages it is accepted."""
        self._poll(level=100, total=1000, minute=0, capacity=23800)
        meta = self._poll(level=99, total=1300, minute=1, capacity=23800)  # 300 < 357
        self.assertEqual(meta["sample_count"], 0)
        self.assertEqual(self._last_reject(), "quantization_1pct_noise")
        meta = self._poll(level=99, total=1400, minute=2, capacity=23800)  # 400 pages from the same anchor
        self.assertEqual(meta["sample_count"], 1)

    def test_1pct_gate_without_reference_uses_300(self):
        """Without a reference a 1% step with 200 pages is rejected and one with 300 pages is accepted."""
        self._poll(level=100, total=1000, minute=0)
        meta = self._poll(level=99, total=1200, minute=1)
        self.assertEqual(meta["sample_count"], 0)
        meta = self._poll(level=99, total=1300, minute=2)  # 300 pages from the same anchor
        self.assertEqual(meta["sample_count"], 1)

    def test_small_reference_keeps_150_floor(self):
        """A 2,000-page reference keeps the 150-page floor, so 150 pages is accepted and 149 is not."""
        self._poll(level=100, total=1000, minute=0, capacity=2000)
        meta = self._poll(level=99, total=1149, minute=1, capacity=2000)
        self.assertEqual(meta["sample_count"], 0)
        meta = self._poll(level=99, total=1150, minute=2, capacity=2000)
        self.assertEqual(meta["sample_count"], 1)


if __name__ == "__main__":
    unittest.main()
