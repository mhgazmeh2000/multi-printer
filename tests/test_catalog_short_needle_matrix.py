# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_catalog_short_needle_matrix.py
# @brief:        Matrix of realistic printer inputs against the repository cartridge catalog.
# @details:      Responsibilities: pin the catalog outcome for full part numbers, short forms,
#                spaced and compact spellings, the CF387A cross-family case, model-only entries
#                (M506/M527) and the Canon 737 variants. Expectations are the outcomes on the
#                current code. Where the current outcome is wrong, the test says so and names
#                the tracked issue; the test is pinned so that any change shows up as a diff.
#                Main API: CatalogShortNeedleMatrixTests
#                Side effects: none (uses the repository catalog file, read-only)
# @dependencies: internal: core.yield_engine
# @notes:        Clears CARTRIDGE_YIELD_CATALOG so the repository catalog is used, and sets
#                CARTRIDGE_YIELD_MODE=local (the default) so yields are deterministic.
# =============================================================================

"""Catalog matrix for short digit needles: full, short, compact, CF387A, model-only and Canon 737."""

import os
import unittest
from unittest import mock

from core.yield_engine import DEFAULT_YIELD_PER_PAGE, _catalog_lookup

# Printer model strings. Each one contains the substring that the catalog model rule expects.
M225DN = "HP LaserJet Pro MFP M225dn"
M201 = "HP LaserJet Pro M201"
M527 = "HP LaserJet MFP M527"
E52645 = "HP LaserJet Managed MFP E52645"
M506_ENTERPRISE = "HP LaserJet Enterprise M506dn"
M506_MFP = "HP LaserJet MFP M506"
CANON_MF240 = "Canon MF240 Series"
CANON_MF250 = "Canon MF250 Series"
BROTHER_HL = "Brother HL-L2350DW"

# Catalog ids as they exist in cartridge_yield_catalog.json.
ID_CF283A = "hp-cf283a-83a-cartridge"
ID_M225DN_CF283A = "hp-laserjet-pro-m225dn-cf283a"
ID_CF287A = "hp-cf287a-87a-cartridge"
ID_M506_CF287A = "hp-laserjet-m506-cf287a"
ID_M527_CF287A = "hp-laserjet-mfp-m527-cf287a"
ID_CF287X = "hp-cf287x-87x-cartridge"
ID_CF287Y = "hp-cf287y-87y-cartridge"
ID_CF289A = "hp-cf289a-89a-cartridge"
ID_CF289X = "hp-cf289x-89x-cartridge"
ID_CF289Y = "hp-cf289y-89y-cartridge"
ID_CANON_737 = "canon-cartridge-737-cartridge"
ID_CANON_MODEL_737 = "canon-mf210-220-230-240-cartridge-737"

NONE = (None, DEFAULT_YIELD_PER_PAGE)

# (model, cartridge, expected (catalog_id, yield_per_page) in local mode)
FULL_PART_NUMBER = [
    (M225DN, "CF283A", (ID_CF283A, 1000)),
    (M201, "CF287A", (ID_CF287A, 5800)),
    (M527, "CF287X", (ID_CF287X, 11700)),
    (M527, "CF287Y", (ID_CF287Y, 19500)),
    (E52645, "CF289A", (ID_CF289A, 3250)),
    (E52645, "CF289X", (ID_CF289X, 6500)),
    (E52645, "CF289Y", (ID_CF289Y, 13000)),
]

SHORT_FORM_BARE = [
    (M225DN, "83A", (ID_CF283A, 1000)),
    (M201, "87A", (ID_CF287A, 5800)),
    (M527, "87X", (ID_CF287X, 11700)),
    (M527, "87Y", (ID_CF287Y, 19500)),
    (E52645, "89A", (ID_CF289A, 3250)),
    (E52645, "89X", (ID_CF289X, 6500)),
    (E52645, "89Y", (ID_CF289Y, 13000)),
]

SHORT_FORM_SPACED = [
    (M225DN, "HP 83A", (ID_CF283A, 1000)),
    (M201, "HP 87A", (ID_CF287A, 5800)),
    (M527, "HP 87X", (ID_CF287X, 11700)),
    (M527, "HP 87Y", (ID_CF287Y, 19500)),
    (E52645, "HP 89A", (ID_CF289A, 3250)),
    (E52645, "HP 89X", (ID_CF289X, 6500)),
    (E52645, "HP 89Y", (ID_CF289Y, 13000)),
]

# CURRENT BEHAVIOUR, pinned. The word-boundary rule rejects compact spellings such as "HP83A"
# because the letter "p" sits directly before "83a". Where no model entry exists, the result is
# the default. If a digit-before rule is added later, these expectations are expected to change.
COMPACT_CURRENT = [
    (M225DN, "HP83A", (ID_M225DN_CF283A, 1000)),  # id changes, yield unchanged
    (M201, "HP87A", NONE),                        # falls to the default
    (M527, "HP87X", (ID_M527_CF287A, 5800)),      # WRONG VALUE: model-only entry 4 wins (11700 is right)
    (M527, "HP87Y", (ID_M527_CF287A, 5800)),      # WRONG VALUE: model-only entry 4 wins (19500 is right)
    (E52645, "HP89A", NONE),
    (E52645, "HP89X", NONE),
    (E52645, "HP89Y", NONE),
]

# CF387A must not select the 87A entry. The fix holds on M201. On M506 and M527 the model-only
# entries still give a wrong value. This is tracked separately; the test pins the current outcome.
CF387A_CORRECT = [
    (M201, "CF387A", NONE),
    (M201, "HP CF387A", NONE),
]

CF387A_MODEL_ONLY_CURRENT = [
    (M527, "CF387A", (ID_M527_CF287A, 5800)),          # WRONG: model-only entry 4 (tracked bug)
    (M506_ENTERPRISE, "CF387A", (ID_M506_CF287A, 5800)),  # WRONG: model-only entry 3 (tracked bug)
    (M506_MFP, "CF387A", NONE),                         # correct only because this model string misses entry 3
]

# Model-only entries 3 and 4 have no cartridge_contains, so any cartridge on the model matches.
MODEL_ONLY_CURRENT = [
    (M527, "Black Cartridge", (ID_M527_CF287A, 5800)),  # WRONG for 87X/87Y printers (tracked bug)
    (M527, "HP87X", (ID_M527_CF287A, 5800)),            # same mechanism, via a compact name
]

CANON_CURRENT = [
    (CANON_MF240, "Cartridge 737", (ID_CANON_737, 1600)),
    (CANON_MF240, "737", (ID_CANON_MODEL_737, 1600)),        # matched through the model entry only
    (CANON_MF240, "Canon737", (ID_CANON_MODEL_737, 1600)),   # compact: falls to the model entry
    (CANON_MF250, "737", NONE),                              # bare 737 on an unlisted model: default
    (CANON_MF250, "Canon737", NONE),
    (BROTHER_HL, "Cartridge 737", (ID_CANON_737, 1600)),     # WRONG: entry 5 has no model rule (tracked)
]


class CatalogShortNeedleMatrixTests(unittest.TestCase):
    """Realistic printer inputs against the repository catalog. Local mode, black toner."""

    def setUp(self):
        """Use the repository catalog and the local yield mode, and clear the factor."""
        self.env = mock.patch.dict(os.environ, {"CARTRIDGE_YIELD_MODE": "local"})
        self.env.start()
        os.environ.pop("CARTRIDGE_YIELD_CATALOG", None)
        os.environ.pop("CARTRIDGE_YIELD_FACTOR", None)

    def tearDown(self):
        """Restore the environment."""
        self.env.stop()

    @staticmethod
    def _match(model, cartridge):
        """Returns (catalog_id, yield_per_page) for the lookup, or (None, default) when nothing matches."""
        hit = _catalog_lookup(model, cartridge, "black")
        if hit is None:
            return NONE
        return (hit["catalog_id"], hit["yield_per_page"])

    def _check(self, rows):
        """Asserts every (model, cartridge, expected) row, reporting each row on failure."""
        for model, cartridge, expected in rows:
            with self.subTest(model=model, cartridge=cartridge):
                self.assertEqual(self._match(model, cartridge), expected)

    # --- 1. full part number -> correct entry -----------------------------------

    def test_full_part_number_selects_the_cartridge_entry(self):
        """A full part number (CF283A, CF287X, CF289Y, ...) selects its own entry."""
        self._check(FULL_PART_NUMBER)

    # --- 2. short form -> correct entry ----------------------------------------

    def test_bare_short_form_selects_the_cartridge_entry(self):
        """A bare short form (83A, 87X, 89Y, ...) is a whole token and selects its own entry."""
        self._check(SHORT_FORM_BARE)

    def test_spaced_short_form_selects_the_cartridge_entry(self):
        """The short form with a prefix and a space (HP 83A, HP 87X, ...) selects its own entry."""
        self._check(SHORT_FORM_SPACED)

    # --- 3. compact form -> current behaviour (pinned) --------------------------

    def test_compact_form_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. HP83A/HP87X/... do not match as whole tokens (see module note).

        Expected to change if a digit-before boundary rule is added. Note the M527 rows: they
        return a wrong value (5800), not only the default.
        """
        self._check(COMPACT_CURRENT)

    # --- 4. CF387A -> no false match to 87A ------------------------------------

    def test_cf387a_does_not_match_the_87a_entry_on_m201(self):
        """CF387A on M201 must not select hp-cf287a-87a-cartridge (the R4 fix)."""
        self._check(CF387A_CORRECT)

    def test_cf387a_on_m506_and_m527_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. The model-only entries 3 and 4 still answer for CF387A.

        M506 result depends on the exact model string: "LaserJet MFP M506" misses entry 3,
        "LaserJet Enterprise M506dn" does not. Tracked separately (catalog decision).
        """
        self._check(CF387A_MODEL_ONLY_CURRENT)

    # --- 5. model-only entries (M506/M527) -> current behaviour (pinned) --------

    def test_model_only_entry_wins_for_any_cartridge_on_m527_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. Model-only entry 4 matches any cartridge on M527 (tracked bug).

        The generic name 'Black Cartridge' and the compact 'HP87X' both get 5800. The
        cartridge-specific entry 27 is only selected when the full name CF287X is reported
        (see test_full_part_number_selects_the_cartridge_entry).
        """
        self._check(MODEL_ONLY_CURRENT)

    def test_model_only_entry_has_no_cartridge_rule_in_the_catalog(self):
        """Structural guard: entries 3 and 4 have no cartridge_contains. If this fails, revisit the pins above."""
        from core.yield_engine import _load_yield_catalog
        by_id = {e.get("id"): e for e in _load_yield_catalog() if isinstance(e, dict)}
        for entry_id in (ID_M506_CF287A, ID_M527_CF287A):
            with self.subTest(entry_id=entry_id):
                self.assertIsNotNone(by_id.get(entry_id))
                self.assertFalse((by_id[entry_id].get("match") or {}).get("cartridge_contains"))

    # --- 6. Canon 737 variants (bucket C) --------------------------------------

    def test_canon_737_variants_current_behaviour_is_pinned(self):
        """CURRENT BEHAVIOUR, pinned. Canon 737 forms, with and without the model entry.

        The Brother row is WRONG (over-broad entry 5 has no model rule); tracked separately.
        """
        self._check(CANON_CURRENT)


if __name__ == "__main__":
    unittest.main()
