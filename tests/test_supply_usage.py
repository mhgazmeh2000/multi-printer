"""
@file:         tests/test_supply_usage.py
@brief:        Unit tests for system-counted cartridge usage (core/supply_usage.py).
@notes:        Pure functions only; no device, SNMP or web input is involved.
"""

import unittest

from core.supply_usage import (
    advance_record,
    counter_basis,
    genuine_cartridge_ids,
    new_record,
    usage_view,
)

D1, D2 = "2026-11-01", "2026-11-05"


class TestCounterBasis(unittest.TestCase):
    def test_black_uses_total_and_colours_use_full_colour(self):
        counters = {"total": 1200, "full_color": 300, "black_white": 900}
        self.assertEqual(counter_basis("black", counters), 1200)
        self.assertEqual(counter_basis("cyan", counters), 300)
        self.assertEqual(counter_basis("Magenta", counters), 300)

    def test_missing_or_invalid_counters_give_none(self):
        self.assertIsNone(counter_basis("cyan", {"total": 10}))
        self.assertIsNone(counter_basis("black", {"total": True}))
        self.assertIsNone(counter_basis("black", {"total": -1}))
        self.assertIsNone(counter_basis("drum", {"total": 10}))
        self.assertIsNone(counter_basis("black", None))


class TestGenuineIds(unittest.TestCase):
    def test_keeps_chip_and_serial_and_drops_fallbacks(self):
        ids = {"black": "CHIP-1", "cyan": "model:CF259A", "magenta": "SN-9", "yellow": "part:X"}
        types = {"black": "chip_id", "cyan": "chip_id", "magenta": "serial", "yellow": "chip_id"}
        self.assertEqual(genuine_cartridge_ids(ids, types), {"black": "CHIP-1", "magenta": "SN-9"})

    def test_unknown_identity_type_is_not_genuine(self):
        self.assertEqual(genuine_cartridge_ids({"black": "X1"}, {"black": "static"}), {})


class TestAdvanceRecord(unittest.TestCase):
    def test_first_poll_creates_record_at_current_counter(self):
        rec = advance_record(None, 1000, "CHIP-1", D1)
        self.assertEqual(rec, {"installed_on": D1, "start_pages": 1000, "last_pages": 1000,
                               "last_use": None, "cartridge_id": "CHIP-1"})

    def test_pages_grow_and_last_use_is_set_only_when_counter_rises(self):
        rec = new_record(1000, "CHIP-1", D1)
        rec = advance_record(rec, 1000, "CHIP-1", D2)
        self.assertIsNone(rec["last_use"])
        rec = advance_record(rec, 1040, "CHIP-1", D2)
        self.assertEqual(rec["last_use"], D2)
        self.assertEqual(usage_view(rec)["pages"], 40)

    def test_input_record_is_not_modified(self):
        rec = new_record(1000, "CHIP-1", D1)
        snapshot = dict(rec)
        advance_record(rec, 1100, "CHIP-1", D2)
        self.assertEqual(rec, snapshot)

    def test_changed_genuine_identity_starts_new_cartridge(self):
        rec = advance_record(new_record(1000, "CHIP-1", D1), 1500, "CHIP-2", D2)
        self.assertEqual(rec["installed_on"], D2)
        self.assertEqual(rec["start_pages"], 1500)
        self.assertEqual(usage_view(rec)["pages"], 0)
        self.assertIsNone(rec["last_use"])

    def test_identity_seen_for_first_time_does_not_reset(self):
        rec = advance_record(new_record(1000, None, D1), 1100, "CHIP-1", D2)
        self.assertEqual(rec["installed_on"], D1)
        self.assertEqual(rec["cartridge_id"], "CHIP-1")
        self.assertEqual(usage_view(rec)["pages"], 100)

    def test_counter_going_back_keeps_pages_already_counted(self):
        rec = new_record(1000, None, D1)
        rec = advance_record(rec, 1100, None, D1)
        rec = advance_record(rec, 50, None, D2)
        self.assertEqual(usage_view(rec)["pages"], 100)
        rec = advance_record(rec, 70, None, D2)
        self.assertEqual(usage_view(rec)["pages"], 120)

    def test_missing_counter_keeps_record(self):
        rec = new_record(1000, None, D1)
        rec = advance_record(rec, 1100, None, D1)
        self.assertEqual(advance_record(rec, None, None, D2), rec)

    def test_record_without_start_takes_first_counter(self):
        rec = advance_record({"installed_on": D1, "start_pages": None, "last_pages": None,
                              "last_use": None, "cartridge_id": None}, 700, None, D1)
        self.assertEqual(rec["start_pages"], 700)
        self.assertEqual(usage_view(rec)["pages"], 0)


class TestUsageView(unittest.TestCase):
    def test_no_record_shows_nothing(self):
        self.assertEqual(usage_view(None), {"pages": None, "first_install_date": None,
                                            "last_use_date": None})

    def test_view_of_record(self):
        rec = {"installed_on": D1, "start_pages": 1000, "last_pages": 1234,
               "last_use": D2, "cartridge_id": None}
        self.assertEqual(usage_view(rec), {"pages": 234, "first_install_date": D1,
                                           "last_use_date": D2})


class SupplyUsageBoundaryTests(unittest.TestCase):
    def test_nonfinite_fractional_bool_or_text_counter_never_raises(self):
        for value in (float('nan'), float('inf'), -float('inf'), 1.5, True, False, '100', -1, None):
            with self.subTest(value=value):
                self.assertIsNone(counter_basis('black', {'total': value}))
                rec = new_record(1000, 'chip', D1)
                self.assertEqual(advance_record(rec, value, 'chip', D2), rec)
        self.assertEqual(counter_basis('black', {'total': 12.0}), 12)
        self.assertEqual(counter_basis('black', {'total': 0}), 0)

    def test_identity_requires_explicit_provenance_and_filters_casefolded_fallbacks(self):
        self.assertEqual(genuine_cartridge_ids({'black': 'X'}, None), {})
        self.assertEqual(genuine_cartridge_ids({'black': 'X'}, {}), {})
        for cid in (' MODEL:X ', 'PART:X', 'Toner_Gen:7', '', '  ', True):
            with self.subTest(cid=cid):
                self.assertEqual(genuine_cartridge_ids({'black': cid}, {'black': 'chip_id'}), {})
        self.assertEqual(genuine_cartridge_ids({'BLACK': ' SN-7 '}, {'black': 'serial'}), {'black': 'SN-7'})

    def test_corrupt_stored_numbers_do_not_break_poll_or_invent_usage(self):
        for start, last in (('100', 120), (100, '120'), (True, 120), (100, True), (100, 50), (0, -1)):
            rec = dict(new_record(0, None, D1), start_pages=start, last_pages=last)
            with self.subTest(start=start, last=last):
                self.assertIsNone(usage_view(rec)['pages'])
                fixed = advance_record(rec, 500, None, D2)
                self.assertEqual(usage_view(fixed)['pages'], 0)
                self.assertEqual(fixed['installed_on'], D1)

    def test_multiple_real_counter_resets_keep_accumulated_pages(self):
        rec = new_record(1000, None, D1)
        for counter in (1100, 10, 30, 0, 15):
            rec = advance_record(rec, counter, None, D2)
        self.assertEqual(usage_view(rec)['pages'], 135)

    def test_json_roundtrip_is_additive_and_leaves_legacy_metadata_intact(self):
        import json
        from core.database import _load_cartridge_id_state, _dump_cartridge_id_state
        legacy = {'cartridge_ids': {'black': 'chip'}, 'cart_supply_pages': {'black': 7},
                  'cartridge_state': {'black': {'serial': 'chip', 'model': 'Test'}}}
        old_json = _dump_cartridge_id_state(legacy)
        self.assertNotIn('supply_usage', json.loads(old_json))
        self.assertIsNone(_dump_cartridge_id_state({}))
        new = dict(legacy, supply_usage={'black': new_record(1000, 'chip', D1)})
        self.assertEqual(_load_cartridge_id_state(_dump_cartridge_id_state(new)), new)
        self.assertEqual(_load_cartridge_id_state(old_json), legacy)


if __name__ == "__main__":
    unittest.main()
