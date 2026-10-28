# =============================================================================
# @file:         tests/test_generic_cartridge_change_engine.py
# @brief:        Tests for the generic cartridge-change engine.
# @details:      Responsibilities: Check the engine's verdicts for each evidence case.
#                Main API: GenericCartridgeChangeEngineTests
#                Side effects: none at import time
# @dependencies: internal: core.cartridge_change_engine
# @notes:        Can be run directly as a script.
# =============================================================================

"""Tests for the generic cartridge-change engine, checking its verdict for each evidence case."""

import unittest

from core.cartridge_change_engine import evaluate_cartridge_change


class GenericCartridgeChangeEngineTests(unittest.TestCase):
    """Tests for generic cartridge change engine."""
    def test_chip_id_change_confirms(self):
        """Verifies chip id change confirms."""
        prev = {
            "printer_ip": "10.0.0.10",
            "brand": "hp",
            "model": "LaserJet 400",
            "color": "black",
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_description": "HP 85A",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 22,
            "supply_life": 68,
            "generation": 3,
        }
        curr = {**prev, "chip_id": "HP-BBB-222", "serial": "SER-200"}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertTrue(result["should_emit"])
        self.assertEqual(result["confidence"], "CONFIRMED")
        self.assertIn("chip_id", result["detection_method"])
        self.assertEqual(result["old_identity"], "HP-AAA-111")
        self.assertEqual(result["new_identity"], "HP-BBB-222")

    def test_serial_change_is_probable_or_confirmed(self):
        """Verifies serial change is probable or confirmed."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_description": "HP 85A",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 25,
            "supply_life": 65,
            "generation": 4,
        }
        curr = {**prev, "chip_id": "HP-AAA-111", "serial": "SER-555"}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertTrue(result["should_emit"])
        self.assertIn(result["confidence"], {"PROBABLE", "CONFIRMED"})
        self.assertIn("serial", result["detection_method"])

    def test_level_jump_without_identity_is_not_confirmed(self):
        """Verifies level jump without identity is not confirmed."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_description": "HP 85A",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 10,
            "supply_life": 20,
            "generation": 6,
        }
        curr = {**prev, "remaining_level": 100, "supply_life": 90, "page_counter": 15050}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertFalse(result["should_emit"])
        self.assertIn(result["confidence"], {"UNKNOWN", "POSSIBLE"})

    def test_counter_reset_alone_is_not_confirmed(self):
        """Verifies counter reset alone is not confirmed."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 20,
            "supply_life": 25,
            "generation": 8,
        }
        curr = {**prev, "supply_counter": 2, "page_counter": 15010}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertFalse(result["should_emit"])
        self.assertIn(result["confidence"], {"UNKNOWN", "POSSIBLE"})
        self.assertIn("supply_counter", [ev["name"] for ev in result["evidence"]])

    def test_level_counter_and_life_reset_together_become_probable(self):
        """Verifies level counter and life reset together become probable."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 15,
            "supply_life": 20,
            "generation": 9,
        }
        curr = {
            **prev,
            "supply_counter": 1,
            "page_counter": 15030,
            "remaining_level": 95,
            "supply_life": 90,
        }

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertTrue(result["should_emit"])
        self.assertEqual(result["confidence"], "PROBABLE")

    def test_reboot_does_not_emit_false_positive(self):
        """Verifies reboot does not emit false positive."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 30,
            "supply_life": 35,
            "generation": 11,
        }
        curr = {**prev, "supply_counter": 1, "page_counter": 15, "remaining_level": 100}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=True, poll_gap_ok=True)

        self.assertFalse(result["should_emit"])
        self.assertEqual(result["confidence"], "UNKNOWN")

    def test_missing_poll_is_not_false_positive(self):
        """Verifies missing poll is not false positive."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 25,
            "supply_life": 20,
            "generation": 2,
        }
        curr = {**prev, "supply_counter": 1, "page_counter": 15010, "remaining_level": 90}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=False)

        self.assertFalse(result["should_emit"])
        self.assertEqual(result["confidence"], "UNKNOWN")

    def test_duplicate_poll_does_not_repeat_event(self):
        """Verifies duplicate poll does not repeat event."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 22,
            "supply_life": 65,
            "generation": 4,
        }
        curr = {**prev, "chip_id": "HP-BBB-222", "serial": "SER-200"}

        first = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)
        second = evaluate_cartridge_change(curr, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertTrue(first["should_emit"])
        self.assertFalse(second["should_emit"])

    def test_real_change_with_new_chip_emits_one_event(self):
        """Verifies real change with new chip emits one event."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 10,
            "supply_life": 12,
            "generation": 1,
        }
        curr = {
            **prev,
            "chip_id": "HP-CCC-333",
            "serial": "SER-701",
            "supply_counter": 1,
            "page_counter": 200,
            "remaining_level": 91,
            "supply_life": 95,
        }

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertTrue(result["should_emit"])
        self.assertEqual(result["confidence"], "CONFIRMED")
        self.assertEqual(len(result["evidence"]), 6)

    def test_no_evidence_does_not_confirm(self):
        """Verifies no evidence does not confirm."""
        prev = {
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 12000,
            "page_counter": 15000,
            "remaining_level": 20,
            "supply_life": 30,
            "generation": 2,
        }
        curr = {**prev}

        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)

        self.assertFalse(result["should_emit"])
        self.assertEqual(result["confidence"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
