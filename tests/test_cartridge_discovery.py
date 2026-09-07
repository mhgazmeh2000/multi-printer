# -*- coding: utf-8 -*-
"""تست‌های کشف خودکار OID شناسه‌ی کارتریج (cartridge_discovery).

پوشش:
  ۱) فیلتر سریال‌مانند (_looks_like_serial) — رد اعداد خالص/متن‌های توصیفی.
  ۲) گذر کشف (_probe_pass) با walker جعلی — استخراج کاندیدا + نگاشت رنگ.
  ۳) دروازه‌ی ثبات — مقدار ناپایدار تأیید نمی‌شود؛ ۳ ثبات ⇒ تأیید.
  ۴) ذخیره‌سازی اتمیک در بخش learned + دست‌نخوردن ورودی‌های دستی.
  ۵) اولویت خواننده‌ی runtime: printers (دستی) > brands > learned.
  ۶) انتها-به-انتها: کشف ⇒ baseline ⇒ تغییر شناسه ⇒ رویداد CARTRIDGE_CHANGED.
"""
import json
import os
import tempfile
import unittest

from core import store
from core.database import init_db
import core.collectors.base as base_mod
import core.collectors.cartridge_id as cid_mod
import core.collectors.cartridge_discovery as cd_mod
import core.snmp.protocol as snmp_proto


# ═══════════════════════════════════════════════════════════════════════════
# واحدهای فیلتر و گذر کشف
# ═══════════════════════════════════════════════════════════════════════════
class SerialFilterTests(unittest.TestCase):

    def test_rejects_junk(self):
        for bad in (None, "12345", "100", "0", "Black Toner Cartridge",
                    "Toner Supply", "ab", "Unknown Serial", "drum unit 7"):
            self.assertFalse(cd_mod._looks_like_serial(bad), repr(bad))

    def test_rejects_model_names_and_status_messages(self):
        # داده‌ی واقعی ناوگان: نام مدل و پیام نگهداری ثابت‌اند ولی شناسه نیستند
        for bad in ("TOSHIBA e-STUDIO3015AC", "The Time for Periodic Maintenance",
                    "HP LaserJet M527", "Canon i-SENSYS LBP233dw",
                    "Ready", "Call Service Representative"):
            self.assertFalse(cd_mod._looks_like_serial(bad), repr(bad))

    def test_rejects_firmware_versions(self):
        # Toshiba firmware versions — ثابت، غیرunique، تغییر نمی‌کند
        for bad in ("V6.0.0.477.10", "V8.0.0.513.01", "V13.0.0.520.905",
                    "V22.3.0.615.303", "V21.3.0.615.303", "V8.0.0.520.10"):
            self.assertFalse(cd_mod._looks_like_serial(bad), repr(bad))

    def test_rejects_pure_numeric_part_numbers(self):
        # prtMarkerSuppliesPartNumber (مثل 19, 13, 7)
        for bad in ("19", "13", "7", "100", "101"):
            self.assertFalse(cd_mod._looks_like_serial(bad), repr(bad))

    def test_accepts_real_serials(self):
        for good in ("CN-04K1XK-72664", "011DTCHP2634", "SN77F0ACE2", "AB12-CD34"):
            self.assertTrue(cd_mod._looks_like_serial(good), repr(good))


class ProbePassTests(unittest.TestCase):

    def setUp(self):
        self._orig_walk = snmp_proto.snmp_walk

    def tearDown(self):
        snmp_proto.snmp_walk = self._orig_walk

    def _fake_walk(self, rows_by_prefix):
        def walker(ip, prefix, community="public", **kw):
            return rows_by_prefix.get(prefix, [])
        snmp_proto.snmp_walk = walker

    def test_probe_extracts_serial_with_color_from_description(self):
        # جدول استاندارد: شاخص 1 = Black، شاخص 2 = Cyan
        self._fake_walk({
            "1.3.6.1.2.1.43.11.1.1.6": [
                ("1.3.6.1.2.1.43.11.1.1.6.1.1", "Black Cartridge HP 87A"),
                ("1.3.6.1.2.1.43.11.1.1.6.1.2", "Cyan Cartridge"),
            ],
            # شاخه‌ی vendor: سریال‌ها با همان شاخص انتهایی
            "1.3.6.1.4.1.11.2.3.9.4.2.1.4": [
                ("1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.1.0", "CN04K1XK72664"),
                ("1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.0", "CY9988XZ"),
                ("1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.3.0", "12345"),  # عدد خالص → رد
            ],
        })
        res = cd_mod._probe_pass("10.1.1.1", "hp", "public")
        cands = res["candidates"]
        self.assertEqual(cands["black"]["value"], "CN04K1XK72664")
        self.assertEqual(cands["cyan"]["value"], "CY9988XZ")
        self.assertNotIn("", cands)

    def test_probe_color_from_value_text(self):
        self._fake_walk({
            "1.3.6.1.4.1.2435.2.3.9.4.2.1.5": [
                ("1.3.6.1.4.1.2435.2.3.9.4.2.1.5.1.0", "AB12CD34"),
            ],
        })
        res = cd_mod._probe_pass("10.1.1.2", "brother", "public")
        # بدون توضیح رنگ، کاندیدای بی‌رنگ رد می‌شود
        self.assertEqual(res["candidates"], {})

    def test_canon_no_candidate_from_model_identifier(self):
        """Canon: شناسه مدل فریمور (07b2010100000000) نباید کاندیدا شود.

        این مقدار یکسان برای همه Canon MF ها است و با تعویض کارتریج تغییر نمی‌کند.
        Canon فید discovery ندارد چون سریال کارتریج اختصاصی منتشر نمی‌کند."""
        self._fake_walk({
            "1.3.6.1.2.1.43.11.1.1.6": [
                ("1.3.6.1.2.1.43.11.1.1.6.1.1", "Cartridge 137"),
            ],
            "1.3.6.1.4.1.1602.1.11.1": [
                ("1.3.6.1.4.1.1602.1.11.1.1.0", 2),
                ("1.3.6.1.4.1.1602.1.11.1.2.0", "07b2010100000000"),
                ("1.3.6.1.4.1.1602.1.11.1.4.1.2.101", "101"),
            ],
        })
        res = cd_mod._probe_pass("10.1.1.3", "canon", "public")
        # Canon فید discovery ندارد → هیچ کاندیدایی نباید پیدا شود
        self.assertEqual(res["candidates"], {})

    def test_toshiba_firmware_version_filtered(self):
        """Toshiba: نسخه فریمور (V6.0.0.477.10) نباید کاندیدا شود.

        فید Toshiba خالی است (سریال کارتریج منتشر نمی‌کند)، پس حتی اگر
        نسخه فریمور از standard MIB ظاهر شود، کاندیدایی ساخته نمی‌شود."""
        self._fake_walk({
            "1.3.6.1.2.1.43.11.1.1.6": [
                ("1.3.6.1.2.1.43.11.1.1.6.1.1", "Black Toner"),
            ],
        })
        res = cd_mod._probe_pass("10.1.1.4", "toshiba", "public")
        # Toshiba فید discovery خالی دارد → هیچ کاندیدایی نباید پیدا شود
        self.assertEqual(res["candidates"], {})

    def test_probe_rejects_firmware_version_as_serial(self):
        """حتی اگر firmware version به فید vendor تزریق شود، فیلتر ردش می‌کند."""
        self._fake_walk({
            "1.3.6.1.4.1.11.2.3.9.4.2.1.4": [
                ("1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.1.0", "V6.0.0.477.10"),
                ("1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.0", "V8.0.0.513.01"),
            ],
        })
        res = cd_mod._probe_pass("10.1.1.5", "hp", "public")
        # firmware versions باید توسط فیلتر رد شوند
        self.assertEqual(res["candidates"], {})

    def test_canon_counter_labels_rejected(self):
        """Canon: برچسب‌های شمارنده (TotalCount...) رد می‌شوند"""
        self._fake_walk({
            "1.3.6.1.4.1.1602.1.11.1": [
                ("1.3.6.1.4.1.1602.1.11.1.4.1.2.101", "TotalCount101"),
                ("1.3.6.1.4.1.1602.1.11.1.4.1.2.102", "TotalCount102"),
            ],
        })
        res = cd_mod._probe_pass("10.1.1.4", "canon", "public")
        self.assertEqual(res["candidates"], {})


# ═══════════════════════════════════════════════════════════════════════════
# دروازه‌ی ثبات + ذخیره‌سازی
# ═══════════════════════════════════════════════════════════════════════════
class DiscoveryPersistenceTests(unittest.TestCase):

    IP = "10.9.8.7"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.tmp.name, "cartridge_id_map.json")
        # تنظیمات دستی از قبل موجود — نباید بازنویسی شود
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "brands": {"hp": {"snmp": {}}},
                "printers": {self.IP: {"snmp": {}}},
            }, fh)
        self._orig_cfg = cd_mod.CONFIG_PATH
        self._orig_cid_cfg = cid_mod.CONFIG_PATH
        self._orig_cache = dict(cid_mod._config_cache)
        cd_mod.CONFIG_PATH = self.cfg_path
        cid_mod.CONFIG_PATH = self.cfg_path
        cid_mod.clear_cache()
        cd_mod.reset_stability()

    def tearDown(self):
        cd_mod.CONFIG_PATH = self._orig_cfg
        cid_mod.CONFIG_PATH = self._orig_cid_cfg
        cid_mod._config_cache.update(self._orig_cache)
        cid_mod.clear_cache()
        cd_mod.reset_stability()
        self.tmp.cleanup()

    def _probe_fn_factory(self, values_per_pass):
        """probe_fn جعلی که به ترتیب پاس‌ها مقادیر متفاوت/یکسان می‌دهد."""
        calls = {"n": 0}

        def probe_fn(ip, brand, community, snmp_version):
            n = min(calls["n"], len(values_per_pass) - 1)
            calls["n"] += 1
            val = values_per_pass[n]
            if val is None:
                return {"candidates": {}, "desc_map": {}}
            return {"candidates": {"black": {"oid": "1.3.6.1.4.1.99.1.1.0", "value": val}},
                    "desc_map": {}}
        return probe_fn

    def test_unstable_value_never_confirmed(self):
        out = cd_mod.discover_for_printer(
            self.IP, "hp", "public", passes=4, probe_gap=0,
            probe_fn=self._probe_fn_factory(["AAA-1", "BBB-2", "CCC-3", "DDD-4"]))
        self.assertEqual(out["confirmed"], {})
        self.assertEqual(cd_mod.load_learned_oids(self.IP), {})

    def test_stable_value_confirmed_after_three_passes(self):
        out = cd_mod.discover_for_printer(
            self.IP, "hp", "public", passes=3, probe_gap=0,
            probe_fn=self._probe_fn_factory(["STABLE-77", "STABLE-77", "STABLE-77"]))
        self.assertEqual(out["confirmed"], {"black": "1.3.6.1.4.1.99.1.1.0"})
        learned = cd_mod.load_learned_oids(self.IP)
        self.assertEqual(learned, {"black": "1.3.6.1.4.1.99.1.1.0"})

    def test_manual_entries_preserved_atomic_write(self):
        cd_mod.discover_for_printer(
            self.IP, "hp", "public", passes=3, probe_gap=0,
            probe_fn=self._probe_fn_factory(["KEEP-9", "KEEP-9", "KEEP-9"]))
        with open(self.cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # ورودی دستی (خالی ولی موجود) دست‌نخورده + learned اضافه شده
        self.assertIn(self.IP, data["printers"])
        self.assertIn(self.IP, data["learned"]["printers"])
        self.assertEqual(data["learned"]["printers"][self.IP]["snmp"]["black"],
                         "1.3.6.1.4.1.99.1.1.0")
        self.assertNotEqual(data["learned"]["printers"][self.IP]["snmp"], data["printers"][self.IP]["snmp"])
        # فایل temp باقی نمانده باشد
        self.assertFalse(os.path.exists(self.cfg_path + ".tmp"))

    def test_runtime_reader_merges_learned_manual_wins(self):
        cd_mod.discover_for_printer(
            self.IP, "hp", "public", passes=3, probe_gap=0,
            probe_fn=self._probe_fn_factory(["KEEP-9", "KEEP-9", "KEEP-9"]))
        # دستی: cyan روی OID متفاوت — باید بر learned غلبه کند
        with open(self.cfg_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data["printers"][self.IP]["snmp"] = {"cyan": "1.3.6.1.4.1.99.MANUAL.0"}
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        cid_mod.load_id_config(force=True)
        oids = cid_mod._snmp_oids_for(self.IP, "hp", cid_mod.load_id_config())
        self.assertEqual(oids["black"], "1.3.6.1.4.1.99.1.1.0")   # از learned
        self.assertEqual(oids["cyan"], "1.3.6.1.4.1.99.MANUAL.0")  # دستی برد


# ═══════════════════════════════════════════════════════════════════════════
# انتها-به-انتها: کشف ⇒ baseline ⇒ تعویض ⇒ رویداد CARTRIDGE_CHANGED
# ═══════════════════════════════════════════════════════════════════════════
class EndToEndDiscoveryEventTests(unittest.TestCase):
    IP = "10.7.6.5"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        init_db()
        store._prev._cache.clear()
        self.events = []

        self.cfg_path = os.path.join(self.tmp.name, "cartridge_id_map.json")
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({}, fh)
        self._orig_cfg = cd_mod.CONFIG_PATH
        self._orig_cid_cfg = cid_mod.CONFIG_PATH
        self._orig_cache = dict(cid_mod._config_cache)
        cd_mod.CONFIG_PATH = self.cfg_path
        cid_mod.CONFIG_PATH = self.cfg_path
        cid_mod.clear_cache()
        cd_mod.reset_stability()

        self._orig_add_event = base_mod.add_event
        base_mod.add_event = lambda ip, etype, details: self.events.append(
            {"ip": ip, "type": etype, "details": dict(details or {})})

        import core.snmp.protocol as _snmp_mod
        self._orig_snmp = _snmp_mod.snmp_get_with_fallback
        self.serial = {"v": "DISCOVERED-001"}
        _snmp_mod.snmp_get_with_fallback = (
            lambda ip, oid, community, version=None, timeout=1.0, **kw:
            self.serial["v"] if oid == "1.3.6.1.4.1.99.1.1.0" else None)
        self._orig_hp_web, self._orig_other_web = cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids
        cid_mod._read_hp_web_ids = lambda ip, timeout=3.5: {}
        cid_mod._read_other_web_ids = lambda ip, brand="", timeout=3.0: {}

    def tearDown(self):
        import core.snmp.protocol as _snmp_mod
        base_mod.add_event = self._orig_add_event
        _snmp_mod.snmp_get_with_fallback = self._orig_snmp
        cid_mod._read_hp_web_ids, cid_mod._read_other_web_ids = self._orig_hp_web, self._orig_other_web
        cd_mod.CONFIG_PATH = self._orig_cfg
        cid_mod.CONFIG_PATH = self._orig_cid_cfg
        cid_mod._config_cache.update(self._orig_cache)
        cid_mod.clear_cache()
        cd_mod.reset_stability()
        store._prev._cache.clear()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _probe_fn(self, ip, brand, community, snmp_version):
        return {"candidates": {"black": {"oid": "1.3.6.1.4.1.99.1.1.0",
                                         "value": self.serial["v"]}},
                "desc_map": {}}

    def test_discovery_to_cartridge_changed_event(self):
        # ۱) کشف و تأیید OID
        out = cd_mod.discover_for_printer(
            self.IP, "toshiba", "public", passes=3, probe_gap=0,
            probe_fn=self._probe_fn)
        self.assertEqual(out["confirmed"], {"black": "1.3.6.1.4.1.99.1.1.0"})

        # ۲) خواننده‌ی runtime شناسه را از OID کشف‌شده می‌خواند
        cid_mod.load_id_config(force=True)
        data = cid_mod.get_cartridge_identity_data(self.IP, brand="toshiba",
                                                   force_refresh=True)
        self.assertEqual(data["ids"], {"black": "DISCOVERED-001"})
        self.assertIn("snmp", data["source"])

        # ۳) baseline اولین poll
        store._prev.set(self.IP, {
            "print_total": 1000, "full_color": 0, "black_white": 1000,
            "toner_level": 40, "alert_codes": [], "last_alert_codes": [],
            "uptime": 500_000_000,
        })
        base_mod._counters_event(self.IP, 1000, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=1000, current_toner_level=40,
                                 prev_toner_level=40, cartridge_ids=data["ids"],
                                 uptime=500_100_000)
        self.assertEqual([e for e in self.events if e["type"] == "CARTRIDGE_CHANGED"], [])

        # ۴) تعویض کارتریج: سریال عوض می‌شود ⇒ رویداد قطعی همان poll
        self.serial["v"] = "DISCOVERED-002"
        base_mod._counters_event(self.IP, 1002, store._prev.get(self.IP), [], [],
                                 full_color=0, black_white=1002, current_toner_level=95,
                                 prev_toner_level=40, cartridge_ids={"black": "DISCOVERED-002"},
                                 uptime=500_200_000)
        evs = [e for e in self.events if e["type"] == "CARTRIDGE_CHANGED"]
        self.assertEqual(len(evs), 1)
        d = evs[0]["details"]
        self.assertEqual(d["detection"], "chip_id")
        self.assertEqual(d["prev_cartridge_id"], "DISCOVERED-001")
        self.assertEqual(d["cartridge_id"], "DISCOVERED-002")


if __name__ == "__main__":
    unittest.main()
