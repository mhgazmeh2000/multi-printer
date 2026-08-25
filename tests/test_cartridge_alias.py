# -*- coding: utf-8 -*-
"""
تست‌های نگاشت نام کانونیکال کارتریج کانن.

باگ واقعی: کاربر «Cartridge 737» نصب کرده بود ولی داشبورد «Cartridge 137»
نمایش می‌داد، چون firmware دستگاه (منطقه‌ی آمریکا) نام معادل منطقه‌ای را از
SNMP برمی‌گرداند. CRG-137/337/737/937 یک کارتریج فیزیکی یکسان‌اند؛ کاتالوگ
پروژه و بازار خرید، 737 را مرجع می‌دانند.
"""
import os
import re
import unittest

import core.enhanced_collector as ec


class CanonRegionalAliasTests(unittest.TestCase):
    def test_regional_aliases_map_to_canonical_737(self):
        self.assertEqual(ec._canonical_cartridge_display_name("Cartridge 137"), "Cartridge 737")
        self.assertEqual(ec._canonical_cartridge_display_name("cartridge 337"), "Cartridge 737")
        self.assertEqual(ec._canonical_cartridge_display_name("CARTRIDGE 937"), "Cartridge 737")
        # فضای خالی/حروف بزرگ تحمل می‌شود
        self.assertEqual(ec._canonical_cartridge_display_name("  Cartridge   137 "), "Cartridge 737")

    def test_non_alias_names_pass_through_untouched(self):
        self.assertEqual(ec._canonical_cartridge_display_name("Cartridge 737"), "Cartridge 737")
        self.assertEqual(ec._canonical_cartridge_display_name("CF287A"), "CF287A")
        self.assertEqual(ec._canonical_cartridge_display_name("DR-730"), "DR-730")

    def test_empty_and_none_safe(self):
        self.assertEqual(ec._canonical_cartridge_display_name(""), "")
        self.assertIsNone(ec._canonical_cartridge_display_name(None))

    def test_raw_name_heuristics_still_fire(self):
        """heuristicهایی که به نام خام وابسته‌اند نباید بشکنند."""
        self.assertEqual(ec._canon_display_percent("Canon MF237w", "Cartridge 137", 15), 20)
        self.assertEqual(ec._canon_display_percent("Canon MF237w", "Cartridge 137", 60), 60)
        self.assertIsNone(ec._canon_display_percent("Canon MF237w", "Cartridge 137", None))

    def test_toners_assembly_uses_canonical_name_for_canon_only(self):
        """نقطه‌ی مونتاژ toners: نگاشت فقط برای برند canon و raw_name حفظ شود."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "core", "enhanced_collector.py"),
                   encoding="utf-8").read()
        self.assertRegex(src,
            r"_canonical_cartridge_display_name\(raw_supply_name\)\s+if brand == \"canon\"")
        self.assertIn('"raw_name": raw_supply_name if display_supply_name != raw_supply_name else None', src)


if __name__ == "__main__":
    unittest.main()
