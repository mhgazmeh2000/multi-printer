# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_catalog_part_number_aliases.py
# @brief:        Part-number and model-code aliases in the Toshiba and Brother catalog entries.
# @details:      Responsibilities: a SNMP-style part-number name (for example "T-5070U Black") selects its own
#                catalog entry on its own model; the Toshiba e-STUDIO257 device reporting model code
#                "MFP11219371" selects the 257 entry; the alias is model-gated and colour-gated; the generic
#                "Black Toner" name is unchanged; the 34,400 learning sequence no longer reaches the device
#                in local mode; the current behaviour of the substring rule and of compact spellings is
#                pinned and labelled CURRENT BEHAVIOUR.
#                Main API: CatalogPartNumberAliasTests, ToshibaBug3RegressionTests
#                Side effects: writes a temporary yield database in a temporary working directory
# @dependencies: internal: core.yield_engine
# @notes:        Clears CARTRIDGE_YIELD_CATALOG so the repository catalog is used. The 34,400 sequence runs in
#                local mode only; OEM mode is deferred to the blend-freeze patch.
# =============================================================================

"""Part-number and model-code aliases in the Toshiba and Brother catalog entries."""

import os
import tempfile
import unittest
from unittest import mock

from core.yield_engine import _catalog_lookup, process_cartridge_snapshot

E257 = "e-STUDIO257"
E2505AC = "TOSHIBA e-STUDIO2505AC"
E2050 = "TOSHIBA e-STUDIO2050"
E306 = "TOSHIBA e-STUDIO306"
E2505H = "TOSHIBA e-STUDIO2505H"
E3015AC = "TOSHIBA e-STUDIO3015AC"
E3540C = "TOSHIBA e-STUDIO3540C"
BROTHER = "Brother MFC-8510DN"
HP_M201 = "HP LaserJet Pro M201"

# Model code reported by the device's own web page (TopAccess "Name" field, Copier Model TOSHIBA e-STUDIO257).
DEVICE_MODEL = "MFP11219371"
# Supply name stored for this device in the production snapshot table (reported by the user, not re-checked).
DEVICE_CARTRIDGE = "Black Toner"

ID_257 = "toshiba-estudio-257-black-t5070u"
T5070U_BY_MODE = {"local": 23800, "oem": 36600, "compatible": 27400, "refill": 22000}

# (model, SNMP-style name, colour, expected catalog id, local yield). One row per added alias.
ALIAS_CASES = [
    (E2505H, "T-2507 Black", "black", "toshiba-estudio-2505h-black-t2507", 6500),
    (E2505H, "T-2507U Black", "black", "toshiba-estudio-2505h-black-t2507", 6500),
    (E2050, "T-FC30U-K Black", "black", "toshiba-estudio-2050c-black-tfc30", 25000),
    (E2050, "T-FC30U-C Cyan", "cyan", "toshiba-estudio-2050c-cyan-tfc30", 21800),
    (E2050, "T-FC30U-M Magenta", "magenta", "toshiba-estudio-2050c-magenta-tfc30", 21800),
    (E2050, "T-FC30U-Y Yellow", "yellow", "toshiba-estudio-2050c-yellow-tfc30", 21800),
    (E2505AC, "T-FC505U-K Black", "black", "toshiba-estudio-2505ac-black-tfc505", 25000),
    (E2505AC, "T-FC505U-C Cyan", "cyan", "toshiba-estudio-2505ac-cyan-tfc505", 21800),
    (E2505AC, "T-FC505U-M Magenta", "magenta", "toshiba-estudio-2505ac-magenta-tfc505", 21800),
    (E2505AC, "T-FC505U-Y Yellow", "yellow", "toshiba-estudio-2505ac-yellow-tfc505", 21800),
    (E257, "T-5070U Black", "black", ID_257, 23800),
    (E306, "T-4590 Black", "black", "toshiba-estudio-306-black-t4590", 23800),
    (E3015AC, "T-FC415U-K Black", "black", "toshiba-estudio-3015ac-black-tfc415", 25000),
    (E3015AC, "T-FC415U-C Cyan", "cyan", "toshiba-estudio-3015ac-cyan-tfc415", 21800),
    (E3015AC, "T-FC415U-M Magenta", "magenta", "toshiba-estudio-3015ac-magenta-tfc415", 21800),
    (E3015AC, "T-FC415U-Y Yellow", "yellow", "toshiba-estudio-3015ac-yellow-tfc415", 21800),
    (E3540C, "T-FC25-K Black", "black", "toshiba-estudio-3540c-black-tfc25", 22200),
    (E3540C, "T-FC25-C Cyan", "cyan", "toshiba-estudio-3540c-cyan-tfc25", 19200),
    (E3540C, "T-FC25-M Magenta", "magenta", "toshiba-estudio-3540c-magenta-tfc25", 19200),
    (E3540C, "T-FC25-Y Yellow", "yellow", "toshiba-estudio-3540c-yellow-tfc25", 19200),
    (BROTHER, "TN-750 Black", "black", "brother-mfc-8510-tn750-toner", 5200),
]


def _hit_id(hit):
    return None if hit is None else hit["catalog_id"]


class CatalogPartNumberAliasTests(unittest.TestCase):
    """Part-number aliases: selection, model and colour gating, and pinned current behaviour."""

    def setUp(self):
        """Use the repository catalog and the local yield mode, and clear the factor."""
        self.env = mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": "local"})
        self.env.start()
        os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
        os.environ.pop("CARTRIDGE_YIELD_FACTOR", None)

    def tearDown(self):
        """Restore the environment."""
        self.env.stop()

    # --- 1. part-number name, and the device's real name -------------------------------------------
    def test_toshiba_part_number_only_name_matches_catalog(self):
        """_catalog_lookup('e-STUDIO257', 'T-5070U Black', 'black') returns the T-5070U entry in every mode."""
        for mode, expected_yield in T5070U_BY_MODE.items():
            with self.subTest(mode=mode):
                with mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": mode}):
                    hit = _catalog_lookup(E257, "T-5070U Black", "black")
                self.assertIsNotNone(hit, msg="mode=%s" % mode)
                self.assertEqual(hit["catalog_id"], ID_257)
                self.assertEqual(hit["yield_per_page"], expected_yield)

    def test_toshiba_real_snmp_name_matches_catalog(self):
        """The device's real model code and supply name select the 257 entry (local 23800, OEM 36600)."""
        hit = _catalog_lookup(DEVICE_MODEL, DEVICE_CARTRIDGE, "black")
        self.assertEqual(_hit_id(hit), ID_257)
        self.assertEqual(hit["yield_per_page"], 23800)
        with mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": "oem"}):
            self.assertEqual(_catalog_lookup(DEVICE_MODEL, DEVICE_CARTRIDGE, "black")["yield_per_page"], 36600)

    def test_printer_name_form_selects_257_entry(self):
        """The printer name form 'TOSHIBA T-5070U-K' also selects the T-5070U entry."""
        self.assertEqual(_hit_id(_catalog_lookup("TOSHIBA e-STUDIO257", "TOSHIBA T-5070U-K", "black")), ID_257)

    def test_generic_black_toner_still_selects_257_entry(self):
        """No regression: the generic 'Black Toner' name still selects the same entry on the 257 model names."""
        self.assertEqual(_hit_id(_catalog_lookup(E257, "Black Toner", "black")), ID_257)
        self.assertEqual(_hit_id(_catalog_lookup(DEVICE_MODEL, "Black Toner", "black")), ID_257)

    def test_unknown_toshiba_model_code_is_not_matched(self):
        """A Toshiba model code that is not in the catalog stays unmatched, even with a generic name."""
        self.assertIsNone(_catalog_lookup("MFP99999999", "Black Toner", "black"))

    # --- 2. every added alias selects its own entry -------------------------------------------------
    def test_each_added_alias_selects_its_own_entry(self):
        """Each added part-number alias selects its entry on its own model, colour and local yield."""
        for model, name, color, expected_id, expected_yield in ALIAS_CASES:
            with self.subTest(model=model, name=name, color=color):
                hit = _catalog_lookup(model, name, color)
                self.assertEqual(_hit_id(hit), expected_id)
                self.assertEqual(hit["yield_per_page"], expected_yield)

    # --- 3. gating: model and colour -------------------------------------------------------------------
    def test_alias_is_model_gated(self):
        """'T-5070U Black' does not select the 257 entry on another model, and T-4590 does not select 257."""
        self.assertIsNone(_catalog_lookup(HP_M201, "T-5070U Black", "black"))
        self.assertIsNone(_catalog_lookup(E2505AC, "T-5070U Black", "black"))
        self.assertIsNone(_catalog_lookup(E257, "T-4590 Black", "black"))

    def test_alias_is_colour_gated(self):
        """'T-FC30U-C Cyan' does not select the cyan entry for a black cartridge, and does for cyan."""
        self.assertIsNone(_catalog_lookup(E2050, "T-FC30U-C Cyan", "black"))
        self.assertEqual(_hit_id(_catalog_lookup(E2050, "T-FC30U-C Cyan", "cyan")),
                         "toshiba-estudio-2050c-cyan-tfc30")

    # --- 4. scope: TN-720 is not aliased (decision pending) ----------------------------------------------
    def test_tn720_is_not_aliased_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. 'TN-720' is not an alias: TN-720 compatibility is an open question."""
        self.assertIsNone(_catalog_lookup(BROTHER, "TN-720 Black", "black"))

    # --- 5. known limits of the substring rule (pinned, tracked) ------------------------------------------
    def test_extended_part_number_matches_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. The rule is a substring match for needles longer than three characters, so
        'T-5070UX Black' selects the T-5070U entry. Whether a T-5070UX exists is not verified. Any future change to
        the rule (for example a boundary rule for alphanumeric part numbers) changes this result on purpose.
        """
        self.assertEqual(_hit_id(_catalog_lookup(E257, "T-5070UX Black", "black")), ID_257)

    def test_compact_spelling_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. 'T5070U Black' (no hyphen) does not match, the same gap as the HP compact forms
        pinned in test_catalog_short_needle_matrix. It is tracked with the compact-spelling work.
        """
        self.assertIsNone(_catalog_lookup(E257, "T5070U Black", "black"))


class ToshibaBug3RegressionTests(unittest.TestCase):
    """The 34,400 learning sequence from the Toshiba e-STUDIO257 (local mode only)."""

    def setUp(self):
        """Run in a temporary working directory, in local mode, with the repository catalog."""
        self.old_cwd = os.getcwd()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        self.env = mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": "local"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)

    def test_toshiba_34400_is_not_reachable_after_bug3_fix(self):
        """Eight polls of 1-point drops of 344 pages each. The displayed yield is the catalog reference
        23,800 at every poll, and never 34,400.

        OEM mode is deferred to the blend-freeze patch. Under OEM, 34,400 is within 40% of the 36,600
        reference and is still learned; see the blend-freeze report, section 10.
        """
        ip = "10.99.25.53"
        total = 1000
        levels = [80, 79, 78, 77, 76, 75, 74, 73]
        metas = []
        for i, level in enumerate(levels):
            if i > 0:
                total += 344
            metas.append(process_cartridge_snapshot(
                ip=ip, color="black", printer_model=DEVICE_MODEL, cartridge_name=DEVICE_CARTRIDGE,
                level=level, counters={"total": total}, device_type="mono",
                timestamp="2026-01-%02dT00:00:00" % (i + 1)))
        for i, meta in enumerate(metas):
            with self.subTest(poll=i):
                self.assertNotEqual(meta["yield_per_page"], 34400)
                self.assertEqual(meta["yield_per_page"], 23800)
        self.assertEqual(metas[-1]["yield_source"], "catalog")


if __name__ == "__main__":
    unittest.main()
