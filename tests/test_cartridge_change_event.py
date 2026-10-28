# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_cartridge_change_event.py
# @brief:        Tests for the CARTRIDGE_CHANGED event.
# @details:      Responsibilities: Check when the event is raised and what it records.
#                Main API: CartridgeChangeEventTests
#                Side effects: none at import time
# @dependencies: internal: core, core.collectors.base, core.database
# @notes:        Can be run directly as a script.
# =============================================================================

"""
"CARTRIDGE_CHANGED" (CARTRIDGE_CHANGED) feature tests.

The detection pattern is exactly the same as the proven monochromatic REFILL pattern (candidate in mutation >20% with
Low plates → level stability in 2 consecutive polls → one-shot event), but per-color and
Only real data (raw_level) will be logged until changing color cartridges (C/M/Y).
And show estimations (canon_status_code, etc.) do not create fake events.

Key rules tested:
  1) Cyan 30→90 jump: first candidate, after 2 stable polls → exactly one event.
  2) Oscillation (30→90→30): no event.
  3) Mutation with a lot of printing (delta_pages >= 50): not a candidate (legacy behavior).
  4) The legacy color (black) has the same path as REFILL → no duplicate event.
  5) Colors without real data and non-CMYK keys (drum): no event.
  6) manual_override: detection completely off.
  7) The event is registered only once (one-shot).
  8) Returning the level below the candidate cancels pending and resets the new cycle of hits to zero.
"""
import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod

recorded_events = []


def _fake_add_event(ip, etype, details):
    """Helper: fake add event."""
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class CartridgeChangeEventTests(unittest.TestCase):
    """Tests for cartridge change event."""
    IP = "10.9.9.9"
    UP = 600_000_000  # High uptime (hundreds of seconds) so that reset detection does not interfere

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        recorded_events.clear()
        self._orig_add_event = base_mod.add_event
        base_mod.add_event = _fake_add_event

    def tearDown(self):
        """Release the resources that each test in this class created."""
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ─── helpers ───
    def _events(self, etype):
        """Helper: events."""
        return [e for e in recorded_events if e["type"] == etype]

    def _seed(self, total=1000, manual_override=0):
        """Helper: seed."""
        store._prev.set(self.IP, {
            "print_total": total,
            "full_color": 0,
            "black_white": total,
            "toner_level": 60,
            "alert_codes": [],
            "last_alert_codes": [],
            "uptime": self.UP,
            "manual_override": manual_override,
        })
        recorded_events.clear()

    def _poll(self, total, toner_levels, legacy_color="black",
              current_toner=60, uptime=None):
        """A real poll: prev is read from the store (like the enhanced path)."""
        prev = store._prev.get(self.IP) or {}
        base_mod._counters_event(
            self.IP, total, prev, [], [],
            full_color=0, black_white=total,
            current_toner_level=current_toner,
            prev_toner_level=prev.get("toner_level"),
            toner_levels=toner_levels,
            legacy_toner_color=legacy_color,
            uptime=uptime if uptime is not None else (self.UP + total * 100),
        )

    # ─── 1) The main scenario: replacing the color cartridge ───
    def test_color_cartridge_change_logged_once_after_two_stable_polls(self):
        """Verifies color cartridge change logged once after two stable polls."""
        self._seed(total=1000)
        # baseline: true cyan level 30%
        self._poll(1000, {"cyan": 30})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        # Jump to 90% (substitution) → candidate only, not event yet
        self._poll(1002, {"cyan": 90})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        # First stable poll (hits=1)
        self._poll(1004, {"cyan": 90})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        # Second persistent poll (hits=2) → event confirmed
        self._poll(1006, {"cyan": 90})
        evs = self._events("CARTRIDGE_CHANGED")
        self.assertEqual(len(evs), 1)
        d = evs[0]["details"]
        self.assertEqual(d.get("color"), "cyan")
        self.assertEqual(d.get("prev_toner"), 30)
        self.assertEqual(d.get("new_toner"), 90)
        self.assertTrue(d.get("auto_detected"))
        self.assertTrue(d.get("confirmed"))
        self.assertEqual(d.get("severity"), "info")
        self.assertIn("آبی", d.get("message", ""))
        self.assertIn("تعویض", d.get("message", ""))
        # One shot: the next polls do not create a duplicate event
        self._poll(1008, {"cyan": 90})
        self._poll(1010, {"cyan": 88})
        self.assertEqual(len(self._events("CARTRIDGE_CHANGED")), 1)

    # ─── 2) Oscillation/Blip: No event ───
    def test_flapping_level_never_confirms(self):
        """Verifies flapping level never confirms."""
        self._seed(total=1000)
        self._poll(1000, {"magenta": 30})
        self._poll(1001, {"magenta": 90})   # candidate
        self._poll(1002, {"magenta": 30})   # Back → Invalid candidate
        self._poll(1003, {"magenta": 90})   # new candidate
        self._poll(1004, {"magenta": 30})   # void
        self._poll(1005, {"magenta": 88})   # Jump from 30 → fresh candidate
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── 3) Mutation with a lot of printing: not a candidate (legacy behavior) ───
    def test_no_candidate_when_many_pages_printed(self):
        """Verifies no candidate when many pages printed."""
        self._seed(total=1000)
        self._poll(1000, {"yellow": 30})
        # 60 print pages + level jump → window <50 is rejected
        self._poll(1060, {"yellow": 95})
        self._poll(1062, {"yellow": 95})
        self._poll(1064, {"yellow": 95})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── 4) Legacy color (black): the REFILL route is separate; No repetition ───
    def test_legacy_color_not_double_logged(self):
        """Verifies legacy color not double logged."""
        self._seed(total=1000)
        # Black is connected to current_toner_level (legacy REFILL), fixed cyan
        self._poll(1000, {"black": 40, "cyan": 60}, current_toner=40)
        self._poll(1001, {"black": 95, "cyan": 60}, current_toner=95)
        self._poll(1002, {"black": 95, "cyan": 60}, current_toner=95)
        self._poll(1003, {"black": 95, "cyan": 60}, current_toner=95)
        # For black: Legacy REFILL is verified but CARTRIDGE_CHANGED never
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        refills = self._events("REFILL")
        self.assertEqual(len(refills), 1)
        self.assertTrue(refills[0]["details"].get("confirmed"))

    # ─── 5) Real data only: color without raw and non-CMYK keys are ignored ───
    def test_only_real_levels_and_cmyk_keys_are_considered(self):
        """Verifies only real levels and cmyk keys are considered."""
        self._seed(total=1000)
        # The "drum" is not a toner cartridge; and a color that is not in toner_levels (eg
        # canon_status_code estimate that does not have raw_level) is not entered at all.
        self._poll(1000, {"drum": 30, "cyan": 50})
        self._poll(1001, {"drum": 95, "cyan": 50})
        self._poll(1002, {"drum": 95})
        self._poll(1003, {"drum": 95})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── 6) manual_override: off detection ───
    def test_manual_override_disables_detection(self):
        """Verifies manual override disables detection."""
        self._seed(total=1000, manual_override=1)
        self._poll(1000, {"cyan": 30})
        self._poll(1001, {"cyan": 95})
        self._poll(1002, {"cyan": 95})
        self._poll(1003, {"cyan": 95})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── 7) Canceling pending and starting a new cycle with zero hits ───
    def test_pending_resets_after_level_fall(self):
        """Verifies pending resets after level fall."""
        self._seed(total=1000)
        self._poll(1000, {"cyan": 30})
        self._poll(1001, {"cyan": 90})   # candidate
        self._poll(1002, {"cyan": 90})   # hits=1
        self._poll(1003, {"cyan": 35})   # Fall → invalid candidate
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        self._poll(1004, {"cyan": 90})   # new candidate (hits=0)
        self._poll(1005, {"cyan": 90})   # hits=1 → No event yet
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        self._poll(1006, {"cyan": 90})   # hits=2 → event
        evs = self._events("CARTRIDGE_CHANGED")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["details"].get("prev_toner"), 35)

    # ─── 8) Changing multiple colors at the same time: each one an event ───
    def test_multiple_colors_each_logged_once(self):
        """Verifies multiple colors each logged once."""
        self._seed(total=1000)
        levels = {"cyan": 30, "magenta": 25, "yellow": 40}
        self._poll(1000, levels)
        self._poll(1001, {"cyan": 92, "magenta": 25, "yellow": 90})
        self._poll(1002, {"cyan": 92, "magenta": 25, "yellow": 90})
        self._poll(1003, {"cyan": 92, "magenta": 25, "yellow": 90})
        evs = self._events("CARTRIDGE_CHANGED")
        colors = sorted(e["details"].get("color") for e in evs)
        self.assertEqual(colors, ["cyan", "yellow"])
        self.assertEqual(len(evs), 2)


if __name__ == "__main__":
    unittest.main()
