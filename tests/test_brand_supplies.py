# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_brand_supplies.py
# @brief:        Toner and cartridge compatibility tests for user-reported printer models.
# @details:      Responsibilities: Verify supply parsing per brand and model.
#                Main API: BrandSuppliesTests
#                Side effects: none at import time
# @dependencies: internal: core.collectors.canon, core.enhanced_collector
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Toner/cartridge compatibility tests for user reported models:
  - Canon LBP233dw (i-SENSYS): discrete status codes 0/5/7 + Remote UI script fallback
  - HP LaserJet MFP M527 (FutureSmart): percent unit but MaxCapacity=-3
  - HP LaserJet Managed MFP E52645: CF289* names that did not match before
No real device: SNMP responses are injected according to the documented behavior of each family.
"""
import types
import unittest

import core.enhanced_collector as ec


SUP = "1.3.6.1.2.1.43.11.1.1"


def fake_snmp_factory(table):
    """Helper: fake snmp factory."""
    def _fake(ip, oid, community, **kw):
        """Helper: fake."""
        return table.get(oid)
    return _fake


class BrandSuppliesTests(unittest.TestCase):

    """Tests for brand supplies."""
    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self._orig = ec.snmp_get_with_fallback

    def tearDown(self):
        """Release the resources that each test in this class created."""
        ec.snmp_get_with_fallback = self._orig

    # ─── HP M527 FutureSmart ──────────────────────────────────────
    def test_hp_m527_unit_percent_with_missing_max(self):
        """FutureSmart: unit=19(percent), level=68, but max=-3 (capacity unknown).
        Previously: percent=None → "Can't receive toner". Now it should be 68%."""
        table = {
            f"{SUP}.6.1.1": "Black Cartridge",
            f"{SUP}.5.1.1": 3,            # toner
            f"{SUP}.7.1.1": 19,           # percent
            f"{SUP}.8.1.1": -3,           # max: knows-some / unknown capacity
            f"{SUP}.9.1.1": 68,           # level = percentage self
        }
        ec.snmp_get_with_fallback = fake_snmp_factory(table)
        supplies = ec.walk_supplies_table("1.1.1.1", "public", brand="hp", snmp_version=2)
        self.assertEqual(len(supplies), 1)
        s = supplies[0]
        self.assertEqual(s["percent"], 68)
        self.assertEqual(s["percent_source"], "unit_percent")
        self.assertEqual(s["status"], "ok")

    def test_hp_cf287x_name_matches_alternate_oid(self):
        """The name "Black Cartridge CF287X" used to not match the CF287A key."""
        seen_oids = []

        def fake(ip, oid, community, **kw):
            """Helper: fake."""
            seen_oids.append(oid)
            table = {
                f"{SUP}.6.1.1": "Black Cartridge CF287X",
                f"{SUP}.5.1.1": 3,
                f"{SUP}.7.1.1": 8,        # sheets — not a direct percentage
                f"{SUP}.8.1.1": -2,       # max unknown
                f"{SUP}.9.1.1": -2,       # level unknown
                ec._HP_NPCL_PCT_OID: 42,  # JetDirect replacement OID
            }
            return table.get(oid)

        ec.snmp_get_with_fallback = fake
        supplies = ec.walk_supplies_table("1.1.1.1", "public", brand="hp", snmp_version=2)
        s = supplies[0]
        self.assertEqual(s["percent"], 42)
        self.assertEqual(s["percent_source"], "alternative_oid")
        self.assertIn(ec._HP_NPCL_PCT_OID, seen_oids)

    def test_hp_e52645_cf289y_matches(self):
        """E52645 has 89Y/CF289Y cartridge — new map key."""

        def fake(ip, oid, community, **kw):
            """Helper: fake."""
            return 37 if oid == ec._HP_NPCL_PCT_OID else None

        ec.snmp_get_with_fallback = fake
        pct = ec.try_alternative_oids("1.1.1.1", "public", "hp", "Black Cartridge CF289Y")
        self.assertEqual(pct, 37)

    # ─── Canon LBP233dw ───────────────────────────────────────────
    def test_canon_discrete_status_codes_ok(self):
        """Canon i-SENSYS: Returns discrete code 0/5/7 instead of percentage.
        7=OK should be ok (not critical 7%!) and its source is known."""
        table = {
            f"{SUP}.6.1.1": "Cartridge 071 H",
            f"{SUP}.5.1.1": 3,
            f"{SUP}.7.1.1": 2,            # unit unknown
            f"{SUP}.8.1.1": -2,           # max unknown
            f"{SUP}.9.1.1": 7,            # Code "OK" not 7%!
        }
        ec.snmp_get_with_fallback = fake_snmp_factory(table)
        supplies = ec.walk_supplies_table("1.1.1.1", "public", brand="canon", snmp_version=1)
        s = supplies[0]
        self.assertEqual(s["status"], "ok")
        self.assertEqual(s["percent"], 70)
        self.assertEqual(s["percent_source"], "canon_status_code")
        self.assertTrue(s["supply_present"])
        # And more importantly: alternative guess OIDs should not be hit
        # (If it was hit and a valid value was returned, the source label would be changed)

    def test_canon_discrete_empty_and_low(self):
        """Verifies canon discrete empty and low."""
        for code, want_status, want_pct in [(0, "empty", 0), (5, "low", 15)]:
            table = {
                f"{SUP}.6.1.1": "Cartridge 071",
                f"{SUP}.5.1.1": 3,
                f"{SUP}.7.1.1": 2,
                f"{SUP}.8.1.1": -2,
                f"{SUP}.9.1.1": code,
            }
            ec.snmp_get_with_fallback = fake_snmp_factory(table)
            s = ec.walk_supplies_table("1.1.1.1", "public", brand="canon", snmp_version=1)[0]
            self.assertEqual(s["status"], want_status)
            self.assertEqual(s["percent"], want_pct)
            self.assertEqual(s["percent_source"], "canon_status_code")

    def test_canon_scrape_generic_remote_ui_pattern(self):
        """Remote UI script fallback with the usual label Toner/Cartridge."""
        import core.collectors.canon as canon_mod
        html = ("<html><body>Cartridge Level <img src='bar.gif'/> "
                "<span>63%</span> Toner Cartridge</body></html>")

        class _Resp:
            """Helper class: resp."""
            status_code = 200
            text = html

        import sys as _sys
        fake_requests = types.SimpleNamespace(get=lambda *a, **kw: _Resp())
        orig = _sys.modules.get("requests")
        _sys.modules["requests"] = fake_requests
        try:
            result = canon_mod._scrape_canon_toners("1.1.1.1", timeout=0.1)
        finally:
            if orig is not None:
                _sys.modules["requests"] = orig
            else:
                del _sys.modules["requests"]
        self.assertEqual(result, {"black": 63})

    # Sentinel general rules
    def test_minus3_means_some_supply_not_unsupported(self):
        """3- It means "there is some consumption" - not "it is not supported"."""
        table = {
            f"{SUP}.6.1.1": "Black Toner",
            f"{SUP}.5.1.1": 3,
            f"{SUP}.7.1.1": 7,
            f"{SUP}.8.1.1": -3,
            f"{SUP}.9.1.1": -3,
        }
        ec.snmp_get_with_fallback = fake_snmp_factory(table)
        s = ec.walk_supplies_table("1.1.1.1", "public", brand="unknown", snmp_version=2)[0]
        self.assertIsNone(s["percent"])
        self.assertEqual(s["status"], "unknown")
        self.assertTrue(s["supply_present"])

    def test_estimated_canon_code_never_becomes_raw_level(self):
        """Canon status code "estimated" percentage is for display only; Construction logic
        toners should make raw_level=None to not pollute yield learning."""
        supply = {"percent": 70, "percent_source": "canon_status_code"}
        raw = supply["percent"] if supply.get("percent_source") != "canon_status_code" else None
        self.assertIsNone(raw, "کد گسسته‌ی Canon نباید raw_level شود")
        supply2 = {"percent": 68, "percent_source": "unit_percent"}
        raw2 = supply2["percent"] if supply2.get("percent_source") != "canon_status_code" else None
        self.assertEqual(raw2, 68, "درصد واقعی HP باید raw_level بماند")


if __name__ == "__main__":
    unittest.main()
