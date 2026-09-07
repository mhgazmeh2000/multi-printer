import json
import os
import tempfile
import unittest

from core import store
from core.database import init_db, db_connection, get_log
import core.collectors.base as base_mod
from core.cartridge_change_engine import evaluate_cartridge_change


class CartridgePipelineIntegrationTests(unittest.TestCase):
    IP = "10.10.10.10"
    UP = 600_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()

    def tearDown(self):
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _logs(self, etype=None):
        rows = get_log(self.IP, limit=200)
        if etype is None:
            return rows
        return [row for row in rows if row.get("type") == etype]

    def _poll(self, total, *, current_toner_level=None, prev_toner_level=None,
              toner_levels=None, cartridge_ids=None, cartridge_supply_pages=None,
              signal_quality=None, supply_names=None, uptime=None, legacy_toner_color="black"):
        prev = store._prev.get(self.IP) or {}
        base_mod._counters_event(
            self.IP,
            total,
            prev,
            [],
            [],
            full_color=0,
            black_white=total,
            current_toner_level=current_toner_level,
            prev_toner_level=prev_toner_level if prev_toner_level is not None else prev.get("toner_level"),
            toner_levels=toner_levels,
            legacy_toner_color=legacy_toner_color,
            cartridge_ids=cartridge_ids,
            cartridge_supply_pages=cartridge_supply_pages,
            signal_quality=signal_quality,
            supply_names=supply_names,
            uptime=uptime if uptime is not None else (self.UP + total * 100),
        )

    def test_poll_baseline_and_no_event(self):
        self._poll(1000, current_toner_level=55)
        self.assertEqual(self._logs("CARTRIDGE_CHANGED"), [])
        self.assertEqual(len(self._logs()), 0)

        self._poll(1000, current_toner_level=55)
        self.assertEqual(self._logs("CARTRIDGE_CHANGED"), [])
        self.assertEqual(len(self._logs()), 0)

    def test_chip_id_change_emits_exactly_one_event_and_duplicate_is_blocked(self):
        self._poll(1000, current_toner_level=60, cartridge_ids={"black": "HP-AAA-111"})
        self._poll(1001, current_toner_level=60, cartridge_ids={"black": "HP-BBB-222"})

        events = self._logs("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "CARTRIDGE_CHANGED")
        self.assertEqual(event["details"]["old_identity"], "HP-AAA-111")
        self.assertEqual(event["details"]["new_identity"], "HP-BBB-222")
        self.assertEqual(event["details"]["confidence"], "CONFIRMED")
        self.assertIn("chip_id", event["details"]["detection_method"])
        self.assertTrue(event["details"].get("event_id"))

        self._poll(1002, current_toner_level=60, cartridge_ids={"black": "HP-BBB-222"})
        self.assertEqual(len(self._logs("CARTRIDGE_CHANGED")), 1)

    def test_four_poll_identity_sequence_is_zero_zero_one_zero(self):
        self._poll(1000, current_toner_level=60, cartridge_ids={"black": "ABC"})
        self.assertEqual(len(self._logs("CARTRIDGE_CHANGED")), 0)
        self._poll(1001, current_toner_level=60, cartridge_ids={"black": "ABC"})
        self.assertEqual(len(self._logs("CARTRIDGE_CHANGED")), 0)
        self._poll(1002, current_toner_level=60, cartridge_ids={"black": "XYZ"})
        self.assertEqual(len(self._logs("CARTRIDGE_CHANGED")), 1)
        self._poll(1003, current_toner_level=60, cartridge_ids={"black": "XYZ"})
        self.assertEqual(len(self._logs("CARTRIDGE_CHANGED")), 1)

    def test_without_chip_id_multiple_reset_signals_use_engine_confidence(self):
        self._poll(1000, current_toner_level=15, toner_levels={"black": 15},
                   cartridge_supply_pages={"black": 12000})
        self._poll(1001, current_toner_level=95, toner_levels={"black": 95},
                   cartridge_supply_pages={"black": 1})
        events = self._logs("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 1)
        details = events[0]["details"]
        self.assertEqual(details["confidence"], "PROBABLE")
        self.assertIn("supply_counter", details["detection_methods"])
        self.assertIn("remaining_level", details["detection_methods"])
        self.assertIn("supply_life", details["detection_methods"])

    def test_level_only_change_is_not_event(self):
        self._poll(1000, current_toner_level=30, cartridge_ids={"black": "HP-AAA-111"})
        self._poll(1001, current_toner_level=92, cartridge_ids={"black": "HP-AAA-111"})
        self.assertEqual(self._logs("CARTRIDGE_CHANGED"), [])

    def test_counter_reset_without_sufficient_evidence_is_not_confirmed(self):
        prev = {
            "print_total": 15000,
            "full_color": 0,
            "black_white": 15000,
            "toner_level": 40,
            "uptime": self.UP,
        }
        store._prev.set(self.IP, prev)
        self._poll(120, current_toner_level=95, prev_toner_level=40, uptime=self.UP - 100)

        self.assertEqual(self._logs("CARTRIDGE_CHANGED"), [])
        types = [row["type"] for row in self._logs()]
        self.assertIn("COUNTER_ANOMALY", types)

    def test_counter_level_and_life_reset_becomes_probable(self):
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
            "chip_id": "HP-AAA-111",
            "serial": "SER-100",
            "supply_counter": 1,
            "page_counter": 15030,
            "remaining_level": 95,
            "supply_life": 90,
            "generation": 9,
        }
        result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=True)
        self.assertTrue(result["should_emit"])
        self.assertEqual(result["confidence"], "PROBABLE")

    def test_reboot_and_missing_poll_do_not_create_cart_change_event(self):
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

        gap_result = evaluate_cartridge_change(prev, curr, reboot_detected=False, poll_gap_ok=False)
        self.assertFalse(gap_result["should_emit"])
        self.assertEqual(gap_result["confidence"], "UNKNOWN")

    def test_restart_after_db_snapshot_keeps_identity_without_false_positive(self):
        self._poll(1000, current_toner_level=40, cartridge_ids={"black": "HP-AAA-111"})
        store._prev._cache.clear()
        restored = store._prev.get(self.IP) or {}
        self.assertEqual(restored.get("cartridge_ids"), {"black": "HP-AAA-111"})

        self._poll(1001, current_toner_level=40, cartridge_ids={"black": "HP-AAA-111"})
        self.assertEqual(self._logs("CARTRIDGE_CHANGED"), [])

    def test_multicartridge_isolation_black_does_not_touch_cyan(self):
        base_prev = {
            "print_total": 1000,
            "full_color": 0,
            "black_white": 1000,
            "toner_level": 60,
            "uptime": self.UP,
        }
        store._prev.set(self.IP, base_prev)
        self._poll(1000, current_toner_level=60, toner_levels={"black": 60, "cyan": 55}, cartridge_ids={"black": "HP-AAA-111", "cyan": "C-OLD"})
        self._poll(1001, current_toner_level=60, toner_levels={"black": 93, "cyan": 55}, cartridge_ids={"black": "HP-BBB-222", "cyan": "C-OLD"})

        events = self._logs("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["details"]["color"], "black")
        self.assertIn("HP-BBB-222", json.dumps(events[0]["details"]))

    def test_database_contains_event_metadata_and_single_event(self):
        self._poll(1000, current_toner_level=60, cartridge_ids={"black": "HP-AAA-111"})
        self._poll(1001, current_toner_level=60, cartridge_ids={"black": "HP-BBB-222"})
        self._poll(1002, current_toner_level=60, cartridge_ids={"black": "HP-BBB-222"})

        events = self._logs("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 1)
        details = events[0]["details"]
        self.assertTrue(details.get("event_id"))
        self.assertEqual(details["confidence"], "CONFIRMED")
        self.assertIn("chip_id", details["detection_method"])
        self.assertIsInstance(details.get("evidence"), list)
        self.assertEqual(details["old_identity"], "HP-AAA-111")
        self.assertEqual(details["new_identity"], "HP-BBB-222")


if __name__ == "__main__":
    unittest.main()
