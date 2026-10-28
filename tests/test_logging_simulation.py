# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_logging_simulation.py
# @brief:        End-to-end simulation with a virtual printer to prove log correctness.
# @details:      Responsibilities: Simulate printer activity and check the generated log rows.
#                Main API: VirtualToshiba, SimBase, LoggingSimulationTests
#                Side effects: network I/O
# @dependencies: internal: core, core.collectors.base, core.database, core.enhanced_collector,
#                core.snmp.oid_map
# @notes:        Can be run directly as a script.
# =============================================================================

"""
End-to-end "virtual printer" emulator to prove the correctness of logs.

Idea: Instead of relying on unit testing, we create a virtual Toshiba device that behaves multiple times
Plays a real week (A4/A3 print/copy/color, SNMP failure, monitor failure,
reboot, factory reset, cartridge replacement, repeated poll) and recorded events with
We compare ground truth.

This test goes through the same path as the production code:
_collect_toshiba_job_data  →  attribute_paper_size  →  _counters_event
"""
import os
import tempfile
import unittest
from datetime import datetime

from core import store
from core.database import init_db
import core.collectors.base as base_mod
import core.enhanced_collector as ec
from core.snmp.oid_map import OIDS

recorded_events = []


def _fake_add_event(ip, etype, details):
    """Helper: fake add event."""
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class VirtualToshiba:
    """Virtual printer with compatible counters (a3_total + a4_total == print_total)."""

    def __init__(self, large=40_000, small=160_000, fc_from_large=0):
        """Initialize the instance."""
        self.a3 = large            # Branch 207: Whole large paper
        self.a4 = small            # Branch 208: Whole small paper
        self.fc = 0                # Default mono
        self.toner = 60
        self.uptime = 900_000_000  # sysUpTime (hundreds of seconds)
        self.fail_keys = set()     # Keys that this poll should return None (SNMP failure)
        self.uptime_fail = False
        # ground truth
        self.truth = {"large": 0, "small": 0, "color": 0}

    @property
    def total(self):
        """Helper: total."""
        return self.a3 + self.a4

    @property
    def bw(self):
        """Helper: bw."""
        return self.total - self.fc

    def print_job(self, large=0, small=0, color_pages=0):
        """Helper: print job."""
        self.a3 += large
        self.a4 += small
        self.fc += color_pages
        self.truth["large"] += large
        self.truth["small"] += small
        self.truth["color"] += color_pages
        self.toner = max(5, self.toner)  # Toner consumption is kept constant in the tests

    def poll_uptime(self):
        """Helper: poll uptime."""
        if self.uptime_fail:
            return None
        return self.uptime

    def tick(self, seconds=60):
        """Helper: tick."""
        self.uptime += seconds * 100


class SimBase(unittest.TestCase):
    """Helper class: sim base."""
    IP = "192.168.1.99"

    def setUp(self):
        """Prepare the fixtures that each test in this class uses."""
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        recorded_events.clear()
        with store.printers_lock:
            store.PRINTERS[:] = []
        self._orig_add_event = base_mod.add_event
        self._orig_snmp = ec.snmp_get_with_fallback
        base_mod.add_event = _fake_add_event
        self.dev = VirtualToshiba()
        ec.snmp_get_with_fallback = self._snmp

    def tearDown(self):
        """Release the resources that each test in this class created."""
        base_mod.add_event = self._orig_add_event
        ec.snmp_get_with_fallback = self._orig_snmp
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ── Virtual SNMP: Only OIDs that _collect_toshiba_job_data reads ──
    def _snmp(self, ip, oid, community, **kw):
        """Helper: snmp."""
        d = self.dev
        table = {
            OIDS["a3_total"]: d.a3, OIDS["a4_total"]: d.a4,
            OIDS["print_fc"]: d.fc, OIDS["print_bw"]: d.bw,
            OIDS["print_printer_fc"]: 0, OIDS["print_printer_bw"]: 0,
            OIDS["print_copy_fc"]: 0, OIDS["print_copy_bw"]: 0,
            OIDS["print_twin"]: 0, OIDS["print_fax"]: 0, OIDS["print_list"]: 0,
            OIDS["scan_fc"]: 0, OIDS["scan_bw"]: 0,
            OIDS["scan_net_fc"]: 0, OIDS["scan_net_bw"]: 0,
            OIDS["a4r_total"]: 0, OIDS["a4r_fc"]: 0, OIDS["a4r_bw"]: 0,
            OIDS["a5_total"]: 0, OIDS["a5_fc"]: 0, OIDS["a5_bw"]: 0,
            OIDS["b4_total"]: 0, OIDS["b4_fc"]: 0, OIDS["b4_bw"]: 0,
        }
        # Reverse mapping OIDS[key] → key for fail_keys
        key = next((k for k, v in OIDS.items() if v == oid), None)
        if key in d.fail_keys:
            return None
        return table.get(oid, 0)

    def poll(self):
        """Exactly equivalent to the collect_enhanced event block on the virtual machine."""
        d = self.dev
        prev = store._prev.get(self.IP) or {}
        total = None if "print_total" in d.fail_keys else d.total
        color = None if total is None else (None if "print_fc" in d.fail_keys else d.fc)
        toshiba_data = ec._collect_toshiba_job_data(self.IP, "public", 1, total, color)
        ut = d.poll_uptime()
        base_mod._counters_event(
            self.IP, total, prev, [], [],
            full_color=color,
            black_white=(toshiba_data or {}).get("black_white_for_event"),
            paper_size=(toshiba_data or {}).get("paper_size"),
            current_toner_level=d.toner, prev_toner_level=prev.get("toner_level"),
            uptime=ut,
            a3_total=(toshiba_data or {}).get("a3_total"),
            a4_total=(toshiba_data or {}).get("a4_total"),
            poll_timestamp=datetime.now().isoformat(),
            paper_split=(toshiba_data or {}).get("paper_split"),
        )
        d.tick()
        d.fail_keys.clear()
        d.uptime_fail = False

    # helpers ────────────────────────────────────────────────
    def events_of(self, etype):
        """Helper: events of."""
        return [e for e in recorded_events if e["type"] == etype]

    def pages(self, etype="PRINT"):
        """Helper: pages."""
        return sum((e["details"].get("pages") or 0) for e in self.events_of(etype))

    def split_sums(self):
        """Helper: split sums."""
        large = small = 0
        for e in recorded_events:
            s = e["details"].get("paper_split")
            if s:
                large += s.get("large") or 0
                small += s.get("small") or 0
        return large, small

    def unattributed_pages(self):
        """Helper: unattributed pages."""
        return sum((e["details"].get("pages") or 0)
                   for e in self.events_of("PRINT") + self.events_of("PRINT_GAP")
                   if not e["details"].get("paper_split"))

    def assert_no_fake_reset(self):
        """Helper: assert no fake reset."""
        self.assertEqual(self.events_of("COUNTER_RESET"), [],
                         "COUNTER_RESET کاذب نباید ثبت شود")

    def assert_page_integrity(self, note=""):
        """The sum of logged pages (PRINT for online printing) must be exactly equal to the truth."""
        d = self.dev
        self.assertEqual(self.pages("PRINT"), d.truth["large"] + d.truth["small"],
                         f"pages mismatch {note}: logged={self.pages('PRINT')} "
                         f"truth={d.truth['large'] + d.truth['small']}")
        lg, sm = self.split_sums()
        attributed = lg + sm + self.unattributed_pages()
        self.assertEqual(attributed, d.truth["large"] + d.truth["small"],
                         "مجموع صفحات نسبت‌داده‌شده+نامشخص باید با حقیقت برابر باشد")


class LoggingSimulationTests(SimBase):

    """Tests for logging simulation."""
    def test_01_first_poll_baseline_no_fake_giant_print(self):
        # The old famous bug: the first poll should not log 200,000 pages
        """Verifies 01 first poll baseline no fake giant print."""
        self.poll()
        self.assertEqual(recorded_events, [], "اولین poll باید فقط baseline بگیرد")
        self.dev.print_job(small=5)
        self.poll()
        self.assertEqual(self.pages("PRINT"), 5)

    def test_02_normal_office_week_exact_pages_and_labels(self):
        """Verifies 02 normal office week exact pages and labels."""
        self.poll()  # baseline
        for i in range(80):
            self.dev.print_job(small=7)
            if i % 4 == 0:
                self.dev.print_job(large=3)
            self.poll()
        self.assert_page_integrity("office-week")
        lg, sm = self.split_sums()
        self.assertEqual((lg, sm), (self.dev.truth["large"], self.dev.truth["small"]),
                         "تفکیک بزرگ/کوچک باید یک‌به‌یک با حقیقت باشد")
        self.assert_no_fake_reset()
        # Mixing: Mixed events must have strict segregation
        mixed = [e for e in self.events_of("PRINT")
                 if e["details"].get("paper_size") == "Mixed"]
        self.assertTrue(mixed, "حداقل چند رویداد Mixed انتظار داریم")
        for e in mixed:
            self.assertTrue(e["details"].get("paper_split"))

    def test_03_uptime_read_failure_never_triggers_counter_reset(self):
        # Production bug: 276 false COUNTER_RESET with current_uptime=0
        """Verifies 03 uptime read failure never triggers counter reset."""
        self.poll()
        for _ in range(30):
            self.dev.print_job(small=4)
            self.dev.uptime_fail = True     # Uptime is not read at all
            self.poll()
        self.assert_no_fake_reset()
        self.assertEqual(self.events_of("COUNTER_ANOMALY"), [])
        self.assert_page_integrity("uptime-fail")

    def test_04_vendor_total_failure_keeps_snapshot_no_spike(self):
        # Production bug: print_total fluctuation (160,432 → 2,124 → 160,503)
        """Verifies 04 vendor total failure keeps snapshot no spike."""
        self.poll()
        self.dev.print_job(small=10)
        self.dev.fail_keys.add("print_total")      # OID vendor of this poll is not read
        self.poll()
        self.assertEqual(self.pages("PRINT"), 0)
        self.assertTrue(self.events_of("SNMP_COUNTER_READ_ERROR"))
        self.dev.print_job(small=10)
        self.poll()                                 # 20 pages in two intervals, a correct log
        self.assertEqual(self.pages("PRINT"), 20)
        self.assertEqual(self.events_of("PRINT_OVERFLOW"), [])
        self.assert_no_fake_reset()

    def test_05_genuine_reboot_without_counter_reset_logs_normally(self):
        # Actual reboot: uptime is reset, but the counter is saved in NVRAM
        """Verifies 05 genuine reboot without counter reset logs normally."""
        self.poll()
        self.dev.print_job(small=6)
        self.dev.uptime = 2_000                     # The device rebooted
        self.poll()
        self.assert_no_fake_reset()
        self.assertEqual(self.events_of("COUNTER_ANOMALY"), [])
        self.assert_page_integrity("reboot")
        # Continue printing after reboot is also accurate
        self.dev.print_job(small=6)
        self.poll()
        self.assert_page_integrity("after-reboot")

    def test_06_mild_counter_drop_with_reboot_is_anomaly_not_reset(self):
        # Partial reduction + reboot without evidence of actual reset → COUNTER_ANOMALY + snapshot retention
        """Verifies 06 mild counter drop with reboot is anomaly not reset."""
        self.poll()
        self.dev.print_job(small=1)
        self.poll()
        # Device rebooted and the counter lags a bit (unstable SNMP resources)
        self.dev.a4 -= 5
        self.dev.uptime = 3_000
        self.poll()
        self.assert_no_fake_reset()
        self.assertEqual(len(self.events_of("COUNTER_ANOMALY")), 1)
        # saved snapshot; Continue printing without double counting.
        # Physical point (not a bug): 5 pages that the counter of the device "forgets"
        # cannot be logged — they cannot be retrieved from any source. system instead
        # The false log has registered a clear COUNTER_ANOMALY so that the loss is auditable.
        self.dev.print_job(small=8)
        self.poll()
        truth = self.dev.truth["large"] + self.dev.truth["small"]
        self.assertEqual(self.pages("PRINT"), truth - 5,
                         "تنها صفحاتِ گمشده باید همان ۵ صفحه‌ای باشند که خود شمارنده دستگاه از دست داد")
        self.assertLessEqual(self.pages("PRINT"), truth,
                             "لاگ هرگز نباید بیشتر از واقعیت باد شود")

    def test_07_factory_reset_logs_exactly_one_counter_reset(self):
        """Verifies 07 factory reset logs exactly one counter reset."""
        self.poll()
        self.dev.print_job(small=10)
        self.poll()
        before = self.pages("PRINT")
        # Factory reset: zero counter + uptime reset
        self.dev.a3 = self.dev.a4 = self.dev.fc = 0
        self.dev.uptime = 5_000
        self.poll()
        resets = self.events_of("COUNTER_RESET")
        self.assertEqual(len(resets), 1, "باید دقیقاً یک COUNTER_RESET واقعی ثبت شود")
        self.dev.print_job(small=4, large=1)
        self.poll()
        self.assertEqual(self.pages("PRINT") - before, 5,
                         "چاپ بعد از ریست باید از baseline جدید درست حساب شود")

    def test_08_monitor_downtime_gap_becomes_print_gap_not_inflated_print(self):
        # Non-physical jump of the counter (the same spikes + 123 thousand production pages: source
        # counter oscillated) → pending and then PRINT_GAP, not inflated PRINT.
        """Verifies 08 monitor downtime gap becomes print gap not inflated print."""
        self.poll()
        self.dev.print_job(small=400, large=100)      # A leap of 500 in one interval
        self.poll()                                   # pending overflow
        self.assertEqual(self.pages("PRINT"), 0)
        self.assertTrue(self.events_of("PRINT_OVERFLOW"))
        self.poll()                                   # Confirm → PRINT_GAP
        gaps = self.events_of("PRINT_GAP")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["details"]["pages"], 500)
        self.assertEqual(gaps[0]["details"].get("paper_split"), {"large": 100, "small": 400})
        self.assertEqual(self.pages("PRINT"), 0,
                         "صفحات gap نباید در آمار چاپ روزانه (type=PRINT) باد کنند")
        self.assert_no_fake_reset()

    def test_08b_reasonable_gap_is_normal_print_not_pending(self):
        # Failure of the monitor with "physical" printing (60 pages in a few hours) should be normal PRINT
        # Record — these pages were actually printed, and to omit them would be to lose the truth.
        """Verifies 08 b reasonable gap is normal print not pending."""
        self.poll()
        self.dev.print_job(small=60)
        self.poll()
        self.assertEqual(self.pages("PRINT"), 60)
        self.assertEqual(self.events_of("PRINT_OVERFLOW"), [])
        self.assertEqual(self.events_of("PRINT_GAP"), [])
        lg, sm = self.split_sums()
        self.assertEqual((lg, sm), (0, 60))

    def test_09_paper_read_failure_suppresses_label_honestly_then_recovers(self):
        """Verifies 09 paper read failure suppresses label honestly then recovers."""
        self.poll()
        self.dev.print_job(small=4, large=1)
        self.dev.fail_keys.add("a3_total")            # Reading large branch fail
        self.poll()
        last = self.events_of("PRINT")[-1]
        self.assertIsNone(last["details"].get("paper_size"),
                          "با خواندن ناقص نباید حدس زده شود")
        self.dev.print_job(small=4)                   # Next poll: Snapshot rotation error is absorbed
        self.poll()
        last = self.events_of("PRINT")[-1]
        self.assertEqual(last["details"].get("paper_size"), "Small (A4/A5)",
                         "با جذب خطای داخل تلورانس باید دقیقاً Small ثبت شود (نه Mixed)")
        self.assertEqual(last["details"].get("paper_split"), {"large": 0, "small": 4},
                         "paper_split هرگز نباید از pages رویداد بیشتر شود")
        self.dev.print_job(small=4)                   # Next poll: full recovery
        self.poll()
        last = self.events_of("PRINT")[-1]
        self.assertEqual(last["details"].get("paper_size"), "Small (A4/A5)")
        self.assert_page_integrity("paper-read-fail")

    def test_10_two_pass_job_counter_updates_sum_correctly(self):
        # 30-page job whose counter is updated in two polls (NVRAM delay)
        """Verifies 10 two pass job counter updates sum correctly."""
        self.poll()
        self.dev.print_job(small=18)
        self.poll()
        self.dev.print_job(small=12)
        self.poll()
        self.assertEqual(self.pages("PRINT"), 30)
        self.assertEqual(len(self.events_of("PRINT")), 2)

    def test_11_duplicate_poll_race_never_double_logs(self):
        # A double poll (race/retry with the same snapshot) should not create duplicate events
        """Verifies 11 duplicate poll race never double logs."""
        self.poll()
        self.dev.print_job(small=9)
        self.poll()
        n = len(recorded_events)
        # Run again with exactly the same snapshot (device not countered)
        self.dev.uptime += 0
        self.poll()
        self.assertEqual(len(recorded_events), n, "delta صفر نباید PRINT جدید بسازد")

    def test_12_cartridge_swap_logs_exactly_one_confirmed_refill(self):
        """Verifies 12 cartridge swap logs exactly one confirmed refill."""
        self.poll()
        self.dev.print_job(small=30)
        self.dev.toner = 30                            # Reaching the bottom of the cartridge with normal consumption
        # (By printing enough in this same poll until the noise guard of "drop>25% with <5 pages" it
        # Don't treat the reading as noise - the same bug that broke the old version of the test)
        self.poll()
        self.dev.toner = 55                            # Cartridge replacement: +25 jump without printing
        self.poll()
        self.assertEqual(self.events_of("REFILL"), [],
                         "با یک poll نباید REFILL ثبت شود (مرحله pending)")
        self.dev.toner = 54                            # First verification (±1 noise is allowed)
        self.poll()
        self.assertEqual(self.events_of("REFILL"), [],
                         "با فقط یک تأیید نباید REFILL ثبت شود (دو تأیید پشت‌سر لازم است)")
        self.dev.toner = 54                            # Second confirmation of the back → definitive registration
        self.poll()
        refills = self.events_of("REFILL")
        self.assertEqual(len(refills), 1, "REFILL باید فقط یک بار و بعد از دو تأیید پشت‌سر ثبت شود")
        self.assertTrue(refills[0]["details"].get("confirmed"))
        self.dev.print_job(small=5)
        self.poll()
        self.assert_page_integrity("refill")


if __name__ == "__main__":
    unittest.main()
