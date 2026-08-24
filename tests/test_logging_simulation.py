# -*- coding: utf-8 -*-
"""
شبیه‌ساز سرتاسری «چاپگر مجازی» برای اثبات درستی لاگ‌ها.

ایده: به‌جای تکیه بر تست واحد، یک دستگاه Toshiba مجازی می‌سازیم که رفتار چند
هفته‌ای واقعی را پخش می‌کند (چاپ A4/A3/کپی/رنگی، خرابی SNMP، قطعی مانیتور،
ریبوت، فکتوری‌ریست، تعویض کارتریج، poll تکراری) و رویدادهای ثبت‌شده را با
حقیقت زمینی (Ground Truth) مقایسه می‌کنیم.

این تست از همان مسیر کد production عبور می‌کند:
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
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


class VirtualToshiba:
    """چاپگر مجازی با شمارنده‌های سازگار (a3_total + a4_total == print_total)."""

    def __init__(self, large=40_000, small=160_000, fc_from_large=0):
        self.a3 = large            # شاخه ۲۰۷: کل کاغذ بزرگ
        self.a4 = small            # شاخه ۲۰۸: کل کاغذ کوچک
        self.fc = 0                # مونو پیش‌فرض
        self.toner = 60
        self.uptime = 900_000_000  # sysUpTime (صدم ثانیه)
        self.fail_keys = set()     # کلیدهایی که این poll باید None برگردند (خرابی SNMP)
        self.uptime_fail = False
        # حقیقت زمینی
        self.truth = {"large": 0, "small": 0, "color": 0}

    @property
    def total(self):
        return self.a3 + self.a4

    @property
    def bw(self):
        return self.total - self.fc

    def print_job(self, large=0, small=0, color_pages=0):
        self.a3 += large
        self.a4 += small
        self.fc += color_pages
        self.truth["large"] += large
        self.truth["small"] += small
        self.truth["color"] += color_pages
        self.toner = max(5, self.toner)  # مصرف تونر در تست‌ها ثابت نگه داشته می‌شود

    def poll_uptime(self):
        if self.uptime_fail:
            return None
        return self.uptime

    def tick(self, seconds=60):
        self.uptime += seconds * 100


class SimBase(unittest.TestCase):
    IP = "192.168.1.99"

    def setUp(self):
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
        base_mod.add_event = self._orig_add_event
        ec.snmp_get_with_fallback = self._orig_snmp
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    # ── SNMP مجازی: فقط OIDهایی که _collect_toshiba_job_data می‌خواند ──
    def _snmp(self, ip, oid, community, **kw):
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
        # نگاشت معکوس OIDS[key] → key برای fail_keys
        key = next((k for k, v in OIDS.items() if v == oid), None)
        if key in d.fail_keys:
            return None
        return table.get(oid, 0)

    def poll(self):
        """دقیقاً معادل بلاک رویداد collect_enhanced روی دستگاه مجازی."""
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
        return [e for e in recorded_events if e["type"] == etype]

    def pages(self, etype="PRINT"):
        return sum((e["details"].get("pages") or 0) for e in self.events_of(etype))

    def split_sums(self):
        large = small = 0
        for e in recorded_events:
            s = e["details"].get("paper_split")
            if s:
                large += s.get("large") or 0
                small += s.get("small") or 0
        return large, small

    def unattributed_pages(self):
        return sum((e["details"].get("pages") or 0)
                   for e in self.events_of("PRINT") + self.events_of("PRINT_GAP")
                   if not e["details"].get("paper_split"))

    def assert_no_fake_reset(self):
        self.assertEqual(self.events_of("COUNTER_RESET"), [],
                         "COUNTER_RESET کاذب نباید ثبت شود")

    def assert_page_integrity(self, note=""):
        """جمع صفحات لاگ‌شده (PRINT برای چاپ آنلاین) باید دقیقاً برابر حقیقت باشد."""
        d = self.dev
        self.assertEqual(self.pages("PRINT"), d.truth["large"] + d.truth["small"],
                         f"pages mismatch {note}: logged={self.pages('PRINT')} "
                         f"truth={d.truth['large'] + d.truth['small']}")
        lg, sm = self.split_sums()
        attributed = lg + sm + self.unattributed_pages()
        self.assertEqual(attributed, d.truth["large"] + d.truth["small"],
                         "مجموع صفحات نسبت‌داده‌شده+نامشخص باید با حقیقت برابر باشد")


class LoggingSimulationTests(SimBase):

    def test_01_first_poll_baseline_no_fake_giant_print(self):
        # باگ مشهور قدیم: اولین poll نباید ۲۰۰هزار صفحه لاگ کند
        self.poll()
        self.assertEqual(recorded_events, [], "اولین poll باید فقط baseline بگیرد")
        self.dev.print_job(small=5)
        self.poll()
        self.assertEqual(self.pages("PRINT"), 5)

    def test_02_normal_office_week_exact_pages_and_labels(self):
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
        # اختلاط: رویدادهای Mixed باید تفکیک دقیق داشته باشند
        mixed = [e for e in self.events_of("PRINT")
                 if e["details"].get("paper_size") == "Mixed"]
        self.assertTrue(mixed, "حداقل چند رویداد Mixed انتظار داریم")
        for e in mixed:
            self.assertTrue(e["details"].get("paper_split"))

    def test_03_uptime_read_failure_never_triggers_counter_reset(self):
        # باگ تولیدی: ۲۷۶ COUNTER_RESET کاذب با current_uptime=0
        self.poll()
        for _ in range(30):
            self.dev.print_job(small=4)
            self.dev.uptime_fail = True     # uptime اصلاً خوانده نمی‌شود
            self.poll()
        self.assert_no_fake_reset()
        self.assertEqual(self.events_of("COUNTER_ANOMALY"), [])
        self.assert_page_integrity("uptime-fail")

    def test_04_vendor_total_failure_keeps_snapshot_no_spike(self):
        # باگ تولیدی: نوسان print_total (160,432 → 2,124 → 160,503)
        self.poll()
        self.dev.print_job(small=10)
        self.dev.fail_keys.add("print_total")      # OID vendor این poll خوانده نمی‌شود
        self.poll()
        self.assertEqual(self.pages("PRINT"), 0)
        self.assertTrue(self.events_of("SNMP_COUNTER_READ_ERROR"))
        self.dev.print_job(small=10)
        self.poll()                                 # ۲۰ صفحه در دو بازه، یک لاگ درست
        self.assertEqual(self.pages("PRINT"), 20)
        self.assertEqual(self.events_of("PRINT_OVERFLOW"), [])
        self.assert_no_fake_reset()

    def test_05_genuine_reboot_without_counter_reset_logs_normally(self):
        # ریبوت واقعی: uptime ریست می‌شود ولی شمارنده در NVRAM حفظ است
        self.poll()
        base = self.dev.uptime
        self.dev.print_job(small=6)
        self.dev.uptime = 2_000                     # دستگاه ریبوت شد
        self.poll()
        self.assert_no_fake_reset()
        self.assertEqual(self.events_of("COUNTER_ANOMALY"), [])
        self.assert_page_integrity("reboot")
        # ادامه چاپ بعد از ریبوت هم دقیق است
        self.dev.print_job(small=6)
        self.poll()
        self.assert_page_integrity("after-reboot")

    def test_06_mild_counter_drop_with_reboot_is_anomaly_not_reset(self):
        # کاهش جزئی + ریبوت بدون شواهد ریست واقعی → COUNTER_ANOMALY + حفظ snapshot
        self.poll()
        self.dev.print_job(small=1)
        self.poll()
        # دستگاه ریبوت شد و شمارنده کمی عقب افتاد (منابع SNMP ناپایدار)
        self.dev.a4 -= 5
        self.dev.uptime = 3_000
        self.poll()
        self.assert_no_fake_reset()
        self.assertEqual(len(self.events_of("COUNTER_ANOMALY")), 1)
        # snapshot حفظ شده؛ ادامه چاپ بدون دوبارشماری.
        # نکته‌ی فیزیکی (نه باگ): ۵ صفحه‌ای که خودِ شمارنده‌ی دستگاه «فراموش»
        # کرده قابل لاگ نیست — آن‌ها از هیچ منبعی قابل بازیابی نیستند. سیستم به‌جای
        # لاگ دروغین، COUNTER_ANOMALY شفاف ثبت کرده تا گمشدگی حسابرسی‌پذیر باشد.
        self.dev.print_job(small=8)
        self.poll()
        truth = self.dev.truth["large"] + self.dev.truth["small"]
        self.assertEqual(self.pages("PRINT"), truth - 5,
                         "تنها صفحاتِ گمشده باید همان ۵ صفحه‌ای باشند که خود شمارنده دستگاه از دست داد")
        self.assertLessEqual(self.pages("PRINT"), truth,
                             "لاگ هرگز نباید بیشتر از واقعیت باد شود")

    def test_07_factory_reset_logs_exactly_one_counter_reset(self):
        self.poll()
        self.dev.print_job(small=10)
        self.poll()
        before = self.pages("PRINT")
        # فکتوری‌ریست: شمارنده صفر + uptime ریست
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
        # جهش غیرفیزیکی شمارنده (همان spikeهای +۱۲۳هزار صفحه‌ای تولید: منبع
        # شمارنده نوسان کرد) → pending و سپس PRINT_GAP، نه PRINT بادکرده.
        self.poll()
        self.dev.print_job(small=400, large=100)      # جهش ۵۰۰ تایی در یک بازه
        self.poll()                                   # pending overflow
        self.assertEqual(self.pages("PRINT"), 0)
        self.assertTrue(self.events_of("PRINT_OVERFLOW"))
        self.poll()                                   # تأیید → PRINT_GAP
        gaps = self.events_of("PRINT_GAP")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["details"]["pages"], 500)
        self.assertEqual(gaps[0]["details"].get("paper_split"), {"large": 100, "small": 400})
        self.assertEqual(self.pages("PRINT"), 0,
                         "صفحات gap نباید در آمار چاپ روزانه (type=PRINT) باد کنند")
        self.assert_no_fake_reset()

    def test_08b_reasonable_gap_is_normal_print_not_pending(self):
        # قطعی مانیتور با چاپ «فیزیکی» (۶۰ صفحه در چند ساعت) باید PRINT عادی
        # ثبت شود — این صفحات واقعاً چاپ شده‌اند و حذفشان یعنی گم‌کردن حقیقت.
        self.poll()
        self.dev.print_job(small=60)
        self.poll()
        self.assertEqual(self.pages("PRINT"), 60)
        self.assertEqual(self.events_of("PRINT_OVERFLOW"), [])
        self.assertEqual(self.events_of("PRINT_GAP"), [])
        lg, sm = self.split_sums()
        self.assertEqual((lg, sm), (0, 60))

    def test_09_paper_read_failure_suppresses_label_honestly_then_recovers(self):
        self.poll()
        self.dev.print_job(small=4, large=1)
        self.dev.fail_keys.add("a3_total")            # خواندن شاخه بزرگ fail
        self.poll()
        last = self.events_of("PRINT")[-1]
        self.assertIsNone(last["details"].get("paper_size"),
                          "با خواندن ناقص نباید حدس زده شود")
        self.dev.print_job(small=4)                   # poll بعد: خطای گردش snapshot جذب می‌شود
        self.poll()
        last = self.events_of("PRINT")[-1]
        self.assertEqual(last["details"].get("paper_size"), "Small (A4/A5)",
                         "با جذب خطای داخل تلورانس باید دقیقاً Small ثبت شود (نه Mixed)")
        self.assertEqual(last["details"].get("paper_split"), {"large": 0, "small": 4},
                         "paper_split هرگز نباید از pages رویداد بیشتر شود")
        self.dev.print_job(small=4)                   # poll بعد: بازیابی کامل
        self.poll()
        last = self.events_of("PRINT")[-1]
        self.assertEqual(last["details"].get("paper_size"), "Small (A4/A5)")
        self.assert_page_integrity("paper-read-fail")

    def test_10_two_pass_job_counter_updates_sum_correctly(self):
        # job ۳۰صفحه‌ای که شمارنده‌اش در دو poll آپدیت می‌شود (تأخیر NVRAM)
        self.poll()
        self.dev.print_job(small=18)
        self.poll()
        self.dev.print_job(small=12)
        self.poll()
        self.assertEqual(self.pages("PRINT"), 30)
        self.assertEqual(len(self.events_of("PRINT")), 2)

    def test_11_duplicate_poll_race_never_double_logs(self):
        # poll دوبل (race/retry با همان snapshot) نباید رویداد تکراری بسازد
        self.poll()
        self.dev.print_job(small=9)
        self.poll()
        n = len(recorded_events)
        # اجرای دوباره با دقیقاً همان snapshot (دستگاه شمارنده نزده)
        self.dev.uptime += 0
        self.poll()
        self.assertEqual(len(recorded_events), n, "delta صفر نباید PRINT جدید بسازد")

    def test_12_cartridge_swap_logs_exactly_one_confirmed_refill(self):
        self.poll()
        self.dev.print_job(small=30)
        self.dev.toner = 30                            # رسیدن به تهِ کارتریج با مصرف عادی
        # (با چاپِ کافی در همین poll تا گاردِ ضدنویزِ «افت>۲۵٪ با <۵ صفحه» آن را
        #  خوانش نویزی تلقی نکند — همان باگی که نسخه‌ی قدیمی تست را می‌شکست)
        self.poll()
        self.dev.toner = 55                            # تعویض کارتریج: پرش +۲۵ بدون چاپ
        self.poll()
        self.assertEqual(self.events_of("REFILL"), [],
                         "با یک poll نباید REFILL ثبت شود (مرحله pending)")
        self.dev.toner = 54                            # تأیید اول (نویز ±۱ مجاز است)
        self.poll()
        self.assertEqual(self.events_of("REFILL"), [],
                         "با فقط یک تأیید نباید REFILL ثبت شود (دو تأیید پشت‌سر لازم است)")
        self.dev.toner = 54                            # تأیید دومِ پشت‌سر → ثبت قطعی
        self.poll()
        refills = self.events_of("REFILL")
        self.assertEqual(len(refills), 1, "REFILL باید فقط یک بار و بعد از دو تأیید پشت‌سر ثبت شود")
        self.assertTrue(refills[0]["details"].get("confirmed"))
        self.dev.print_job(small=5)
        self.poll()
        self.assert_page_integrity("refill")


if __name__ == "__main__":
    unittest.main()
