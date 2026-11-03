# =============================================================================
# @file:         tests/test_catalog_matching.py
# @brief:        Tests for cartridge catalog matching in the yield engine.
# @details:      Responsibilities: word-boundary matching for short digit needles, the
#                model+cartridge bonus, the priority tie-break, and the generic-vs-specific
#                catalog outcome for HP M225dn / CF283A.
#                Main API: CatalogMatchingTests
#                Side effects: file I/O (temporary catalog files)
# @dependencies: internal: core.yield_engine
# @notes:        Reads environment variables: CARTRIDGE_YIELD_CATALOG, CARTRIDGE_YIELD_MODE,
#                CARTRIDGE_YIELD_FACTOR. Each test restores the environment it changes.
# =============================================================================

"""Tests for cartridge catalog matching: word boundaries, bonus, priority and tie-break."""

import json
import os
import tempfile
import unittest
from unittest import mock

from core.yield_engine import _catalog_entry_yield, _catalog_lookup, _load_yield_catalog


def _entry(entry_id, yield_per_page, match, **extra):
    """Builds one catalog entry for a temporary catalog file."""
    data = {"id": entry_id, "yield_per_page": yield_per_page, "match": match}
    data.update(extra)
    return data


class CatalogMatchingTests(unittest.TestCase):
    """Tests for catalog matching in the yield engine."""

    def setUp(self):
        """Prepare a clean environment and a temporary directory for catalog files."""
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": "local"})
        self.env.start()
        os.environ.pop("CARTRIDGE_YIELD_FACTOR", None)

    def tearDown(self):
        """Restore the environment and remove the temporary files."""
        self.env.stop()
        self.tmp.cleanup()

    def _use_catalog(self, entries):
        """Writes the given entries to a temporary catalog and points the engine at it."""
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f)
        os.environ["CARTRIDGE_YIELD_CATALOG"] = path

    def _lookup(self, model, cartridge, color="black"):
        """Returns the catalog match for the given inputs, or None."""
        return _catalog_lookup(model, cartridge, color)

    # --- word-boundary matching -------------------------------------------------

    def test_short_digit_needle_does_not_match_inside_longer_part_number(self):
        """A short needle such as 87a must not match inside CF387A."""
        self._use_catalog([_entry("hp-cf287a-87a", 9000, {"cartridge_contains": ["87a"]})])
        self.assertIsNone(self._lookup("HP LaserJet Pro M201", "CF387A"))

    def test_short_digit_needle_matches_as_a_whole_token(self):
        """A short needle still matches when it is a standalone token such as 'HP 87A'."""
        self._use_catalog([_entry("hp-cf287a-87a", 9000, {"cartridge_contains": ["87a"]})])
        result = self._lookup("HP LaserJet Pro M201", "HP 87A")
        self.assertIsNotNone(result)
        self.assertEqual(result["catalog_id"], "hp-cf287a-87a")
        self.assertEqual(result["yield_per_page"], 9000)

    def test_long_part_number_needle_still_matches_as_substring(self):
        """Needles longer than three characters keep plain substring matching."""
        self._use_catalog([_entry("hp-cf287a", 9000, {"cartridge_contains": ["cf287a"]})])
        result = self._lookup("HP LaserJet Pro M201", "HP CF287A OEM")
        self.assertIsNotNone(result)
        self.assertEqual(result["catalog_id"], "hp-cf287a")

    # --- model + cartridge bonus -----------------------------------------------

    def test_model_and_cartridge_rule_outranks_cartridge_only_rule(self):
        """An entry with both model and cartridge needles beats a cartridge-only entry, in either order."""
        specific = _entry("specific", 1000, {"model_contains": ["m225dn"], "cartridge_contains": ["cf283a"]})
        generic = _entry("generic", 2000, {"color": "black", "cartridge_contains": ["cf283a"]})
        for entries in ([specific, generic], [generic, specific]):
            self._use_catalog(entries)
            result = self._lookup("HP LaserJet Pro M225dn", "CF283A")
            self.assertEqual(result["catalog_id"], "specific", msg=f"order={[e['id'] for e in entries]}")

    # --- priority and deterministic tie-break ----------------------------------

    def test_priority_breaks_equal_score_tie_in_either_order(self):
        """With equal scores, the entry with the higher priority wins regardless of file order."""
        low = _entry("low-priority", 1000, {"cartridge_contains": ["toner x"]}, priority=0)
        high = _entry("high-priority", 2000, {"cartridge_contains": ["toner x"]}, priority=5)
        for entries in ([low, high], [high, low]):
            self._use_catalog(entries)
            result = self._lookup("Any Printer", "Toner X")
            self.assertEqual(result["catalog_id"], "high-priority", msg=f"order={[e['id'] for e in entries]}")
            self.assertEqual(result["yield_per_page"], 2000)

    def test_equal_score_without_priority_keeps_first_entry_and_logs_debug(self):
        """With no priority on either entry, the first entry in the file wins and a DEBUG line records the tie."""
        first = _entry("first", 1000, {"cartridge_contains": ["toner x"]})
        second = _entry("second", 2000, {"cartridge_contains": ["toner x"]})
        self._use_catalog([first, second])
        with self.assertLogs("PrinterMonitor", level="DEBUG") as logs:
            result = self._lookup("Any Printer", "Toner X")
        self.assertEqual(result["catalog_id"], "first")
        self.assertTrue(any("catalog tie" in line for line in logs.output))

    # --- generic vs specific: same yield, different catalog_id -----------------

    def test_generic_vs_specific_same_yield_only_catalog_id_differs(self):
        """HP M225dn with CF283A: the generic cartridge entry wins today (score 7 vs 5).

        The model-specific entry has no cartridge_contains, so it gets no bonus. Both entries
        carry the same local yield, so only catalog_id differs. This test documents the current
        outcome; it does not change the scoring.
        """
        os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)  # use the repository catalog
        catalog = {e["id"]: e for e in _load_yield_catalog() if isinstance(e, dict)}
        generic = catalog["hp-cf283a-83a-cartridge"]
        specific = catalog["hp-laserjet-pro-m225dn-cf283a"]

        result = self._lookup("HP LaserJet Pro MFP M225dn", "CF283A", "black")

        self.assertEqual(result["catalog_id"], "hp-cf283a-83a-cartridge")
        self.assertEqual(result["yield_per_page"], _catalog_entry_yield(generic))
        self.assertEqual(_catalog_entry_yield(specific), _catalog_entry_yield(generic))
        self.assertEqual(result["yield_per_page"], 1000)


if __name__ == "__main__":
    unittest.main()
