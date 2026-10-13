# -*- coding: utf-8 -*-
"""
تست‌های helper مرکزی زمان (فاز E.3 — کار E.3.1).

این فایل قرارداد فرمت استاندارد UTC را قفل می‌کند:
  ``2026-10-03T12:00:00.123456Z`` — همیشه UTC، همیشه ``Z``، همیشه ۶ رقم
  میکروثانیه، طول ثابت ۲۷ ⇒ مقایسه‌ی رشته‌ای امن.

هشت تست اصلی طبق دستور گام E.3.1 + دو تست سخت‌گیری افزوده (naive در ``parse_utc``
و ورودی نامعتبر) که همان بخشی از قراردادند که در readerها از خطاهای بی‌صدا جلوگیری
می‌کند.

افزوده‌ی E.3.7: هِلپرهای خواننده‌ها (`to_filter_bound`، `to_local_day`،
`to_sort_key`) هم تست دارند — این‌ها سه نقطه‌ای هستند که خواننده‌ها را به فرمت
استاندارد گره می‌زنند.
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

    def test_utc_now_iso_has_standard_format(self):
        ts = utc_now_iso()
        self.assertRegex(ts, _Z_RE)
        self.assertEqual(len(ts), ISO_Z_LENGTH)
        self.assertTrue(ts.endswith("Z"))
        # فرمت از قالب خود ماژول ساخته می‌شود (یک منبع، نه دو تا)
        self.assertEqual(datetime.now(timezone.utc).strftime(ISO_Z_FORMAT)[:19], ts[:19])

    def test_utc_now_iso_is_utc_not_local(self):
        """در TZ=Asia/Tehran هم باید UTC بدهد (نه ساعت محلی)."""
        ts = utc_now_iso()
        utc_hour = parse_utc(ts).astimezone(timezone.utc).hour
        self.assertEqual(utc_hour, datetime.now(timezone.utc).hour)


class ToUtcZTests(unittest.TestCase):

    def test_to_utc_z_on_aware(self):
        local = datetime(2026, 10, 3, 15, 30, tzinfo=TEHRAN)     # ۱۵:۳۰ تهران
        self.assertEqual(to_utc_z(local), "2026-10-03T12:00:00.000000Z")
        utc = datetime(2026, 10, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual(to_utc_z(utc), "2026-10-03T12:00:00.123456Z")

    def test_to_utc_z_on_naive_raises(self):
        with self.assertRaises(ValueError) as ctx:
            to_utc_z(datetime(2026, 10, 3, 12, 0, 0))
        self.assertIn("naive", str(ctx.exception))

    def test_to_utc_z_rejects_non_datetime(self):
        with self.assertRaises(ValueError):
            to_utc_z("2026-10-03T12:00:00Z")


class ParseUtcTests(unittest.TestCase):

    def test_parse_utc_with_z_suffix(self):
        dt = parse_utc("2026-10-03T12:00:00.123456Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.microsecond, 123456)
        self.assertEqual(dt.astimezone(timezone.utc).hour, 12)

    def test_parse_utc_with_explicit_utc_offset(self):
        self.assertEqual(parse_utc("2026-10-03T12:00:00+00:00"),
                         parse_utc("2026-10-03T12:00:00.000000Z"))

    def test_parse_utc_with_other_offset_converts(self):
        """+03:30 یک لحظه‌ی دیگر نیست؛ همان لحظه با نمایش دیگر است."""
        dt = parse_utc("2026-10-03T15:30:00+03:30")
        self.assertEqual(dt.astimezone(timezone.utc).strftime("%H:%M:%S"), "12:00:00")

    def test_parse_utc_rejects_naive_and_garbage(self):
        """سخت‌گیری عمدی: naive و رشته‌ی خراب باید خطای روشن بدهند، نه مقدار حدسی."""
        with self.assertRaises(ValueError) as ctx:
            parse_utc("2026-10-03T12:00:00")
        self.assertIn("naive", str(ctx.exception))
        for bad in ("", "   ", None, "not-a-time", "2026-13-45T99:99:99Z"):
            with self.assertRaises(ValueError, msg=bad):
                parse_utc(bad)


class RoundTripTests(unittest.TestCase):

    def test_round_trip_is_close_to_now(self):
        before = datetime.now(timezone.utc)
        dt = parse_utc(utc_now_iso())
        after = datetime.now(timezone.utc)
        self.assertEqual(dt.tzinfo, timezone.utc)
        # اختلاف مجاز: چند ثانیه (نه دقیقه — اگر ۳:۳۰ خطا باشد اینجا لو می‌رود)
        self.assertLess(abs((dt - before).total_seconds()), 5)
        self.assertLess(abs((after - dt).total_seconds()), 5)

    def test_round_trip_preserves_microseconds_and_order(self):
        t0 = datetime(2026, 10, 3, 12, 0, 0, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 10, 3, 12, 0, 0, 2, tzinfo=timezone.utc)
        self.assertEqual(parse_utc(to_utc_z(t0)), t0)
        self.assertLess(to_utc_z(t0), to_utc_z(t1))     # ترتیب با ترتیب رشته یکی است


class ToLocalStrTests(unittest.TestCase):

    def test_to_local_str_with_tehran(self):
        self.assertEqual(to_local_str("2026-10-03T12:00:00.000000Z"),
                         "2026-10-03 15:30:00")

    def test_to_local_str_default_is_tehran(self):
        self.assertEqual(DEFAULT_DISPLAY_TZ, "Asia/Tehran")
        self.assertEqual(to_local_str("2026-10-03T12:00:00Z"),
                         to_local_str("2026-10-03T12:00:00Z", "Asia/Tehran"))

    def test_to_local_str_propagates_parse_errors(self):
        with self.assertRaises(ValueError):
            to_local_str("2026-10-03T12:00:00")     # naive


class StringComparisonTests(unittest.TestCase):

    def test_string_comparison_matches_chronological_order(self):
        """قلب قرارداد: مقایسه‌ی رشته‌ای = مقایسه‌ی زمانی."""
        self.assertGreater(utc_now_iso(), "2020-01-01T00:00:00.000000Z")
        self.assertLess("2020-01-01T00:00:00.000000Z", utc_now_iso())

    def test_fixed_length_and_padding_keep_lexicographic_order(self):
        """صفر بودن میکروثانیه هم باید ۶ رقم بدهد، وگرنه ترتیب رشته‌ای می‌شکند."""
        a = to_utc_z(datetime(2026, 10, 3, 12, 0, 0, 0, tzinfo=timezone.utc))
        b = to_utc_z(datetime(2026, 10, 3, 12, 0, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(len(a), len(b))
        self.assertLess(a, b)
        # و مقایسه با یک لحظه‌ی دیگر در همان روز
        self.assertLess(a, to_utc_z(datetime(2026, 10, 3, 12, 0, 1, tzinfo=timezone.utc)))


class HelpersTests(unittest.TestCase):

    def test_as_utc_datetime_handles_none_and_values(self):
        self.assertIsNone(as_utc_datetime(None))
        self.assertIsNone(as_utc_datetime("   "))
        self.assertEqual(as_utc_datetime("2026-10-03T12:00:00Z"),
                         datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc))

    def test_local_tz_and_timezone_math(self):
        tz = local_tz()
        dt = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual((dt.hour, dt.minute), (15, 30))
        self.assertEqual(to_utc_z(datetime.now(timezone.utc) - timedelta(hours=1))[:10],
                         utc_now_iso()[:10])   # هر دو امروز-UTC (مگر در نیمه‌شب)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ToFilterBoundTests(unittest.TestCase):
    """مرزِ فیلتر بازه از ورودی کاربر (E.3.4) — ورودی انسانی، رفتار بخشنده و مستند."""

    def test_aware_inputs_are_normalized_to_canonical_z(self):
        self.assertEqual(to_filter_bound("2026-10-03T12:00:00.000000Z"),
                         "2026-10-03T12:00:00.000000Z")
        self.assertEqual(to_filter_bound("2026-10-03T15:30:00+03:30"),
                         "2026-10-03T12:00:00.000000Z")
        # idempotent: خروجی خودش ورودی معتبر است
        once = to_filter_bound("2026-10-03T12:00:00+00:00")
        self.assertEqual(to_filter_bound(once), once)

    def test_naive_and_date_only_are_read_as_local(self):
        """قاعدهٔ قرارداد §۸.۳: naive ⇒ ساعت محلی (Asia/Tehran) ⇒ UTC."""
        self.assertEqual(to_filter_bound("2026-10-03T15:30:00"),
                         "2026-10-03T12:00:00.000000Z")
        # فقط تاریخ ⇒ نیمه‌شبِ محلیِ همان روز
        self.assertEqual(to_filter_bound("2026-10-03"),
                         "2026-10-02T20:30:00.000000Z")

    def test_empty_and_none_pass_through_unchanged(self):
        self.assertIsNone(to_filter_bound(None))
        self.assertEqual("", to_filter_bound(""))
        self.assertEqual("   ", to_filter_bound("   "))

    def test_garbage_passes_through_with_warning(self):
        """رفتار تاریخی endpoint حفظ می‌شود: رشتهٔ نامعتبر استثنا نمی‌دهد."""
        with self.assertLogs("core.time_utils", level="WARNING") as ctx:
            self.assertEqual("not-a-time", to_filter_bound("not-a-time"))
        self.assertIn("not-a-time", " ".join(ctx.output))


class ToLocalDayTests(unittest.TestCase):
    """گروه‌بندی روزانه باید روزِ محلی بدهد، نه روزِ UTC (باگ ۳:۳۰ در سبد آمار)."""

    def test_utc_evening_belongs_to_next_local_day(self):
        # ۲۰:۳۰ UTC = ۰۰:۰۰ تهرانِ روز بعد
        self.assertEqual(to_local_day("2026-10-03T20:30:00.000000Z"), "2026-10-04")
        self.assertEqual(to_local_day("2026-10-03T20:29:59.000000Z"), "2026-10-03")

    def test_utc_morning_is_same_local_day(self):
        self.assertEqual(to_local_day("2026-10-03T12:00:00.000000Z"), "2026-10-03")

    def test_rejects_naive_like_parse_utc(self):
        with self.assertRaises(ValueError):
            to_local_day("2026-10-03T12:00:00")


class ToSortKeyTests(unittest.TestCase):

    def test_orders_mixed_formats_chronologically(self):
        values = ["2026-10-03T12:00:00.000000Z",
                  "2026-10-03T15:30:00+03:30",        # همان لحظه
                  "2026-10-03T12:00:01.000000Z"]
        ordered = sorted(values, key=to_sort_key)
        self.assertEqual(ordered[0], "2026-10-03T12:00:00.000000Z")
        self.assertEqual(ordered[-1], "2026-10-03T12:00:01.000000Z")

    def test_empty_and_garbage_sort_first_without_raising(self):
        values = ["2026-10-03T12:00:00.000000Z", "", None, "bad"]
        self.assertEqual(sorted(values, key=to_sort_key)[-1],
                         "2026-10-03T12:00:00.000000Z")
