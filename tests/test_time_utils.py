# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_time_utils.py
# @brief:        Tests for the central time helper (phase E.3).
# @details:      Responsibilities: Check UTC formatting, parsing and round trips.
#                Main API: UtcNowIsoTests, ToUtcZTests, ParseUtcTests, RoundTripTests,
#                ToLocalStrTests, StringComparisonTests
#                Side effects: none at import time
# @dependencies: internal: core.time_utils
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Time-centered helper tests (Phase E.3 — Task E.3.1).

This file locks the standard UTC format convention:
  ``2026-10-03T12:00:00.123456Z'' — always UTC, always ``Z'', always 6 digits
  Microseconds, fixed length 27 ⇒ safe string comparison.

Eight main tests according to step E.3.1 + two additional tightening tests (naive in ``parse_utc''
and invalid input) which are the same part of the contract that prevents silent errors in readers
does

Added E.3.7: Reader helpers (`to_filter_bound', `to_local_day',
`to_sort_key') also have tests — these are three points that direct readers to the format
They tie the standard.
"""
import re
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.time_utils import (
    DEFAULT_DISPLAY_TZ,
    ISO_Z_FORMAT,
    ISO_Z_LENGTH,
    as_utc_datetime,
    local_tz,
    parse_utc,
    to_filter_bound,
    to_local_day,
    to_local_str,
    to_sort_key,
    to_utc_z,
    utc_now_iso,
)

_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
TEHRAN = ZoneInfo("Asia/Tehran")


class UtcNowIsoTests(unittest.TestCase):

    """Tests for utc now iso."""
    def test_utc_now_iso_has_standard_format(self):
        """Verifies utc now iso has standard format."""
        ts = utc_now_iso()
        self.assertRegex(ts, _Z_RE)
        self.assertEqual(len(ts), ISO_Z_LENGTH)
        self.assertTrue(ts.endswith("Z"))
        # The format is built from the module's own format (one source, not two).
        self.assertEqual(datetime.now(timezone.utc).strftime(ISO_Z_FORMAT)[:19], ts[:19])

    def test_utc_now_iso_is_utc_not_local(self):
        """In TZ=Asia/Tehran it should also give UTC (not local time)."""
        ts = utc_now_iso()
        utc_hour = parse_utc(ts).astimezone(timezone.utc).hour
        self.assertEqual(utc_hour, datetime.now(timezone.utc).hour)


class ToUtcZTests(unittest.TestCase):

    """Tests for to utc z."""
    def test_to_utc_z_on_aware(self):
        """Verifies to utc z on aware."""
        local = datetime(2026, 10, 3, 15, 30, tzinfo=TEHRAN)     # 15:30 Tehran
        self.assertEqual(to_utc_z(local), "2026-10-03T12:00:00.000000Z")
        utc = datetime(2026, 10, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual(to_utc_z(utc), "2026-10-03T12:00:00.123456Z")

    def test_to_utc_z_on_naive_raises(self):
        """Verifies to utc z on naive raises."""
        with self.assertRaises(ValueError) as ctx:
            to_utc_z(datetime(2026, 10, 3, 12, 0, 0))
        self.assertIn("naive", str(ctx.exception))

    def test_to_utc_z_rejects_non_datetime(self):
        """Verifies to utc z rejects non datetime."""
        with self.assertRaises(ValueError):
            to_utc_z("2026-10-03T12:00:00Z")


class ParseUtcTests(unittest.TestCase):

    """Tests for parse utc."""
    def test_parse_utc_with_z_suffix(self):
        """Verifies parse utc with z suffix."""
        dt = parse_utc("2026-10-03T12:00:00.123456Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.microsecond, 123456)
        self.assertEqual(dt.astimezone(timezone.utc).hour, 12)

    def test_parse_utc_with_explicit_utc_offset(self):
        """Verifies parse utc with explicit utc offset."""
        self.assertEqual(parse_utc("2026-10-03T12:00:00+00:00"),
                         parse_utc("2026-10-03T12:00:00.000000Z"))

    def test_parse_utc_with_other_offset_converts(self):
        """+03:30 not another moment; It is the same moment with another show."""
        dt = parse_utc("2026-10-03T15:30:00+03:30")
        self.assertEqual(dt.astimezone(timezone.utc).strftime("%H:%M:%S"), "12:00:00")

    def test_parse_utc_rejects_naive_and_garbage(self):
        """Deliberate strictness: naive and broken strings should return an explicit error, not a guess value."""
        with self.assertRaises(ValueError) as ctx:
            parse_utc("2026-10-03T12:00:00")
        self.assertIn("naive", str(ctx.exception))
        for bad in ("", "   ", None, "not-a-time", "2026-13-45T99:99:99Z"):
            with self.assertRaises(ValueError, msg=bad):
                parse_utc(bad)


class RoundTripTests(unittest.TestCase):

    """Tests for round trip."""
    def test_round_trip_is_close_to_now(self):
        """Verifies round trip is close to now."""
        before = datetime.now(timezone.utc)
        dt = parse_utc(utc_now_iso())
        after = datetime.now(timezone.utc)
        self.assertEqual(dt.tzinfo, timezone.utc)
        # Allowed difference: seconds (not minutes — if 3:30 is an error, it will be exposed here)
        self.assertLess(abs((dt - before).total_seconds()), 5)
        self.assertLess(abs((after - dt).total_seconds()), 5)

    def test_round_trip_preserves_microseconds_and_order(self):
        """Verifies round trip preserves microseconds and order."""
        t0 = datetime(2026, 10, 3, 12, 0, 0, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 10, 3, 12, 0, 0, 2, tzinfo=timezone.utc)
        self.assertEqual(parse_utc(to_utc_z(t0)), t0)
        self.assertLess(to_utc_z(t0), to_utc_z(t1))     # The order is the same as the string order


class ToLocalStrTests(unittest.TestCase):

    """Tests for to local str."""
    def test_to_local_str_with_tehran(self):
        """Verifies to local str with tehran."""
        self.assertEqual(to_local_str("2026-10-03T12:00:00.000000Z"),
                         "2026-10-03 15:30:00")

    def test_to_local_str_default_is_tehran(self):
        """Verifies to local str default is tehran."""
        self.assertEqual(DEFAULT_DISPLAY_TZ, "Asia/Tehran")
        self.assertEqual(to_local_str("2026-10-03T12:00:00Z"),
                         to_local_str("2026-10-03T12:00:00Z", "Asia/Tehran"))

    def test_to_local_str_propagates_parse_errors(self):
        """Verifies to local str propagates parse errors."""
        with self.assertRaises(ValueError):
            to_local_str("2026-10-03T12:00:00")     # naive


class StringComparisonTests(unittest.TestCase):

    """Tests for string comparison."""
    def test_string_comparison_matches_chronological_order(self):
        """The heart of the contract: string comparison = temporal comparison."""
        self.assertGreater(utc_now_iso(), "2020-01-01T00:00:00.000000Z")
        self.assertLess("2020-01-01T00:00:00.000000Z", utc_now_iso())

    def test_fixed_length_and_padding_keep_lexicographic_order(self):
        """Being zero microsecond should also give 6 digits, otherwise the string order will break."""
        a = to_utc_z(datetime(2026, 10, 3, 12, 0, 0, 0, tzinfo=timezone.utc))
        b = to_utc_z(datetime(2026, 10, 3, 12, 0, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(len(a), len(b))
        self.assertLess(a, b)
        # And comparing with another moment on the same day
        self.assertLess(a, to_utc_z(datetime(2026, 10, 3, 12, 0, 1, tzinfo=timezone.utc)))


class HelpersTests(unittest.TestCase):

    """Tests for helpers."""
    def test_as_utc_datetime_handles_none_and_values(self):
        """Verifies as utc datetime handles none and values."""
        self.assertIsNone(as_utc_datetime(None))
        self.assertIsNone(as_utc_datetime("   "))
        self.assertEqual(as_utc_datetime("2026-10-03T12:00:00Z"),
                         datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc))

    def test_local_tz_and_timezone_math(self):
        """Verifies local tz and timezone math."""
        tz = local_tz()
        dt = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual((dt.hour, dt.minute), (15, 30))
        self.assertEqual(to_utc_z(datetime.now(timezone.utc) - timedelta(hours=1))[:10],
                         utc_now_iso()[:10])   # Both today-UTC (except midnight)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ToFilterBoundTests(unittest.TestCase):
    """Interval filter boundary from user input (E.3.4) — human input, forgiving and documented behavior."""

    def test_aware_inputs_are_normalized_to_canonical_z(self):
        """Verifies aware inputs are normalized to canonical z."""
        self.assertEqual(to_filter_bound("2026-10-03T12:00:00.000000Z"),
                         "2026-10-03T12:00:00.000000Z")
        self.assertEqual(to_filter_bound("2026-10-03T15:30:00+03:30"),
                         "2026-10-03T12:00:00.000000Z")
        # idempotent: The output itself is a valid input
        once = to_filter_bound("2026-10-03T12:00:00+00:00")
        self.assertEqual(to_filter_bound(once), once)

    def test_naive_and_date_only_are_read_as_local(self):
        """Contract rule §8.3: naive ⇒ local time (Asia/Tehran) ⇒ UTC."""
        self.assertEqual(to_filter_bound("2026-10-03T15:30:00"),
                         "2026-10-03T12:00:00.000000Z")
        # Date only ⇒ local midnight of the same day
        self.assertEqual(to_filter_bound("2026-10-03"),
                         "2026-10-02T20:30:00.000000Z")

    def test_empty_and_none_pass_through_unchanged(self):
        """Verifies empty and none pass through unchanged."""
        self.assertIsNone(to_filter_bound(None))
        self.assertEqual("", to_filter_bound(""))
        self.assertEqual("   ", to_filter_bound("   "))

    def test_garbage_passes_through_with_warning(self):
        """Endpoint historical behavior is preserved: invalid string does not throw exception."""
        with self.assertLogs("core.time_utils", level="WARNING") as ctx:
            self.assertEqual("not-a-time", to_filter_bound("not-a-time"))
        self.assertIn("not-a-time", " ".join(ctx.output))


class ToLocalDayTests(unittest.TestCase):
    """Daily grouping should give local day, not UTC day (bug 3:30 in statistics basket)."""

    def test_utc_evening_belongs_to_next_local_day(self):
        # 20:30 UTC = 00:00 Tehran the next day
        """Verifies utc evening belongs to next local day."""
        self.assertEqual(to_local_day("2026-10-03T20:30:00.000000Z"), "2026-10-04")
        self.assertEqual(to_local_day("2026-10-03T20:29:59.000000Z"), "2026-10-03")

    def test_utc_morning_is_same_local_day(self):
        """Verifies utc morning is same local day."""
        self.assertEqual(to_local_day("2026-10-03T12:00:00.000000Z"), "2026-10-03")

    def test_rejects_naive_like_parse_utc(self):
        """Verifies rejects naive like parse utc."""
        with self.assertRaises(ValueError):
            to_local_day("2026-10-03T12:00:00")


class ToSortKeyTests(unittest.TestCase):

    """Tests for to sort key."""
    def test_orders_mixed_formats_chronologically(self):
        """Verifies orders mixed formats chronologically."""
        values = ["2026-10-03T12:00:00.000000Z",
                  "2026-10-03T15:30:00+03:30",        # that moment
                  "2026-10-03T12:00:01.000000Z"]
        ordered = sorted(values, key=to_sort_key)
        self.assertEqual(ordered[0], "2026-10-03T12:00:00.000000Z")
        self.assertEqual(ordered[-1], "2026-10-03T12:00:01.000000Z")

    def test_empty_and_garbage_sort_first_without_raising(self):
        """Verifies empty and garbage sort first without raising."""
        values = ["2026-10-03T12:00:00.000000Z", "", None, "bad"]
        self.assertEqual(sorted(values, key=to_sort_key)[-1],
                         "2026-10-03T12:00:00.000000Z")
