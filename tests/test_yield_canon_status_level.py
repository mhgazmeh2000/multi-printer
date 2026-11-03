# =============================================================================
# @file:         tests/test_yield_canon_status_level.py
# @brief:        Tests for B-3: status-coded or estimated toner levels must not feed the yield learner.
# @details:      Responsibilities: a Canon status code (0/5/7 shown as 0/15/70%) has raw_level=None. Such a level
#                is displayed (current_level) but never seeds an anchor and never learns a sample, in the yield
#                engine or in the legacy learner's level selection. Real readings still learn.
#                Main API: CanonStatusLevelTests, LegacyLearningLevelTests
#                Side effects: file I/O (temporary database and catalog)
# @dependencies: internal: core.yield_engine, core.enhanced_collector
# @notes:        Reads environment variables: CARTRIDGE_YIELD_CATALOG. Each test runs in its own temporary
#                working directory. The catalog is empty, so no catalog capacity can affect the results.
# =============================================================================

"""Tests for B-3: display-only toner levels (Canon status codes, override estimates) are never learned from."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from core.enhanced_collector import _legacy_learning_level
from core.yield_engine import (
    DEFAULT_YIELD_PER_PAGE,
    get_yield_status,
    process_printer_yield_snapshot,
)

IP = "10.5.5.5"
MODEL = "Canon MF240 Series"
NAME = "Cartridge 137"


def _ts(minute):
    """Returns an ISO timestamp that increases with minute, so every poll has a distinct time."""
    return (datetime(2026, 5, 1) + timedelta(minutes=minute)).isoformat()


class CanonStatusLevelTests(unittest.TestCase):
    """Tests for display-only levels in the yield engine, through the same path the collector uses."""

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

    def _poll(self, level, raw_level, total, minute):
        """Runs one poll of the black toner, with the same toner dict shape the collector produces."""
        toners = {"black": {"level": level, "raw_level": raw_level, "name": NAME, "status": "ok"}}
        meta = process_printer_yield_snapshot(
            ip=IP, printer_model=MODEL,
            counters={"total": total, "full_color": 0, "black_white": total},
            toners=toners, device_type="mono", timestamp=_ts(minute), source="poll",
        )
        return meta["black"]

    def test_canon_status_coded_level_does_not_learn(self):
        """A status-coded 70% then 15% over 600 pages (raw_level=None) must not produce a sample."""
        self._poll(level=70, raw_level=None, total=1000, minute=0)
        meta = self._poll(level=15, raw_level=None, total=1600, minute=1)
        self.assertEqual(meta["sample_count"], 0)
        self.assertEqual(meta["yield_per_page"], DEFAULT_YIELD_PER_PAGE)

    def test_canon_real_level_still_learns(self):
        """The same 70% then 15% over 600 pages, but as real readings (raw_level set), is accepted and learned."""
        self._poll(level=70, raw_level=70, total=1000, minute=0)
        meta = self._poll(level=15, raw_level=15, total=1600, minute=1)
        self.assertEqual(meta["sample_count"], 1)
        self.assertEqual(meta["yield_source"], "auto_learn")
        self.assertEqual(meta["yield_per_page"], 1091)  # 600 pages over a 55-point drop

    def test_status_coded_display_level_still_shows_in_ui(self):
        """The status-coded level is shown as current_level, while learning is skipped."""
        self._poll(level=70, raw_level=None, total=1000, minute=0)
        self._poll(level=15, raw_level=None, total=1600, minute=1)
        rows = get_yield_status(ip=IP)
        row = [r for r in rows if r["color"] == "black"][0]
        self.assertEqual(row["current_level"], 15)
        self.assertEqual(row["yield_per_page"], DEFAULT_YIELD_PER_PAGE)

    def test_status_coded_poll_keeps_real_anchor(self):
        """A status-coded poll between real readings leaves the real anchor alone, so the next real drop is learned."""
        self._poll(level=100, raw_level=100, total=1000, minute=0)
        meta = self._poll(level=15, raw_level=None, total=1100, minute=1)
        self.assertEqual((meta["anchor_level"], meta["anchor_counter"]), (100, 1000))
        meta = self._poll(level=98, raw_level=98, total=1200, minute=2)  # 2% over 200 pages
        self.assertEqual(meta["sample_count"], 1)
        self.assertEqual(meta["yield_per_page"], 10000)

    def test_status_coded_first_poll_does_not_seed_anchor(self):
        """A status-coded first poll must not become the baseline. The first real reading starts it."""
        first = self._poll(level=70, raw_level=None, total=1000, minute=0)
        self.assertIsNone(first["anchor_level"])
        self._poll(level=100, raw_level=100, total=1100, minute=1)
        meta = self._poll(level=98, raw_level=98, total=1300, minute=2)  # 2% over 200 pages
        self.assertEqual(meta["sample_count"], 1)
        self.assertEqual(meta["yield_per_page"], 10000)


    def test_status_coded_transitions_do_not_record_refill(self):
        """A display-only 0% -> 70% -> 15% -> 70% sequence must not mark a refill or close a cycle."""
        self._poll(level=0, raw_level=None, total=1000, minute=0)
        self._poll(level=70, raw_level=None, total=1100, minute=1)
        self._poll(level=15, raw_level=None, total=1700, minute=2)
        meta = self._poll(level=70, raw_level=None, total=1800, minute=3)
        self.assertEqual(meta["cycle_status"], "active")
        self.assertIsNone(meta["last_refill_at"])


class LegacyLearningLevelTests(unittest.TestCase):
    """Tests for the level the legacy learner and refill detection receive (B-3, enhanced collector)."""

    def test_status_coded_black_is_not_a_learning_level(self):
        """Black with a display level and no reading gives no learning level."""
        self.assertEqual(_legacy_learning_level({"black": {"level": 15, "raw_level": None}}), (None, None))

    def test_manual_override_estimate_is_not_a_learning_level(self):
        """An override changes only `level`, so without a reading it is not used."""
        self.assertEqual(_legacy_learning_level({"black": {"level": 88, "raw_level": None}}), (None, None))

    def test_real_black_reading_is_used(self):
        """A real black reading is the learning level."""
        self.assertEqual(_legacy_learning_level({"black": {"level": 40, "raw_level": 40}}), ("black", 40))

    def test_real_reading_on_another_colour_is_used_when_black_is_status_coded(self):
        """If black has only a display level, a real reading on another colour is used instead."""
        toners = {"black": {"level": 15, "raw_level": None}, "cyan": {"level": 60, "raw_level": 60}}
        self.assertEqual(_legacy_learning_level(toners), ("cyan", 60))


if __name__ == "__main__":
    unittest.main()
