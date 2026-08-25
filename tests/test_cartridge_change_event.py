# -*- coding: utf-8 -*-
"""
تست‌های قابلیت «رویداد خودکار تعویض/شارژ کارتریج» (CARTRIDGE_CHANGED).

الگوی تشخیص عیناً همان الگوی اثبات‌شده‌ی REFILL تک‌رنگ (کاندید در جهش >۲۰٪ با
صفحات کم → پایداری سطح در ۲ poll متوالی → رویداد یک‌شوت) است، اما per-color و
فقط روی داده‌ی واقعی (raw_level) تا تعویض کارتریج‌های رنگی (C/M/Y) هم لاگ شود
و برآوردهای نمایشی (canon_status_code و …) رویداد جعلی نسازند.

قواعد کلیدی تست‌شده:
  ۱) جهش ۳۰→۹۰ سیان: اول کاندید، بعد از ۲ poll پایدار → دقیقاً یک رویداد.
  ۲) نوسان (۳۰→۹۰→۳۰): هیچ رویدادی.
  ۳) جهش همراه با چاپ زیاد (delta_pages >= 50): کاندید نمی‌شود (رفتار لگاسی).
  ۴) رنگِ لگاسی (black) همان مسیر REFILL را دارد → بدون رویداد تکراری.
  ۵) رنگ‌های بدون داده‌ی واقعی و کلیدهای غیر CMYK (drum): هیچ رویدادی.
  ۶) manual_override: تشخیص کاملاً خاموش.
  ۷) رویداد فقط یک‌بار ثبت می‌شود (one-shot).
  ۸) برگشت سطح به زیر کاندید، pending را می‌باطلد و چرخه‌ی تازه hits را صفر می‌کند.
"""
import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod

recorded_events = []


def _fake_add_event(ip, etype, details):
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class CartridgeChangeEventTests(unittest.TestCase):
    IP = "10.9.9.9"
    UP = 600_000_000  # uptime بالا (صدم ثانیه) تا reset detection دخالت نکند

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        recorded_events.clear()
        self._orig_add_event = base_mod.add_event
        base_mod.add_event = _fake_add_event

    def tearDown(self):
        base_mod.add_event = self._orig_add_event
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ─── helpers ───
    def _events(self, etype):
        return [e for e in recorded_events if e["type"] == etype]

    def _seed(self, total=1000, manual_override=0):
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
        """یک poll واقعی: prev از store خوانده می‌شود (مثل مسیر enhanced)."""
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

    # ─── ۱) سناریوی اصلی: تعویض کارتریج رنگی ───
    def test_color_cartridge_change_logged_once_after_two_stable_polls(self):
        self._seed(total=1000)
        # baseline: سطح واقعی سیان 30٪
        self._poll(1000, {"cyan": 30})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        # جهش به 90٪ (تعویض) → فقط کاندید، هنوز رویداد نه
        self._poll(1002, {"cyan": 90})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        # poll پایدار اول (hits=1)
        self._poll(1004, {"cyan": 90})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        # poll پایدار دوم (hits=2) → رویداد تأییدشده
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
        # یک‌شوت: pollهای بعدی رویداد تکراری نسازند
        self._poll(1008, {"cyan": 90})
        self._poll(1010, {"cyan": 88})
        self.assertEqual(len(self._events("CARTRIDGE_CHANGED")), 1)

    # ─── ۲) نوسان/بليپ: هیچ رویدادی ───
    def test_flapping_level_never_confirms(self):
        self._seed(total=1000)
        self._poll(1000, {"magenta": 30})
        self._poll(1001, {"magenta": 90})   # کاندید
        self._poll(1002, {"magenta": 30})   # برگشت → کاندید باطل
        self._poll(1003, {"magenta": 90})   # کاندید تازه
        self._poll(1004, {"magenta": 30})   # باطل
        self._poll(1005, {"magenta": 88})   # جهش از 30 → کاندید تازه
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── ۳) جهش همراه چاپ زیاد: کاندید نمی‌شود (رفتار لگاسی) ───
    def test_no_candidate_when_many_pages_printed(self):
        self._seed(total=1000)
        self._poll(1000, {"yellow": 30})
        # ۶۰ صفحه چاپ + جهش سطح → پنجره‌ی <۵۰ رد می‌شود
        self._poll(1060, {"yellow": 95})
        self._poll(1062, {"yellow": 95})
        self._poll(1064, {"yellow": 95})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── ۴) رنگ لگاسی (black): مسیر REFILL جداست؛ بدون تکرار ───
    def test_legacy_color_not_double_logged(self):
        self._seed(total=1000)
        # مشکی به current_toner_level وصل است (legacy REFILL)، سیان ثابت
        self._poll(1000, {"black": 40, "cyan": 60}, current_toner=40)
        self._poll(1001, {"black": 95, "cyan": 60}, current_toner=95)
        self._poll(1002, {"black": 95, "cyan": 60}, current_toner=95)
        self._poll(1003, {"black": 95, "cyan": 60}, current_toner=95)
        # برای مشکی: REFILL لگاسی تأیید می‌شود ولی CARTRIDGE_CHANGED هرگز نه
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        refills = self._events("REFILL")
        self.assertEqual(len(refills), 1)
        self.assertTrue(refills[0]["details"].get("confirmed"))

    # ─── ۵) فقط داده‌ی واقعی: رنگ بدون raw و کلیدهای غیر CMYK نادیده‌اند ───
    def test_only_real_levels_and_cmyk_keys_are_considered(self):
        self._seed(total=1000)
        # «drum» یک کارتریج تونر نیست؛ و رنگی که در toner_levels نیست (مثل
        # تخمین canon_status_code که raw_level ندارد) اصلاً وارد نمی‌شود.
        self._poll(1000, {"drum": 30, "cyan": 50})
        self._poll(1001, {"drum": 95, "cyan": 50})
        self._poll(1002, {"drum": 95})
        self._poll(1003, {"drum": 95})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── ۶) manual_override: تشخیص خاموش ───
    def test_manual_override_disables_detection(self):
        self._seed(total=1000, manual_override=1)
        self._poll(1000, {"cyan": 30})
        self._poll(1001, {"cyan": 95})
        self._poll(1002, {"cyan": 95})
        self._poll(1003, {"cyan": 95})
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])

    # ─── ۷) باطل‌شدن pending و شروع چرخه‌ی تازه با hits صفر ───
    def test_pending_resets_after_level_fall(self):
        self._seed(total=1000)
        self._poll(1000, {"cyan": 30})
        self._poll(1001, {"cyan": 90})   # کاندید
        self._poll(1002, {"cyan": 90})   # hits=1
        self._poll(1003, {"cyan": 35})   # سقوط → کاندید باطل
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        self._poll(1004, {"cyan": 90})   # کاندید تازه (hits=0)
        self._poll(1005, {"cyan": 90})   # hits=1 → هنوز رویداد نه
        self.assertEqual(self._events("CARTRIDGE_CHANGED"), [])
        self._poll(1006, {"cyan": 90})   # hits=2 → رویداد
        evs = self._events("CARTRIDGE_CHANGED")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["details"].get("prev_toner"), 35)

    # ─── ۸) تعویض چند رنگ هم‌زمان: هرکدام یک رویداد ───
    def test_multiple_colors_each_logged_once(self):
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
