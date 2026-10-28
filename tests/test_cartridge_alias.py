# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_cartridge_alias.py
# @brief:        Tests for canonical cartridge-name mapping.
# @details:      Responsibilities: Check that regional cartridge names map to one canonical name.
#                Main API: CanonRegionalAliasTests
#                Side effects: file I/O
# @dependencies: internal: core.enhanced_collector
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Canonical cartridge name mapping tests.

Actual bug: User had installed "Cartridge 737" but dashboard "Cartridge 137"
It would display, because the device firmware (US region) the equivalent name of the region from
Returns SNMP. CRG-137/337/737/937 are physically the same cartridge; catalog
The project and the purchase market consider the 737 as a reference.
"""
import os
import unittest

import core.enhanced_collector as ec


class CanonRegionalAliasTests(unittest.TestCase):
    """Tests for canon regional alias."""
    def test_regional_aliases_map_to_canonical_737(self):
        """Verifies regional aliases map to canonical 737."""
        self.assertEqual(ec._canonical_cartridge_display_name("Cartridge 137"), "Cartridge 737")
        self.assertEqual(ec._canonical_cartridge_display_name("cartridge 337"), "Cartridge 737")
        self.assertEqual(ec._canonical_cartridge_display_name("CARTRIDGE 937"), "Cartridge 737")
        # Spaces/uppercase letters are tolerated
        self.assertEqual(ec._canonical_cartridge_display_name("  Cartridge   137 "), "Cartridge 737")

    def test_non_alias_names_pass_through_untouched(self):
        """Verifies non alias names pass through untouched."""
        self.assertEqual(ec._canonical_cartridge_display_name("Cartridge 737"), "Cartridge 737")
        self.assertEqual(ec._canonical_cartridge_display_name("CF287A"), "CF287A")
        self.assertEqual(ec._canonical_cartridge_display_name("DR-730"), "DR-730")

    def test_empty_and_none_safe(self):
        """Verifies empty and none safe."""
        self.assertEqual(ec._canonical_cartridge_display_name(""), "")
        self.assertIsNone(ec._canonical_cartridge_display_name(None))

    def test_raw_name_heuristics_still_fire(self):
        """Heuristics that depend on the raw name should not break."""
        self.assertEqual(ec._canon_display_percent("Canon MF237w", "Cartridge 137", 15), 20)
        self.assertEqual(ec._canon_display_percent("Canon MF237w", "Cartridge 137", 60), 60)
        self.assertIsNone(ec._canon_display_percent("Canon MF237w", "Cartridge 137", None))

    def test_toners_assembly_uses_canonical_name_for_canon_only(self):
        """Toners assembly point: keep mapping only for canon brand and raw_name."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "core", "enhanced_collector.py"),
                   encoding="utf-8").read()
        self.assertRegex(src,
            r"_canonical_cartridge_display_name\(raw_supply_name\)\s+if brand == \"canon\"")
        self.assertIn('"raw_name": raw_supply_name if display_supply_name != raw_supply_name else None', src)


if __name__ == "__main__":
    unittest.main()
