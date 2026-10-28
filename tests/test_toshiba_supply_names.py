# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_toshiba_supply_names.py
# @brief:        Tests for reading the cartridge names of a Toshiba printer.
# @details:      Responsibilities: Check that _read_supply_names asks the standard
#                supplies table for each colour and tolerates missing or failing reads.
#                Main API: ToshibaSupplyNamesTests
#                Side effects: none at import time
# @dependencies: internal: core.collectors.toshiba
# @notes:        SNMP is mocked; no network is used. Can be run directly as a script.
# =============================================================================

import unittest
from unittest import mock

import core.collectors.toshiba as toshiba_mod

IP = "192.0.2.10"
COMMUNITY = "public"
DESC_PREFIX = "1.3.6.1.2.1.43.11.1.1.6.1."


class ToshibaSupplyNamesTests(unittest.TestCase):
    """Tests for _read_supply_names."""

    def _run(self, responder):
        """Run the helper with a fake SNMP function and return (names, calls)."""
        with mock.patch.object(toshiba_mod, "snmp_get_with_fallback", side_effect=responder) as fake:
            names = toshiba_mod._read_supply_names(IP, COMMUNITY)
        return names, fake

    def test_reads_each_colour_from_its_supply_row(self):
        """Each colour is read from its own row of the supplies table."""
        rows = {DESC_PREFIX + "1": "Black Cart", DESC_PREFIX + "2": "Cyan Cart",
                DESC_PREFIX + "3": "Magenta Cart", DESC_PREFIX + "4": "Yellow Cart"}
        names, fake = self._run(lambda ip, oid, community, timeout=None: rows.get(oid))
        self.assertEqual(names, {"black": "Black Cart", "cyan": "Cyan Cart",
                                 "magenta": "Magenta Cart", "yellow": "Yellow Cart"})
        requested = sorted(c.args[1] for c in fake.call_args_list)
        self.assertEqual(requested, sorted(DESC_PREFIX + str(i) for i in (1, 2, 3, 4)))

    def test_uses_the_printer_and_community_without_forcing_a_version(self):
        """The read goes through the shared version detection (no explicit version)."""
        _, fake = self._run(lambda ip, oid, community, timeout=None: "x")
        for c in fake.call_args_list:
            self.assertEqual(c.args[0], IP)
            self.assertEqual(c.args[2], COMMUNITY)
            self.assertNotIn("version", c.kwargs)

    def test_missing_or_empty_value_leaves_the_colour_out(self):
        """None or blank values are not reported as names."""
        rows = {DESC_PREFIX + "1": None, DESC_PREFIX + "2": "   ",
                DESC_PREFIX + "3": "Magenta Cart"}
        names, _ = self._run(lambda ip, oid, community, timeout=None: rows.get(oid))
        self.assertEqual(names, {"magenta": "Magenta Cart"})

    def test_failure_for_one_colour_does_not_stop_the_others(self):
        """An exception on one read is logged and the remaining colours are still read."""
        def responder(ip, oid, community, timeout=None):
            if oid.endswith(".2"):
                raise OSError("timeout")
            return "Part " + oid[-1]

        with self.assertLogs("PrinterMonitor", level="DEBUG") as logs:
            names, _ = self._run(responder)
        self.assertEqual(names, {"black": "Part 1", "magenta": "Part 3", "yellow": "Part 4"})
        self.assertTrue(any("supply description read failed" in m for m in logs.output))

    def test_programming_errors_are_not_silently_swallowed(self):
        """A NameError-type bug surfaces in the debug log instead of disappearing."""
        def responder(ip, oid, community, timeout=None):
            raise NameError("name 'snmp_version' is not defined")

        with self.assertLogs("PrinterMonitor", level="DEBUG") as logs:
            names, _ = self._run(responder)
        self.assertEqual(names, {})
        self.assertTrue(any("snmp_version" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
