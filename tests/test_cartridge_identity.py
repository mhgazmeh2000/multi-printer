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
              ids=None, pages=None, uptime=None, signal_quality=None,
              supply_names=None):
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
            signal_quality=signal_quality,
            supply_names=supply_names,
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
        self._poll(1004, pages={"black": 15})          # reset alone is insufficient evidence
        evs = self._events()
        self.assertEqual(evs, [])

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
    def test_static_quality_suppresses_event(self):
        """شناسه با کیفیت static (مدل/پارت) نباید رویداد تغییر بدهد."""
        self._seed()
        sq = {"black": "static"}
        self._poll(1000, ids={"black": "model:Cartridge 137"}, signal_quality=sq)
        self._poll(1001, ids={"black": "model:Canon Cartridge 324 II"}, signal_quality=sq)
        # تغییر مدل → false positive → نباید رویداد باشد
        self.assertEqual(self._events(), [])

    def test_static_quality_uses_signal_quality_param(self):
        """فقط وقتی signal_quality=static باشد، فیلتر فعال شود."""
        self._seed()
        self._poll(1000, ids={"black": "AAA"})
        prev = store._prev.get(self.IP) or {}
        # تغییر شناسه بدون signal_quality → رویداد (like genuine)
        base_mod._counters_event(
            self.IP, 1001, prev, [], [],
            full_color=0, black_white=1001,
            current_toner_level=40, prev_toner_level=40,
            cartridge_ids={"black": "BBB"},
        )
        self.assertEqual(len(self._events()), 1)

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
        self.assertEqual(len(evs), 1)
        self.assertEqual(kinds, ["chip_id"])
        self.assertIn("supply_counter", [item["name"] for item in evs[0]["details"]["evidence"]])


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
                    "-", "--", "abc", "x" * 60, "!!!####",
                    "Number", "number", "No", "ID"):
            self.assertIsNone(cid_mod.normalize_id(bad), repr(bad))

    def test_hp_web_parser_skips_bare_number_label(self):
        # باگ واقعی ناوگان: برچسب دوپاره‌ی Serial</span><span>Number باعث می‌شد
        # خودِ واژه‌ی «Number» به‌عنوان سریال گرفته شود.
        html = """
        <html><body>
          <tr><td>Serial</td><td>Number</td><td>16846561</td></tr>
        </body></html>
        """
        orig_fetch = base_mod.fetch_first_web_page
        base_mod.fetch_first_web_page = lambda ip, urls, timeout=4.0: (urls[0], html)
        try:
            out = cid_mod._read_hp_web_ids("10.6.6.7")
        finally:
            base_mod.fetch_first_web_page = orig_fetch
        self.assertEqual(out.get("ids", {}).get("black"), "16846561")
        self.assertEqual(out.get("identity_type", {}).get("black"), "serial")

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

        import core.snmp.protocol as _snmp_mod
        orig_snmp = _snmp_mod.snmp_get_with_fallback
        _snmp_mod.snmp_get_with_fallback = fake_snmp
        try:
            orig_hp, orig_other = cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids
            cid_mod._read_hp_web_ids = lambda ip, timeout=3.5: {}
            cid_mod._read_other_web_ids = lambda ip, brand="", timeout=3.0: {}
            try:
                res = cid_mod.get_cartridge_identity_data(
                    ip="10.7.7.7", brand="hp", community="public", force_refresh=True)
            finally:
                cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids = orig_hp, orig_other
        finally:
            _snmp_mod.snmp_get_with_fallback = orig_snmp
        os.unlink(tmp.name)
        # black معتبر است؛ cyan به‌دلیل مقدار «Unknown» حذف می‌شود
        self.assertEqual(res["ids"], {"black": "HP-CART-0001"})
        self.assertEqual(res["identity_type"], {"black": "chip_id"})
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
        self.assertEqual(out.get("identity_type", {}).get("black"), "serial")

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

        import core.snmp.protocol as _snmp_mod
        orig_snmp = _snmp_mod.snmp_get_with_fallback
        _snmp_mod.snmp_get_with_fallback = fake_snmp
        orig_hp, orig_other = cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids
        cid_mod._read_hp_web_ids = lambda ip, timeout=3.5: {}
        cid_mod._read_other_web_ids = lambda ip, brand="", timeout=3.0: {}
        try:
            cid_mod.get_cartridge_identity_data(ip="10.5.5.5", brand="hp")
            cid_mod.get_cartridge_identity_data(ip="10.5.5.5", brand="hp")
            cid_mod.get_cartridge_identity_data(ip="10.5.5.5", brand="hp")
        finally:
            _snmp_mod.snmp_get_with_fallback = orig_snmp
            cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids = orig_hp, orig_other
            os.unlink(tmp.name)
        # سه فراخوانی در پنجره‌ی TTL → فقط یک خواندن واقعی SNMP
        self.assertEqual(counter["n"], 1)


class FallbackIdentityTests(unittest.TestCase):
    """تست شناسه‌های جایگزین (Brother toner gen / HP part / Canon model)."""

    def setUp(self):
        import core.snmp.protocol as _snmp_mod
        self._orig_snmp_mod = _snmp_mod
        self._orig_cfg = cid_mod.CONFIG_PATH
        self._orig_cache = dict(cid_mod._config_cache)
        cid_mod.clear_cache()
        self._tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({}, self._tmp)
        self._tmp.close()
        cid_mod.CONFIG_PATH = self._tmp.name
        cid_mod.load_id_config(force=True)
        self._patcher = False
        # Mock web scrapers to avoid real HTTP calls
        self._orig_hp_web = cid_mod._read_hp_web_ids
        self._orig_other_web = cid_mod._read_other_web_ids
        cid_mod._read_hp_web_ids = lambda ip, timeout=3.5: {}
        cid_mod._read_other_web_ids = lambda ip, brand="", timeout=3.0: {}

    def _patch_sg(self, fake_fn):
        """Patch snmp_get_with_fallback at the protocol module level."""
        import core.snmp.protocol as _snmp_mod
        self._orig_sg_val = _snmp_mod.snmp_get_with_fallback
        _snmp_mod.snmp_get_with_fallback = fake_fn
        self._patcher = True

    def tearDown(self):
        if self._patcher:
            import core.snmp.protocol as _snmp_mod
            _snmp_mod.snmp_get_with_fallback = self._orig_sg_val
        cid_mod._read_hp_web_ids = self._orig_hp_web
        cid_mod._read_other_web_ids = self._orig_other_web
        cid_mod.CONFIG_PATH = self._orig_cfg
        cid_mod._config_cache.update(self._orig_cache)
        cid_mod.clear_cache()
        os.unlink(self._tmp.name)

    def test_brother_toner_gen_count(self):
        def fake_sg(ip, oid, community, version=None, timeout=1.0, **kw):
            if "2435" in oid:
                return "5"
            return None
        self._patch_sg(fake_sg)
        r = cid_mod.get_cartridge_identity_data("10.2.2.2", "brother")
        self.assertEqual(r["ids"], {"black": "toner_gen:5"})
        self.assertEqual(r["source"], "brother_snmp_gen")

    def test_hp_part_number_fallback(self):
        def fake_sg(ip, oid, community, version=None, timeout=1.0, **kw):
            if "43.11.1.1.6" in oid:
                return "CC388A"
            return None
        self._patch_sg(fake_sg)
        r = cid_mod.get_cartridge_identity_data("10.3.3.3", "hp")
        self.assertEqual(r["ids"], {"black": "part:CC388A"})
        self.assertEqual(r["source"], "hp_mib_part")

    def test_canon_model_fallback(self):
        def fake_sg(ip, oid, community, version=None, timeout=1.0, **kw):
            if "43.11.1.1.6" in oid:
                return "Canon Cartridge 324 II"
            return None
        self._patch_sg(fake_sg)
        r = cid_mod.get_cartridge_identity_data("10.4.4.4", "canon")
        self.assertEqual(r["ids"], {"black": "model:Canon Cartridge 324 II"})
        self.assertEqual(r["source"], "canon_mib_model")

    def test_fallback_not_used_when_ids_exist(self):
        """اگر شناسه از SNMP معمولی پیدا شد، fallback فراخوانی نشود."""
        # config با OID دستی تا _snmp_oids_for چیزی برگرداند
        with open(self._tmp.name, "w", encoding="utf-8") as fh:
            json.dump({"brands": {"hp": {"snmp": {"black": "1.3.6.1.4.1.99.9.0"}}}}, fh)
        cid_mod.load_id_config(force=True)

        def fake_sg(ip, oid, community, version=None, timeout=1.0, **kw):
            if "99.9.0" in oid:
                return "SN123"
            if "43.11.1.1.6" in oid:
                return "SHOULD-NOT-BE-USED"
            if "2435" in oid:
                return "99"
            return None
        self._patch_sg(fake_sg)
        r = cid_mod.get_cartridge_identity_data("10.5.5.5", "hp")
        self.assertEqual(r["ids"], {"black": "SN123"})
        self.assertNotIn("SHOULD-NOT-BE-USED", str(r))

    def test_brother_gen_change_detected(self):
        """تغییر شمارشگر Brother ⇒ تشخیص تعویض کارتریج."""
        calls = {"n": 0}
        def fake_sg(ip, oid, community, version=None, timeout=1.0, **kw):
            if "2435" in oid:
                calls["n"] += 1
                return "3" if calls["n"] <= 1 else "4"
            return None
        self._patch_sg(fake_sg)
        r1 = cid_mod.get_cartridge_identity_data("10.6.6.6", "brother", force_refresh=True)
        self.assertEqual(r1["ids"]["black"], "toner_gen:3")
        r2 = cid_mod.get_cartridge_identity_data("10.6.6.6", "brother", force_refresh=True)
        self.assertEqual(r2["ids"]["black"], "toner_gen:4")
        self.assertNotEqual(r1["ids"]["black"], r2["ids"]["black"])


# ═══════════════════════════════════════════════════════════════════════════
# تشخیص تعویض از روی تغییر نام supply
# ═══════════════════════════════════════════════════════════════════════════
class SupplyNameChangeDetectionTests(unittest.TestCase):
    IP = "10.9.9.20"
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

    def _seed(self, total=1000):
        store._prev.set(self.IP, {
            "print_total": total, "full_color": 0, "black_white": total,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 0,
        })
        recorded_events.clear()

    def _poll(self, total, supply_names=None, ids=None, sq=None):
        prev = store._prev.get(self.IP) or {}
        base_mod._counters_event(
            self.IP, total, prev, [], [],
            full_color=0, black_white=total,
            current_toner_level=40,
            prev_toner_level=prev.get("toner_level"),
            cartridge_ids=ids,
            signal_quality=sq,
            supply_names=supply_names,
            uptime=self.UP + total * 100,
        )

    def test_supply_name_change_fires_event(self):
        """تغییر نام supply از HP 87A به CC388A ⇒ رویداد (بعد از ۲ poll)."""
        self._seed()
        self._poll(1000, supply_names={"black": "Black Cartridge HP 87A (CF287A)"})
        self.assertEqual(self._events(), [])  # baseline
        self._poll(1001, supply_names={"black": "CC388A"})
        self.assertEqual(self._events(), [])  # اولین تغییر → pending
        self._poll(1002, supply_names={"black": "CC388A"})
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertIn("نام supply تغییر کرد", evs[0]["details"]["message"])
        self.assertEqual(evs[0]["details"]["detection"], "supply_name_change")
        self.assertEqual(evs[0]["details"]["prev_supply_name"], "Black Cartridge HP 87A (CF287A)")
        self.assertEqual(evs[0]["details"]["supply_name"], "CC388A")

    def test_supply_name_same_no_event(self):
        """نام supply یکسان ⇒ رویداد نه."""
        self._seed()
        self._poll(1000, supply_names={"black": "Black Toner Cartridge"})
        self._poll(1001, supply_names={"black": "Black Toner Cartridge"})
        self.assertEqual(self._events(), [])

    def test_supply_name_case_insensitive(self):
        """تغییر فقط در حروف بزرگ/کوچک ⇒ رویداد نه."""
        self._seed()
        self._poll(1000, supply_names={"black": "Black Toner"})
        self._poll(1001, supply_names={"black": "black toner"})
        self.assertEqual(self._events(), [])

    def test_supply_name_first_observation_no_event(self):
        """اولین مشاهده نام ⇒ فقط baseline، رویداد نه."""
        self._seed()
        self._poll(1000, supply_names={"black": "CC388A"})
        self.assertEqual(self._events(), [])  # اولین poll → baseline
        self._poll(1001, supply_names={"black": "CC388A"})
        self.assertEqual(self._events(), [])  # بدون تغییر

    def test_supply_name_skipped_when_chip_id_genuine(self):
        """اگر chip_id واقعی (genuine) فعال است، تغییر نام نادیده گرفته شود."""
        self._seed()
        self._poll(1000, supply_names={"black": "HP 87A"},
                   ids={"black": "ABC123"}, sq={"black": "genuine"})
        self._poll(1001, supply_names={"black": "HP 87X"},
                   ids={"black": "ABC123"}, sq={"black": "genuine"})
        self.assertEqual(self._events(), [])  # genuine → skip name change

    def test_supply_name_fires_when_chip_id_static(self):
        """اگر chip_id static است، تغییر نام باید رویداد بدهد (بعد از ۲ poll)."""
        self._seed()
        self._poll(1000, supply_names={"black": "HP 87A"},
                   ids={"black": "part:CC388A"}, sq={"black": "static"})
        self._poll(1001, supply_names={"black": "HP 87X"},
                   ids={"black": "part:CC388A"}, sq={"black": "static"})
        self.assertEqual(self._events(), [])  # اولین تغییر → pending
        self._poll(1002, supply_names={"black": "HP 87X"},
                   ids={"black": "part:CC388A"}, sq={"black": "static"})
        evs = self._events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["details"]["detection"], "supply_name_change")

    def test_supply_name_skipped_when_chip_id_changed(self):
        """اگر قبلاً chip_id_changed ثبت شده، تغییر نام تکراری نیست."""
        self._seed()
        self._poll(1000, supply_names={"black": "HP 87A"},
                   ids={"black": "OLD123"})
        # chip_id changed + supply_name changed → فقط chip_id event
        self._poll(1001, supply_names={"black": "HP 87X"},
                   ids={"black": "NEW456"})
        evs = self._events()
        # باید فقط یک رویداد (chip_id) باشد، نه دو
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["details"]["detection"], "chip_id")

    def test_supply_name_manual_override_skips(self):
        """manual_override ⇒ تغییر نام نادیده گرفته شود."""
        store._prev.set(self.IP, {
            "print_total": 1000, "full_color": 0, "black_white": 1000,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 1,
        })
        recorded_events.clear()
        self._poll(1001, supply_names={"black": "New Toner"})
        self.assertEqual(self._events(), [])

    def test_supply_name_flip_flop_no_event(self):
        """تغییر نام و برگشت (flip-flop) ⇒ رویداد نه — دروازه ثبات."""
        self._seed()
        self._poll(1000, supply_names={"black": "HP 87A"})
        self._poll(1001, supply_names={"black": "black cartridge"})  # flip
        self.assertEqual(self._events(), [])  # pending
        self._poll(1002, supply_names={"black": "HP 87A"})  # flop back
        self.assertEqual(self._events(), [])  # pending لغو شد
        self._poll(1003, supply_names={"black": "black cartridge"})  # flip again
        self.assertEqual(self._events(), [])  # دوباره pending
        self._poll(1004, supply_names={"black": "HP 87A"})  # flop again
        self.assertEqual(self._events(), [])  # هیچ رویدادی


# ═══════════════════════════════════════════════════════════════════════════
# تست End-to-End: ذخیره‌ی رویداد در دیتابیس واقعی
# ═══════════════════════════════════════════════════════════════════════════
class EndToEndDatabaseTests(unittest.TestCase):
    """تست زنجیره‌ی کامل: collector → _counters_event → add_event → SQLite → get_log.

    برخلاف تست‌های بالا که add_event را با fake جایگزین می‌کنند، اینجا از
    add_event واقعی استفاده می‌شود تا تأیید شود رویداد با تمام جزئیات
    در دیتابیس ذخیره و قابل بازیابی است.
    """
    IP = "10.99.99.1"
    UP = 900_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        # بدون patch کردن add_event — از مسیر واقعی دیتابیس استفاده می‌شود
        # اما نیاز به راه‌اندازی PRINTERS store داریم تا add_event نام پرینتر را پیدا کند
        store.PRINTERS = [{"ip": self.IP, "name": "Test HP M527"}]

    def tearDown(self):
        store._prev._cache.clear()
        store.PRINTERS = []
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _query_events(self, etype=None):
        """خواندن رویدادها از SQLite با فیلتر نوع."""
        from core.database import get_log
        events = get_log(self.IP, limit=100)
        if etype:
            events = [e for e in events if e.get("type") == etype]
        return events

    def _poll(self, total, ids=None, pages=None, supply_names=None, sq=None):
        prev = store._prev.get(self.IP) or {}
        base_mod._counters_event(
            self.IP, total, prev, [], [],
            full_color=0, black_white=total,
            current_toner_level=40,
            prev_toner_level=prev.get("toner_level"),
            cartridge_ids=ids,
            cartridge_supply_pages=pages,
            signal_quality=sq,
            supply_names=supply_names,
            uptime=self.UP + total * 100,
        )

    # ─── ۱) تغییر نام supply ⇒ رویداد در دیتابیس ───
    def test_supply_name_change_stored_in_database(self):
        """زنجیره‌ی کامل: seed → poll baseline → poll change → poll confirm → DB query."""
        # seed
        store._prev.set(self.IP, {
            "print_total": 1000, "full_color": 0, "black_white": 1000,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 0,
        })

        # poll 1: baseline — نام اولیه ثبت می‌شود
        self._poll(1000, supply_names={"black": "HP 87A (CF287A)"})
        self.assertEqual(self._query_events("CARTRIDGE_CHANGED"), [])

        # poll 2: تغییر نام — pending ثبت می‌شود
        self._poll(1001, supply_names={"black": "CC388A"})
        self.assertEqual(self._query_events("CARTRIDGE_CHANGED"), [])

        # poll 3: تأیید — رویداد باید در SQLite ذخیره شده باشد
        self._poll(1002, supply_names={"black": "CC388A"})
        events = self._query_events("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 1, "باید دقیقاً یک رویداد CARTRIDGE_CHANGED ذخیره شود")

        e = events[0]
        # بررسی فیلدهای top-level
        self.assertEqual(e["printer_ip"], self.IP)
        self.assertEqual(e["printer_name"], "Test HP M527")
        self.assertEqual(e["severity"], "info")
        self.assertIn("نام supply تغییر کرد", e["message"])

        # بررسی فیلدهای details (ذخیره‌شده به‌صورت JSON در ستون details)
        self.assertEqual(e["detection"], "supply_name_change")
        self.assertEqual(e["color"], "black")
        self.assertEqual(e["color_fa"], "مشکی")
        self.assertTrue(e["auto_detected"])
        self.assertTrue(e["confirmed"])
        self.assertEqual(e["prev_supply_name"], "HP 87A (CF287A)")
        self.assertEqual(e["supply_name"], "CC388A")

        # timestamp باید معتبر باشد
        self.assertIsNotNone(e["timestamp"])
        self.assertTrue(len(e["timestamp"]) > 10)

    # ─── ۲) تغییر شناسه تراشه ⇒ رویداد با detection=chip_id ───
    def test_chip_id_change_stored_in_database(self):
        store._prev.set(self.IP, {
            "print_total": 5000, "full_color": 0, "black_white": 5000,
            "toner_level": 50, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 0,
        })

        self._poll(5000, ids={"black": "SN-OLD-123"})
        self._poll(5001, ids={"black": "SN-NEW-456"})

        events = self._query_events("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["detection"], "chip_id")
        self.assertEqual(e["prev_cartridge_id"], "SN-OLD-123")
        self.assertEqual(e["cartridge_id"], "SN-NEW-456")

    # ─── ۳) ریست صفحات کارتریج به‌تنهایی ⇒ بدون رویداد ───
    def test_pages_reset_stored_in_database(self):
        store._prev.set(self.IP, {
            "print_total": 1000, "full_color": 0, "black_white": 1000,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 0,
        })

        self._poll(1000, pages={"black": 30000})
        self._poll(1001, pages={"black": 30050})
        self._poll(1002, pages={"black": 20})     # ریست!

        events = self._query_events("CARTRIDGE_CHANGED")
        self.assertEqual(events, [])

    # ─── ۴) فیلتر کیفیت static در مسیر دیتابیس ───
    def test_static_quality_no_event_in_database(self):
        """شناسه static نباید رویدادی در دیتابیس ایجاد کند."""
        store._prev.set(self.IP, {
            "print_total": 1000, "full_color": 0, "black_white": 1000,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 0,
        })
        sq = {"black": "static"}
        self._poll(1000, ids={"black": "model:Old"}, sq=sq)
        self._poll(1001, ids={"black": "model:New"}, sq=sq)

        events = self._query_events("CARTRIDGE_CHANGED")
        self.assertEqual(len(events), 0, "شناسه static نباید رویداد ثبت کند")

    # ─── ۵) جستجوی متنی در دیتابیس — رویداد قابل فیلتر است ───
    def test_event_queryable_by_type_filter(self):
        """رویدادهای CARTRIDGE_CHANGED از بقیه رویدادها قابل تفکیک هستند."""
        store._prev.set(self.IP, {
            "print_total": 1000, "full_color": 0, "black_white": 1000,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": self.UP, "manual_override": 0,
        })

        # poll اولیه + ثبت یک رویداد PRINT
        self._poll(1000)
        self._poll(1001)  # 1 صفحه چاپ
        self._poll(1002, supply_names={"black": "HP 87A"})
        self._poll(1003, supply_names={"black": "HP 87A"})
        self._poll(1004, supply_names={"black": "CC388A"})
        self._poll(1005, supply_names={"black": "CC388A"})  # تأیید

        all_events = self._query_events()
        cc_events = self._query_events("CARTRIDGE_CHANGED")
        print_events = self._query_events("PRINT")

        # حداقل یک PRINT و یک CARTRIDGE_CHANGED باید باشد
        self.assertGreater(len(print_events), 0, "باید رویداد PRINT وجود داشته باشد")
        self.assertEqual(len(cc_events), 1, "باید دقیقاً یک CARTRIDGE_CHANGED وجود داشته باشد")
        self.assertIn(cc_events[0], all_events, "CARTRIDGE_CHANGED باید در همه رویدادها باشد")
        self.assertNotIn(cc_events[0], print_events, "CARTRIDGE_CHANGED نباید در PRINT باشد")


if __name__ == "__main__":
    unittest.main()
