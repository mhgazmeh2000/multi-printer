# -*- coding: utf-8 -*-
"""
تست‌های تشخیص تعویض کارتریج بر اساس «شناسه‌ی یکتا» (Chip ID / سریال / ریست
شمارنده‌ی صفحات‌باکارتریج) — سیگنال قطعیِ رویداد CARTRIDGE_CHANGED.

قواعد کلیدی:
  ۱) تغییر شناسه‌ی تراشه ⇒ رویداد همان poll (بدون انتظار تأیید ۲poll)، یک‌شوت.
  ۲) اولین مشاهده‌ی شناسه ⇒ فقط baseline (رویداد نه).
  ۳) خواندن ناموفق (کلید نبود) مقدار قبلی را پاک نمی‌کند.
  ۴) رویداد شناسه‌محور، pendingهای مسیر جهش سطح را پاک می‌کند (بدون رویداد دوم).
  ۵) ریست «صفحات با کارتریج» (۳۲۰۰۰→۱۵) ⇒ رویداد supply_pages_reset.
  ۶) manual_override همه‌ی تشخیص‌ها را خاموش می‌کند.
  ۷) شناسه‌ها در دیتابیس persist می‌شوند (تابع‌آمایی در برابر ری‌استارت).
بخش دوم: واحدهای خواننده (normalize/config SNMP/HP web/cache TTL).
"""
import json
import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod
import core.collectors.cartridge_id as cid_mod

recorded_events = []


def _fake_add_event(ip, etype, details):
    recorded_events.append({"ip": ip, "type": etype, "details": dict(details or {})})


# ═══════════════════════════════════════════════════════════════════════════
# موتور رویداد (ادغام با _counters_event)
# ═══════════════════════════════════════════════════════════════════════════
class CartridgeIdentityEngineTests(unittest.TestCase):
    IP = "10.9.9.10"
    UP = 800_000_000

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

    def _events(self, etype="CARTRIDGE_CHANGED"):
        return [e for e in recorded_events if e["type"] == etype]

    def _seed(self, total=1000, manual_override=0):
        store._prev.set(self.IP, {
            "print_total": total, "full_color": 0, "black_white": total,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": manual_override,
        })
        recorded_events.clear()

    def _poll(self, total, toner_levels=None, legacy_color="black", current_toner=40,
              ids=None, pages=None, uptime=None):
        prev = store._prev.get(self.IP) or {}
        base_mod._counters_event(
            self.IP, total, prev, [], [],
            full_color=0, black_white=total,
            current_toner_level=current_toner,
            prev_toner_level=prev.get("toner_level"),
            toner_levels=toner_levels,
            legacy_toner_color=legacy_color,
            cartridge_ids=ids,
            cartridge_supply_pages=pages,
            uptime=uptime if uptime is not None else (self.UP + total * 100),
        )

    # ─── ۱) تغییر ChipID ⇒ رویداد فوری (بدون ۲poll) ───
    def test_chip_id_change_fires_immediately_once(self):
        self._seed()
        self._poll(1000, ids={"black": "AAA-111"})
        self.assertEqual(self._events(), [])          # اولین مشاهده = baseline
        self._poll(1001, ids={"black": "BBB-222"})    # تعویض → رویداد همان poll
        evs = self._events()
        self.assertEqual(len(evs), 1)
        d = evs[0]["details"]
        self.assertEqual(d.get("detection"), "chip_id")
        self.assertEqual(d.get("prev_cartridge_id"), "AAA-111")
        self.assertEqual(d.get("cartridge_id"), "BBB-222")
        self.assertEqual(d.get("color"), "black")
        # یک‌شوت: pollهای بعدی با همان شناسه، رویدادی نیست
        self._poll(1002, ids={"black": "BBB-222"})
        self._poll(1003, ids={"black": "BBB-222"})
        self.assertEqual(len(self._events()), 1)

    # ─── ۳) خواندن ناموفق مقدار قبلی را حفظ می‌کند ───
    def test_missing_read_keeps_previous_identity(self):
        self._seed()
        self._poll(1000, ids={"black": "AAA-111"})
        self._poll(1001, ids={})                       # خواندن fail → حفظ قبلی
        self.assertEqual(self._events(), [])
        self._poll(1002, ids={"black": "AAA-111"})     # همان کارتریج → رویداد نه
        self.assertEqual(self._events(), [])
        self._poll(1003, ids={"black": "ZZZ-999"})     # تعویض واقعی → رویداد
        self.assertEqual(len(self._events()), 1)

    # ─── ۴) رنگ لگاسی: تعویض شناسه‌محور، REFILL آتی را حذف می‌کند ───
    def test_identity_change_suppresses_refill_and_level_paths(self):
        self._seed()
        # baseline: تونر ۴۰٪ + شناسه‌ی قدیم، سیان پایدار
        self._poll(1000, toner_levels={"cyan": 30}, ids={"black": "AAA-111"},
                   current_toner=40)
        # تعویض: سطح ۴۰→۹۵ (کاندید REFILL لگاسیِ معمولی) + شناسه عوض شد
        self._poll(1001, toner_levels={"cyan": 30}, ids={"black": "BBB-222"},
                   current_toner=95)
        # pollهای بعدی: سطح پایدار — REFILL هرگز نباید تأیید شود
        self._poll(1002, toner_levels={"cyan": 30}, ids={"black": "BBB-222"},
                   current_toner=95)
        self._poll(1003, toner_levels={"cyan": 30}, ids={"black": "BBB-222"},
                   current_toner=95)
        self.assertEqual(self._events("REFILL"), [])
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["details"].get("detection"), "chip_id")

    # ─── ۴ب) کاندید سطحِ در جریان هم پاک می‌شود ───
    def test_pending_level_candidate_cleared_by_identity_event(self):
        self._seed()
        self._poll(1000, toner_levels={"cyan": 30}, ids={"cyan": "C-OLD"})
        self._poll(1001, toner_levels={"cyan": 90}, ids={"cyan": "C-OLD"})   # کاندید
        self._poll(1002, toner_levels={"cyan": 90}, ids={"cyan": "C-NEW"})   # شناسه عوض شد
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["details"].get("detection"), "chip_id")
        # تأییدِ دوپالیِ مسیر سطح نباید رویداد دوم بسازد
        self._poll(1003, toner_levels={"cyan": 90}, ids={"cyan": "C-NEW"})
        self._poll(1004, toner_levels={"cyan": 90}, ids={"cyan": "C-NEW"})
        self.assertEqual(len(self._events()), 1)

    # ─── ۵) ریست «صفحات با کارتریج» ───
    def test_supply_pages_reset_detection(self):
        self._seed()
        self._poll(1000, pages={"black": 32000})
        self.assertEqual(self._events(), [])
        self._poll(1002, pages={"black": 32150})       # صعودی عادی → رویداد نه
        self.assertEqual(self._events(), [])
        self._poll(1004, pages={"black": 15})          # ریست → تعویض
        evs = self._events()
        self.assertEqual(len(evs), 1)
        d = evs[0]["details"]
        self.assertEqual(d.get("detection"), "supply_pages_reset")
        self.assertEqual(d.get("prev_supply_pages"), 32150)
        self.assertEqual(d.get("supply_pages"), 15)

    def test_supply_pages_small_drop_or_growth_no_event(self):
        self._seed()
        self._poll(1000, pages={"black": 100})
        self._poll(1001, pages={"black": 101})
        self._poll(1002, pages={"black": 60})          # افتِ نه‌چندان عمیق (< حد) → نه
        self.assertEqual(self._events(), [])

    # ─── ۶) manual_override ───
    def test_manual_override_disables_identity_detection(self):
        self._seed(manual_override=1)
        self._poll(1000, ids={"black": "AAA-111"}, pages={"black": 5000})
        self._poll(1001, ids={"black": "BBB-222"}, pages={"black": 5})
        self._poll(1002, ids={"black": "BBB-222"}, pages={"black": 6})
        self.assertEqual(self._events(), [])

    # ─── ۷) پایداری در برابر ری‌استارت سرور (DB round-trip) ───
    def test_identity_survives_restart_via_db(self):
        self._seed()
        self._poll(1000, ids={"black": "AAA-111"}, pages={"black": 32000})
        # شبیه‌سازی ری‌استارت: کش حافظه پاک، prev از دیتابیس می‌آید
        store._prev._cache.clear()
        prev = store._prev.get(self.IP) or {}
        self.assertEqual(prev.get("cartridge_ids"), {"black": "AAA-111"})
        self.assertEqual(prev.get("cart_supply_pages"), {"black": 32000})
        # تعویض در زمان خاموشی → اولین poll بعد از روشن‌شدن رویداد می‌دهد
        self._poll(1001, ids={"black": "CCC-333"}, pages={"black": 10})
        evs = self._events()
        kinds = sorted(e["details"].get("detection") for e in evs)
        self.assertEqual(len(evs), 2)
        self.assertEqual(kinds, ["chip_id", "supply_pages_reset"])


# ═══════════════════════════════════════════════════════════════════════════
# واحدهای خواننده (normalize / config-SNMP / HP web / کش)
# ═══════════════════════════════════════════════════════════════════════════
class CartridgeIdReaderTests(unittest.TestCase):

    def setUp(self):
        cid_mod.clear_cache()
        self._orig_cfg = cid_mod.CONFIG_PATH
        self._orig_cache = dict(cid_mod._config_cache)

    def tearDown(self):
        cid_mod.CONFIG_PATH = self._orig_cfg
        cid_mod._config_cache.update(self._orig_cache)
        cid_mod.clear_cache()

    def test_normalize_id_rejects_junk(self):
        for bad in (None, "", "  ", "0", "00", "Unknown", "N/A", "not available",
                    "-", "--", "abc", "x" * 60, "!!!####"):
            self.assertIsNone(cid_mod.normalize_id(bad), repr(bad))

    def test_normalize_id_accepts_real_serials(self):
        self.assertEqual(cid_mod.normalize_id("  ABC-1234-XZ "), "ABC-1234-XZ")
        self.assertEqual(cid_mod.normalize_id(b"SN:77F0ACE2"), "SN:77F0ACE2")
        self.assertEqual(cid_mod.normalize_id("011DTCHP2634"), "011DTCHP2634")

    def test_snmp_ids_read_from_config_map(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({
            "brands": {"hp": {"snmp": {"black": "1.3.6.1.4.1.99.1.1.0"}}},
            "printers": {"10.7.7.7": {"snmp": {"cyan": "1.3.6.1.4.1.99.1.2.0"}}},
        }, tmp)
        tmp.close()
        cid_mod.CONFIG_PATH = tmp.name
        cid_mod.load_id_config(force=True)

        calls = []

        def fake_snmp(ip, oid, community, version=None, timeout=1.0, **kw):
            calls.append(oid)
            return {"1.3.6.1.4.1.99.1.1.0": "HP-CART-0001",
                    "1.3.6.1.4.1.99.1.2.0": "Unknown"}.get(oid)

        orig_snmp = cid_mod.snmp_get_with_fallback
        cid_mod.snmp_get_with_fallback = fake_snmp
        try:
            # defer web reads برای ایزوله‌سازی تست
            orig_hp, orig_other = cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids
            cid_mod._read_hp_web_ids = lambda ip, timeout=3.5: {}
            cid_mod._read_other_web_ids = lambda ip, brand="", timeout=3.0: {}
            try:
                res = cid_mod.get_cartridge_identity_data(
                    ip="10.7.7.7", brand="hp", community="public", force_refresh=True)
            finally:
                cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids = orig_hp, orig_other
        finally:
            cid_mod.snmp_get_with_fallback = orig_snmp
            os.unlink(tmp.name)
        # black معتبر است؛ cyan به‌دلیل مقدار «Unknown» حذف می‌شود
        self.assertEqual(res["ids"], {"black": "HP-CART-0001"})
        self.assertIn("snmp", res["source"])
        self.assertEqual(len(calls), 2)

    def test_hp_web_parser_extracts_serial_and_pages(self):
        html = """
        <html><body>
          <h2 id="BlackCartridge1-Header">Black Cartridge</h2>
          <table>
            <tr><td>Serial Number:</td><td>011DTCHP2634</td></tr>
            <tr><td>Pages printed with this supply:</td><td>12,345</td></tr>
            <tr><td>Percent remaining:</td><td>67%*</td></tr>
          </table>
        </body></html>
        """
        orig_fetch = base_mod.fetch_first_web_page
        base_mod.fetch_first_web_page = lambda ip, urls, timeout=4.0: (urls[0], html)
        try:
            out = cid_mod._read_hp_web_ids("10.6.6.6")
        finally:
            base_mod.fetch_first_web_page = orig_fetch
        self.assertEqual(out.get("ids", {}).get("black"), "011DTCHP2634")
        self.assertEqual(out.get("supply_pages", {}).get("black"), 12345)

    def test_reader_uses_ttl_cache(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"brands": {"hp": {"snmp": {"black": "1.3.6.1.4.1.99.9.0"} } }}, tmp)
        tmp.close()
        cid_mod.CONFIG_PATH = tmp.name
        cid_mod.load_id_config(force=True)

        counter = {"n": 0}

        def fake_snmp(ip, oid, community, version=None, timeout=1.0, **kw):
            counter["n"] += 1
            return "KEEP-ME-77"

        orig_snmp = cid_mod.snmp_get_with_fallback
        cid_mod.snmp_get_with_fallback = fake_snmp
        orig_hp, orig_other = cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids
        cid_mod._read_hp_web_ids = lambda ip, timeout=3.5: {}
        cid_mod._read_other_web_ids = lambda ip, brand="", timeout=3.0: {}
        try:
            cid_mod.get_cartridge_identity_data(ip="10.5.5.5", brand="hp")
            cid_mod.get_cartridge_identity_data(ip="10.5.5.5", brand="hp")
            cid_mod.get_cartridge_identity_data(ip="10.5.5.5", brand="hp")
        finally:
            cid_mod.snmp_get_with_fallback = orig_snmp
            cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids = orig_hp, orig_other
            os.unlink(tmp.name)
        # سه فراخوانی در پنجره‌ی TTL → فقط یک خواندن واقعی SNMP
        self.assertEqual(counter["n"], 1)


if __name__ == "__main__":
    unittest.main()
