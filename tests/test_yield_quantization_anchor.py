# =============================================================================
# @file:         tests/test_yield_quantization_anchor.py
# @brief:        Tests for the 1% quantization gate and its effect on the anchor (B-2).
# @details:      Responsibilities: a quantization reject (1% drop with fewer than QUANT_MIN_PAGES_1PCT pages)
#                must keep the anchor so that later polls accumulate pages against it. A real drop of 2% or
#                more still moves the anchor and is learned. The noise gate and the refill gate are unchanged.
#                Main API: QuantizationAnchorTests
#                Side effects: file I/O (temporary database and catalog)
# @dependencies: internal: core.yield_engine, core.database
# @notes:        Reads environment variables: CARTRIDGE_YIELD_CATALOG. Each test runs in its own temporary
#                working directory. The catalog is empty, so no catalog capacity can affect the results.
# =============================================================================

"""Tests for the 1% quantization gate: anchor handling, sample learning, and the noise and refill gates."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core.database import db_connection
from core.yield_engine import (
    DEFAULT_YIELD_PER_PAGE,
    REFILL_JUMP_PERCENT,
    process_cartridge_snapshot,
)

IP = "10.7.7.7"
COLOR = "black"
MODEL = "Quant Test"
CARTRIDGE = "Quant Toner"


def _ts(minute):
    """Returns an ISO timestamp that increases with minute, so every poll has a distinct time."""
    return (datetime(2026, 3, 1) + timedelta(minutes=minute)).isoformat()


class QuantizationAnchorTests(unittest.TestCase):
    """Tests for the anchor behaviour of the quantization gate."""

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

    def _poll(self, level, total, minute):
        """Runs one poll for the black slot of the test printer; minute orders the timestamps."""
        return process_cartridge_snapshot(
            ip=IP, color=COLOR, printer_model=MODEL, cartridge_name=CARTRIDGE,
            level=level, counters={"total": total}, device_type="mono",
            timestamp=_ts(minute),
        )

    def _last_snapshot(self):
        """Returns (reject_reason, valid) of the most recent snapshot row written for the test printer."""
        with db_connection() as conn:
            row = conn.execute(
                "SELECT reject_reason, valid FROM toner_snapshots_v2 WHERE printer_ip = ? ORDER BY id DESC LIMIT 1",
                (IP,),
            ).fetchone()
        return (row[0], bool(row[1]))

    def test_frequent_1pct_polls_accumulate_and_learn(self):
        """Sixty polls of a 1% drop per 100 pages must learn a yield once the drop reaches 2%."""
        meta = self._poll(level=100, total=1000, minute=0)
        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 1000)

        for i in range(1, 61):
            level, total = 100 - i, 1000 + 100 * i
            prev_sample_count = meta["sample_count"]
            prev_anchor = (meta["anchor_level"], meta["anchor_counter"])
            meta = self._poll(level=level, total=total, minute=i)
            if meta["sample_count"] == prev_sample_count:
                # Rejected poll (quantization or not_enough_pages_yet): the anchor must not move.
                self.assertEqual((meta["anchor_level"], meta["anchor_counter"]), prev_anchor,
                                 f"anchor moved on rejected poll {i}")
            else:
                # Accepted poll: the anchor moves to the current reading.
                self.assertEqual((meta["anchor_level"], meta["anchor_counter"]), (level, total),
                                 f"anchor not moved on accepted poll {i}")

        self.assertGreater(meta["sample_count"], 0)
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertNotEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)
        # Every accepted sample is 200 pages over a 2% drop = 10000 pages per 100%.
        self.assertEqual(meta["yield_per_page"], 10000)

    def test_quantization_reject_does_not_move_anchor(self):
        """A single 1% drop with fewer than QUANT_MIN_PAGES_1PCT pages is rejected and keeps the anchor."""
        self._poll(level=100, total=1000, minute=0)
        meta = self._poll(level=99, total=1100, minute=1)  # 1% drop, 100 pages (< 150)

        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 1000)
        self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)
        self.assertEqual(meta["sample_count"], 0)
        self.assertEqual(self._last_snapshot(), ("quantization_1pct_noise", False))

    def test_2pct_drop_still_moves_anchor_and_learns(self):
        """A 2% drop with 200 pages is accepted, learned, and moves the anchor."""
        self._poll(level=100, total=1000, minute=0)
        meta = self._poll(level=98, total=1200, minute=1)  # 2% drop, 200 pages

        self.assertEqual(meta["sample_count"], 1)
        self.assertEqual(meta["anchor_level"], 98)
        self.assertEqual(meta["anchor_counter"], 1200)
        self.assertEqual(meta["yield_per_page"], 10000)
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertEqual(self._last_snapshot(), (None, True))

    def test_noise_gate_still_works(self):
        """A severe drop with almost no pages is read noise: rejected, and the anchor is not moved."""
        self._poll(level=100, total=1000, minute=0)
        meta = self._poll(level=60, total=1002, minute=1)  # 40-point drop, 2 pages (< LEVEL_NOISE_MAX_PAGES)

        self.assertEqual(meta["anchor_level"], 100)
        self.assertEqual(meta["anchor_counter"], 1000)
        self.assertEqual(meta["sample_count"], 0)
        self.assertEqual(self._last_snapshot(), ("level_read_noise", False))

    def test_refill_gate_still_works(self):
        """A jump of at least REFILL_JUMP_PERCENT becomes a refill candidate and is confirmed on the next jump."""
        base = 40
        candidate = base + REFILL_JUMP_PERCENT
        confirmed = candidate + REFILL_JUMP_PERCENT

        self._poll(level=base, total=1000, minute=0)
        meta = self._poll(level=candidate, total=1010, minute=1)
        self.assertEqual(meta["cycle_status"], "refill_pending")
        self.assertIsNone(meta["last_refill_at"])

        meta = self._poll(level=confirmed, total=1020, minute=2)
        self.assertEqual(meta["last_refill_at"], _ts(2))
        self.assertEqual(meta["cycle_status"], "active")
        self.assertEqual(meta["anchor_level"], confirmed)
        self.assertEqual(meta["anchor_counter"], 1020)


if __name__ == "__main__":
    unittest.main()
