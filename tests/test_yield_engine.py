# =============================================================================
# @file:         tests/test_yield_engine.py
# @brief:        Tests for the yield engine.
# @details:      Responsibilities: Check yield calculations and cartridge snapshots.
#                Main API: YieldEngineTests
#                Side effects: file I/O
# @dependencies: internal: core.yield_engine
# @notes:        Reads environment variables: CARTRIDGE_YIELD_CATALOG. Can be run directly as a
#                script.
# =============================================================================

"""Tests for the yield engine: yield calculations and cartridge snapshots."""

import os
import tempfile
import unittest

from core.yield_engine import (
    DEFAULT_YIELD_PER_PAGE,
    get_yield_status,
    process_cartridge_snapshot,
    register_manual_refill,
)


class YieldEngineTests(unittest.TestCase):
    """Tests for yield engine."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_catalog_env = os.environ.get("CARTRIDGE_YIELD_CATALOG")
        os.chdir(self.tmp.name)
        self.catalog_path = os.path.join(self.tmp.name, "catalog.json")
        with open(self.catalog_path, "w", encoding="utf-8") as f:
            f.write('{"entries":[{"id":"test-catalog-toner","color":"black","yield_per_page":4100,"match":{"color":"black","cartridge_contains":["catalog toner x"]}}]}')
        os.environ["CARTRIDGE_YIELD_CATALOG"] = self.catalog_path

    def tearDown(self):
        """Release the resources that each test in this class created."""
        if self.old_catalog_env is None:
            os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
        else:
            os.environ["CARTRIDGE_YIELD_CATALOG"] = self.old_catalog_env
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_low_usage_anchor_accumulates_until_toner_drops(self):
        # Anchor at 80%, then many low-page polls with no toner change.
        # ✅ Commit 17: Quantization rule (≥150 pages per 1% drop) — So
        # The scenario is run at real scale (≥200 pages/pulse); Smaller inputs
        # Deliberately rejected as delusional samples (yield=320/394 in real Debug)
        # not be made
        """Verifies low usage anchor accumulates until toner drops."""
        process_cartridge_snapshot(
            ip="10.0.0.1", color="black", printer_model="HP X", cartridge_name="CF283A",
            level=80, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        for total in (1200, 1400, 1600, 1800):
            meta = process_cartridge_snapshot(
                ip="10.0.0.1", color="black", printer_model="HP X", cartridge_name="CF283A",
                level=80, counters={"total": total}, device_type="mono", timestamp="2026-01-02T00:00:00",
            )
            self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)

        # When level finally drops, pages are calculated from the original anchor, not last poll.
        meta = process_cartridge_snapshot(
            ip="10.0.0.1", color="black", printer_model="HP X", cartridge_name="CF283A",
            level=79, counters={"total": 1950}, device_type="mono", timestamp="2026-01-03T00:00:00",
        )
        # 950 pages from anchor with 1% drop → 95000 (in the allowed range of 300..100000)
        self.assertEqual(meta["yield_per_page"], 95000)
        self.assertEqual(meta["yield_source"], "auto_learn")

    def test_high_confidence_profile_is_shared_by_model_and_cartridge_name(self):
        """Verifies high confidence profile is shared by model and cartridge name."""
        ip1 = "10.0.0.10"
        model = "Canon MF Test"
        cartridge = "Cartridge 137"
        # Existing second printer with the same model/cartridge starts as default.
        process_cartridge_snapshot(
            ip="10.0.0.11", color="black", printer_model=model, cartridge_name=cartridge,
            level=90, counters={"total": 500}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )

        total = 1000
        level = 90
        process_cartridge_snapshot(
            ip=ip1, color="black", printer_model=model, cartridge_name=cartridge,
            level=level, counters={"total": total}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        for i in range(4):
            total += 400   # ✅ Commit 17: Each sample must have ≥150 pages/1%
            level -= 1
            meta = process_cartridge_snapshot(
                ip=ip1, color="black", printer_model=model, cartridge_name=cartridge,
                level=level, counters={"total": total}, device_type="mono", timestamp=f"2026-01-0{i+2}T00:00:00",
            )

        self.assertEqual(meta["confidence"], "high")
        self.assertEqual(meta["yield_per_page"], 40000)

        rows = get_yield_status()
        second = [r for r in rows if r["printer_ip"] == "10.0.0.11" and r["color"] == "black"][0]
        self.assertEqual(second["yield_per_page"], 40000)
        self.assertEqual(second["yield_source"], "shared_profile")
        self.assertEqual(second["confidence"], "high")

    def test_manual_refill_resets_anchor_without_losing_existing_profile(self):
        """Verifies manual refill resets anchor without losing existing profile."""
        meta = process_cartridge_snapshot(
            ip="10.0.0.20", color="black", printer_model="Brother Test", cartridge_name="TN Test",
            level=60, counters={"total": 2000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        self.assertEqual(meta["anchor_level"], 60)

        meta = register_manual_refill(
            ip="10.0.0.20", color="black", printer_model="Brother Test", cartridge_name="TN Test",
            new_level=100, counters={"total": 2100}, device_type="mono", timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 2100)
        rows = get_yield_status(ip="10.0.0.20")
        self.assertEqual(rows[0]["last_refill_at"], "2026-01-02T00:00:00")

    def test_catalog_capacity_replaces_default_when_no_better_source_exists(self):
        """Verifies catalog capacity replaces default when no better source exists."""
        meta = process_cartridge_snapshot(
            ip="10.0.0.24", color="black", printer_model="Any Model", cartridge_name="Catalog Toner X",
            level=80, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], 4100)
        self.assertEqual(meta["yield_source"], "catalog")
        self.assertEqual(meta["confidence"], "medium")

    def test_device_reported_capacity_replaces_default_until_auto_learn(self):
        # ✅ Commit 17: with large device_capacity (copies), ≥150p/1% samples are possible
        # are learning; A sample with low confidence does not override the baseline, but with
        # The second sample (medium confidence) is replaced by auto_learn.
        """Verifies device reported capacity replaces default until auto learn."""
        process_cartridge_snapshot(
            ip="10.0.0.25", color="black", printer_model="HP Capacity", cartridge_name="Device Toner",
            level=75, counters={"total": 1000}, device_type="mono", device_capacity_pages=32000,
            timestamp="2026-01-01T00:00:00",
        )
        # A sample with low confidence should not override a better baseline such as device_capacity.
        meta = process_cartridge_snapshot(
            ip="10.0.0.25", color="black", printer_model="HP Capacity", cartridge_name="Device Toner",
            level=74, counters={"total": 1300}, device_type="mono", device_capacity_pages=32000,
            timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], 32000)
        self.assertEqual(meta["yield_source"], "device_capacity")

        # With sufficient sample and moderate confidence, auto_learn can be replaced.
        meta = process_cartridge_snapshot(
            ip="10.0.0.25", color="black", printer_model="HP Capacity", cartridge_name="Device Toner",
            level=73, counters={"total": 1600}, device_type="mono", device_capacity_pages=32000,
            timestamp="2026-01-03T00:00:00",
        )
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertEqual(meta["confidence"], "medium")
        self.assertEqual(meta["yield_per_page"], 30000)

    def test_device_capacity_protected_from_weak_samples(self):
        # ✅ New (commit 17): small cartridge (device_capacity=3200) by default never
        # The sample does not give ≥150p/1%; All are rejected as "1% quantization noise".
        # And the capacity of the spraying machine is not. This same bug yield=1901 poisoned zinc
        # It was e-STUDIO306 (actual Debug log).
        """Verifies device capacity protected from weak samples."""
        process_cartridge_snapshot(
            ip="10.0.0.26", color="black", printer_model="HP Small", cartridge_name="Small Toner",
            level=75, counters={"total": 1000}, device_type="mono", device_capacity_pages=3200,
            timestamp="2026-01-01T00:00:00",
        )
        for i, total in enumerate((1050, 1100, 1150)):
            meta = process_cartridge_snapshot(
                ip="10.0.0.26", color="black", printer_model="HP Small", cartridge_name="Small Toner",
                level=74 - i, counters={"total": total}, device_type="mono",
                device_capacity_pages=3200, timestamp=f"2026-01-0{i+2}T00:00:00",
            )
        self.assertEqual(meta["yield_per_page"], 3200)
        self.assertEqual(meta["yield_source"], "device_capacity")

    def test_counter_decrease_resets_anchor_and_does_not_learn_bad_sample(self):
        """Verifies counter decrease resets anchor and does not learn bad sample."""
        process_cartridge_snapshot(
            ip="10.0.0.30", color="black", printer_model="HP Reset", cartridge_name="Toner",
            level=70, counters={"total": 5000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.30", color="black", printer_model="HP Reset", cartridge_name="Toner",
            level=69, counters={"total": 100}, device_type="mono", timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)
        self.assertEqual(meta["anchor_counter"], 100)
        self.assertEqual(meta["anchor_level"], 69)

    def test_zero_plateau_can_reach_high_after_next_refill(self):
        # Start cycle with 100% reset/charge
        """Verifies zero plateau can reach high after next refill."""
        register_manual_refill(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            new_level=100, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            level=0, counters={"total": 5200}, device_type="mono", timestamp="2026-01-10T00:00:00",
        )
        # Toner runs out but printing continues; The system should track pages_after_zero.
        self.assertEqual(meta["cycle_status"], "zero_plateau")
        meta = process_cartridge_snapshot(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            level=0, counters={"total": 6200}, device_type="mono", timestamp="2026-01-15T00:00:00",
        )
        self.assertEqual(meta["pages_after_zero"], 1000)
        # The next charge closes the previous cycle and makes the actual yield high.
        meta = register_manual_refill(
            ip="10.0.0.35", color="black", printer_model="Zero Test", cartridge_name="ZT",
            new_level=100, counters={"total": 6800}, device_type="mono", timestamp="2026-01-20T00:00:00",
        )
        self.assertEqual(meta["yield_source"], "cycle_learn")
        self.assertEqual(meta["confidence"], "high")
        self.assertEqual(meta["yield_per_page"], 5800)

    def test_cmy_with_full_color_counter_is_conservative_before_high_confidence(self):
        # ✅ Commit 17: The scale of samples increased (≥150p/1%) and engine before high
        # behaves conservatively (lower conf for color channels).
        """Verifies cmy with full color counter is conservative before high confidence."""
        process_cartridge_snapshot(
            ip="10.0.0.40", color="cyan", printer_model="Color Test", cartridge_name="C Toner",
            level=80, counters={"total": 9000, "full_color": 1000}, device_type="color", timestamp="2026-01-01T00:00:00",
        )
        total = 1000
        level = 80
        for i in range(4):
            total += 400
            level -= 1
            meta = process_cartridge_snapshot(
                ip="10.0.0.40", color="cyan", printer_model="Color Test", cartridge_name="C Toner",
                level=level, counters={"total": 9000 + total, "full_color": total}, device_type="color", timestamp=f"2026-01-0{i+2}T00:00:00",
            )
        self.assertEqual(meta["yield_per_page"], 40000)
        self.assertEqual(meta["confidence"], "medium")

    def test_toner_blip_does_not_move_anchor(self):
        # ✅ NEW (commit 16/17, from real e-STUDIO306 log): instant drop 100→8% with
        # 3 print pages are "level reading noise"; No anchor is moved, no sample is created
        # will be discriminating value: if blip is rejected → next sample from anchor
        # Old is calculated (30300), if not rejected → 30000.
        """Verifies toner blip does not move anchor."""
        process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=100, counters={"total": 1000}, device_type="mono", timestamp="2026-01-01T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=8, counters={"total": 1003}, device_type="mono", timestamp="2026-01-02T00:00:00",
        )
        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 1000)

        process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=100, counters={"total": 1003}, device_type="mono", timestamp="2026-01-03T00:00:00",
        )
        meta = process_cartridge_snapshot(
            ip="10.0.0.60", color="black", printer_model="Toshiba Blip", cartridge_name="T-Blip",
            level=99, counters={"total": 1303}, device_type="mono", timestamp="2026-01-04T00:00:00",
        )
        self.assertEqual(meta["yield_per_page"], 30300)
        self.assertEqual(meta["yield_source"], "auto_learn")


if __name__ == "__main__":
    unittest.main()
