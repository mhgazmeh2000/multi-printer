# -*- coding: utf-8 -*-
"""تست‌های مسیر کامل USE_SNMP_EXPORTER=1 در برابر fixtureهای طلایی (گام H.3).

این تست پیمانِ برگ‌های قابل‌استخراج (Extractable Leaves Contract) را در دو سطح می‌سنجد:

  سطح ۱ — `collect_via_exporter` در برابر fixtureهای طلایی:
    • ۳۰ برگِ قابل‌استخراجِ SNMP برای HP color (تفاضل صفر در هر دو حالت پر و خالی).
    • ۲ برگِ volatile (last_poll, poll_ms) که پویا هستند.
    • ۲ برگِ fixture gap (counters.full_color, counters.black_white) که OIDهای شمارندهٔ
      اختصاصی اچ‌پی (11.2.3.9.6.1.1.*) در fixture وجود ندارند (ثبت در TD-020).
    • ۱۲۶ برگِ غنای legacy برای حالت پر (و ۱۳۴ برگ برای خالی با احتساب ۸ کلید هشدارهای سنتز‌شده).
    • صفر برگ طبقه‌بندی‌نشده (Unclassified) در هر دو ساختار.
    • اثبات عدم ورود (non-entry) برای برندهای پشتیبانی‌نشده (Brother، Toshiba، ECS100G)
      با صفر کوئری exporter و ثبت دقیقاً یک warning به ازای هر برند.

  سطح ۲ — `poller.collect()` با پرچمِ `USE_SNMP_EXPORTER=1`:
    • برای HP، بعد از پاسخ موفق exporter هیچ OID collector خوانده نمی‌شود
      (توالی فراخوانی transport فقط شامل health probe اولیه است).
    • مقادیر `nickname` و `device_type` توسط poller به خروجی الحاق می‌شوند.
    • خروجی در `store.printer_data` ذخیره می‌شود.
    • طبق طراحی G.5، مسیر exporter عمداً هیچ رویداد شمارنده‌ای ثبت نمی‌کند و صفر ردیف در DB می‌نویسد.
    • برای برندهای پشتیبانی‌نشده، poller با خاموش ماندن مسیر exporter، به شکل خودکار
      به مسیر legacy برمی‌گردد.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import tempfile
import unittest

import pytest
import yaml

from core import store
from core.collectors.exporter_adapter import collect_via_exporter
from core.exporter_client import ExporterClient
import core.poller as poller
from tests.golden_harness import (
    FIXTURE_DIR,
    FakeHttp,
    frozen_time,
    isolated_db,
    snapshot_db,
)
from tests.golden_profiles import build_transport
from tools.compare_snmp_sources import flatten

pytestmark = [pytest.mark.pytest_only, pytest.mark.sqlite_only]


def load_tests(loader, tests, pattern):  # noqa: ARG001
    """پروتکل unittest: این فایل با unittest کشف/اجرا نمی‌شود."""
    return unittest.TestSuite()


ROOT = Path(__file__).resolve().parents[1]
MODS = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))["modules"]


# ── ۱) پیمان برگ‌ها (Leaf Classification Contracts) ─────────────────────────

#: ۳۰ برگِ قابل‌استخراجِ مستقیم از MIBهای استاندارد SNMP برای HP color.
#: این برگ‌ها در هر دو سناریوی پر (enhanced_01) و خالی (enhanced_02) با golden تفاضل-صفر دارند.
EXTRACTABLE_SNMP_LEAVES_HP_COLOR = frozenset({
    "brand",
    "counters.copy",
    "counters.fax",
    "counters.printer",
    "counters.total",
    "device.firmware",
    "device.model",
    "device.serial",
    "device.uptime_str",
    "ip",
    "name",
    "online",
    "toners.black.level",
    "toners.black.max",
    "toners.black.name",
    "toners.black.remaining",
    "toners.black.status",
    "toners.cyan.level",
    "toners.cyan.max",
    "toners.cyan.name",
    "toners.cyan.remaining",
    "toners.cyan.status",
    "trays.0.capacity",
    "trays.0.level",
    "trays.0.name",
    "trays.0.status",
    "trays.1.capacity",
    "trays.1.level",
    "trays.1.name",
    "trays.1.status",
})

#: ۲ برگِ پویا که زمان و تأخیر اجرای poll را نشان می‌دهند (معادل VOLATILE_PATHS ابزار G.6).
VOLATILE_PATHS = frozenset({
    "last_poll",
    "poll_ms",
})

#: ۲ برگِ شمارنده‌های اختصاصی اچ‌پی که OIDهای آن‌ها (1.3.6.1.4.1.11.2.3.9.6.1.1.*)
#: در fixtureهای فعلی وجود ندارند و در نتیجه در خروجی exporter برابر None هستند (TD-020).
FIXTURE_GAPS = frozenset({
    "counters.full_color",
    "counters.black_white",
})

#: ۱۲۶ برگِ غنای legacy برای HP color در وضعیت عادی/پر (enhanced_01).
#: این داده‌ها از catalog، موتور yield_engine و PrevStore تولید می‌شوند و در MIB خام نیستند.
LEGACY_ENRICHMENT_PATHS = frozenset({
    "cartridge_id_source",
    "cartridge_identity_type.black",
    "cartridge_ids.black",
    "cartridge_signal_quality.black",
    "counters.copy_bw",
    "counters.copy_fc",
    "counters.force_estimate",
    "counters.list",
    "counters.pages_since_last_reset",
    "counters.printer_bw",
    "counters.printer_fc",
    "counters.scan_bw",
    "counters.scan_fc",
    "counters.scan_net_bw",
    "counters.scan_net_fc",
    "counters.twin",
    "counters.yield_confidence",
    "counters.yield_learning_failures",
    "counters.yield_per_page",
    "counters.yield_source",
    "device_type",
    "nickname",
    "toners.black.capacity_pages",
    "toners.black.capacity_source",
    "toners.black.cycle_start_counter",
    "toners.black.cycle_start_level",
    "toners.black.cycle_status",
    "toners.black.device_capacity_pages",
    "toners.black.index",
    "toners.black.pages_after_zero",
    "toners.black.percent_source",
    "toners.black.raw_level",
    "toners.black.raw_name",
    "toners.black.supply_present",
    "toners.black.unit",
    "toners.black.unit_code",
    "toners.black.usage",
    "toners.black.usage_m",
    "toners.black.yield_cartridge_key",
    "toners.black.yield_confidence",
    "toners.black.yield_per_page",
    "toners.black.yield_sample_count",
    "toners.black.yield_total_weight",
    "toners.cyan.capacity_pages",
    "toners.cyan.capacity_source",
    "toners.cyan.cycle_start_counter",
    "toners.cyan.cycle_start_level",
    "toners.cyan.cycle_status",
    "toners.cyan.device_capacity_pages",
    "toners.cyan.index",
    "toners.cyan.pages_after_zero",
    "toners.cyan.percent_source",
    "toners.cyan.raw_level",
    "toners.cyan.raw_name",
    "toners.cyan.supply_present",
    "toners.cyan.unit",
    "toners.cyan.unit_code",
    "toners.cyan.usage",
    "toners.cyan.usage_m",
    "toners.cyan.yield_cartridge_key",
    "toners.cyan.yield_confidence",
    "toners.cyan.yield_per_page",
    "toners.cyan.yield_sample_count",
    "toners.cyan.yield_total_weight",
    "toners.magenta.capacity_pages",
    "toners.magenta.capacity_source",
    "toners.magenta.cycle_start_counter",
    "toners.magenta.cycle_start_level",
    "toners.magenta.cycle_status",
    "toners.magenta.device_capacity_pages",
    "toners.magenta.index",
    "toners.magenta.level",
    "toners.magenta.max",
    "toners.magenta.name",
    "toners.magenta.pages_after_zero",
    "toners.magenta.percent_source",
    "toners.magenta.raw_level",
    "toners.magenta.raw_name",
    "toners.magenta.remaining",
    "toners.magenta.status",
    "toners.magenta.supply_present",
    "toners.magenta.unit",
    "toners.magenta.unit_code",
    "toners.magenta.usage",
    "toners.magenta.usage_m",
    "toners.magenta.yield_cartridge_key",
    "toners.magenta.yield_confidence",
    "toners.magenta.yield_per_page",
    "toners.magenta.yield_sample_count",
    "toners.magenta.yield_total_weight",
    "toners.yellow.capacity_pages",
    "toners.yellow.capacity_source",
    "toners.yellow.cycle_start_counter",
    "toners.yellow.cycle_start_level",
    "toners.yellow.cycle_status",
    "toners.yellow.device_capacity_pages",
    "toners.yellow.index",
    "toners.yellow.level",
    "toners.yellow.max",
    "toners.yellow.name",
    "toners.yellow.pages_after_zero",
    "toners.yellow.percent_source",
    "toners.yellow.raw_level",
    "toners.yellow.raw_name",
    "toners.yellow.remaining",
    "toners.yellow.status",
    "toners.yellow.supply_present",
    "toners.yellow.unit",
    "toners.yellow.unit_code",
    "toners.yellow.usage",
    "toners.yellow.usage_m",
    "toners.yellow.yield_cartridge_key",
    "toners.yellow.yield_confidence",
    "toners.yellow.yield_per_page",
    "toners.yellow.yield_sample_count",
    "toners.yellow.yield_total_weight",
    "trays.0.fill_percent",
    "trays.0.index",
    "trays.1.fill_percent",
    "trays.1.index",
    "trays.2.capacity",
    "trays.2.fill_percent",
    "trays.2.index",
    "trays.2.level",
    "trays.2.name",
    "trays.2.status",
})

#: ۸ برگِ هشدارهای مصنوعی که در سناریوی کارتریج خالی (enhanced_02) توسط legacy ساخته می‌شوند.
SYNTHESIZED_ALERT_PATHS = frozenset({
    "alerts.0.code",
    "alerts.0.message",
    "alerts.1.code",
    "alerts.1.message",
    "alerts.2.code",
    "alerts.2.message",
    "alerts.3.code",
    "alerts.3.message",
})


# ── ۲) توابع کمکی hermetic (Exposition Generator + Fake HTTP) ─────────────

def _esc_prom(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def make_exposition_from_spec(module: str, oids: dict) -> str:
    """تولید خروجی متن Prometheus بر اساس ساختار واقعی `monitoring/snmp.yml`."""
    lines = []
    module_spec = MODS.get(module, {})
    for met in module_spec.get("metrics", []):
        oid = met["oid"]
        if oid not in oids:
            continue
        v = oids[oid]
        name = met["name"]
        typ = met.get("type", "gauge")
        kind = "counter" if typ == "counter" else "gauge"
        lines.append(f"# HELP {name} PrintGuard {module}: {name}")
        lines.append(f"# TYPE {name} {kind}")
        if typ == "DisplayString" or isinstance(v, str):
            lines.append(f'{name}{{{name}="{_esc_prom(v)}"}} 1')
        else:
            lines.append(f"{name} {v}")
    return "\n".join(lines) + ("\n" if lines else "")


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.status = 200

    def getcode(self):
        return self.status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeExporterHttp:
    """`open_fn` ایزوله برای شبیه‌سازی snmp_exporter بر اساس داده‌های fixture."""

    def __init__(self, per_module: dict[str, str]):
        self.per_module = per_module
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float | None = None):
        self.calls.append(url)
        m = re.search(r"[?&]module=([^&]+)", url)
        if m is None:
            return _FakeResp("snmp_exporter\n")
        return _FakeResp(self.per_module.get(m.group(1), ""))


def _load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _run_exporter_for_fixture(fixture: dict) -> tuple[dict | None, FakeExporterHttp]:
    printer = fixture["input"]["printer"]
    phase = fixture["input"]["phases"][0]
    dev_map = phase.get("devices") or {}
    oids: dict = {}
    for dev in dev_map.values():
        oids.update(dev.get("oids") or {})
    per_module = {m: make_exposition_from_spec(m, oids) for m in MODS}
    fake_http = FakeExporterHttp(per_module)
    client = ExporterClient("http://127.0.0.1:9116", open_fn=fake_http)
    ip = phase.get("ip") or printer["ip"]
    brand = (printer.get("brand") or "").lower()
    res = collect_via_exporter(client, ip, printer["name"], brand, start=1000.0)
    return res, fake_http


# ── ۳) تست‌های سطح ۱: تطابق برگ‌های قابل‌استخراج و طبقه‌بندی ──────────────────

def test_leaf_classification_inventory_is_consistent():
    """بررسی جامعیت و عدم هم‌پوشانی دسته‌های ۵‌گانه برگ‌ها."""
    assert len(EXTRACTABLE_SNMP_LEAVES_HP_COLOR) == 30
    assert len(VOLATILE_PATHS) == 2
    assert len(FIXTURE_GAPS) == 2
    assert len(LEGACY_ENRICHMENT_PATHS) == 126
    assert len(SYNTHESIZED_ALERT_PATHS) == 8

    # هیچ دو دسته‌ای نباید اشتراک داشته باشند
    categories = [
        EXTRACTABLE_SNMP_LEAVES_HP_COLOR,
        VOLATILE_PATHS,
        FIXTURE_GAPS,
        LEGACY_ENRICHMENT_PATHS,
        SYNTHESIZED_ALERT_PATHS,
    ]
    for i in range(len(categories)):
        for j in range(i + 1, len(categories)):
            overlap = categories[i] & categories[j]
            assert not overlap, f"تداخل بین دسته {i} و {j}: {overlap}"


@pytest.mark.parametrize("fx_name", [
    "enhanced_01_hp_color_toner_full",
    "enhanced_02_hp_color_toner_empty",
])
def test_exporter_path_matches_extractable_golden_leaves(fx_name):
    """هر ۳۰ برگِ قابل‌استخراج در مسیر exporter دقیقاً و بدونِ تفاضل برابر با golden است."""
    fixture = _load_fixture(fx_name)
    exporter_result, _ = _run_exporter_for_fixture(fixture)
    assert exporter_result is not None

    golden_return = fixture["expected"]["steps"][0]["return_value"]
    exporter_flat = flatten(exporter_result)
    golden_flat = flatten(golden_return)

    matched_count = 0
    diffs = []
    for leaf in sorted(EXTRACTABLE_SNMP_LEAVES_HP_COLOR):
        assert leaf in exporter_flat, f"برگِ {leaf} در خروجی exporter یافت نشد"
        assert leaf in golden_flat, f"برگِ {leaf} در golden یافت نشد"
        exp_val = exporter_flat[leaf]
        gold_val = golden_flat[leaf]
        if exp_val != gold_val:
            diffs.append((leaf, gold_val, exp_val))
        else:
            matched_count += 1

    assert not diffs, f"اختلاف در برگ‌های قابل استخراج: {diffs}"
    assert matched_count == 30, f"انتظار ۳۰ برگ منطبق، اما {matched_count} تطابق یافت شد"


@pytest.mark.parametrize("fx_name,expected_legacy_set", [
    ("enhanced_01_hp_color_toner_full", LEGACY_ENRICHMENT_PATHS),
    ("enhanced_02_hp_color_toner_empty", LEGACY_ENRICHMENT_PATHS | SYNTHESIZED_ALERT_PATHS),
])
def test_unclassified_leaves_do_not_exist(fx_name, expected_legacy_set):
    """هیچ برگِ طبقه‌بندی‌نشده‌ای در golden یا خروجی exporter نباید وجود داشته باشد."""
    fixture = _load_fixture(fx_name)
    exporter_result, _ = _run_exporter_for_fixture(fixture)
    assert exporter_result is not None

    golden_return = fixture["expected"]["steps"][0]["return_value"]
    exporter_flat = flatten(exporter_result)
    golden_flat = flatten(golden_return)

    all_classified_golden = (
        EXTRACTABLE_SNMP_LEAVES_HP_COLOR
        | FIXTURE_GAPS
        | VOLATILE_PATHS
        | expected_legacy_set
    )
    unclassified_golden = set(golden_flat.keys()) - all_classified_golden
    assert not unclassified_golden, f"برگ‌های طبقه‌بندی‌نشده در golden: {unclassified_golden}"

    all_classified_exporter = (
        EXTRACTABLE_SNMP_LEAVES_HP_COLOR
        | FIXTURE_GAPS
        | VOLATILE_PATHS
    )
    unclassified_exporter = set(exporter_flat.keys()) - all_classified_exporter
    assert not unclassified_exporter, f"برگ‌های طبقه‌بندی‌نشده در exporter: {unclassified_exporter}"

    # هیچ کلید اضافه‌ای در خروجی exporter بیرون از مجموعه کلیدهای golden وجود ندارد
    extra_exporter_leaves = set(exporter_flat.keys()) - set(golden_flat.keys())
    assert not extra_exporter_leaves, f"کلیدهای پیش‌بینی‌نشده در exporter: {extra_exporter_leaves}"


@pytest.mark.parametrize("fx_name", [
    "enhanced_01_hp_color_toner_full",
    "enhanced_02_hp_color_toner_empty",
])
def test_fixture_gaps_are_documented_and_evaluated(fx_name):
    """اثبات این‌که ۲ اختلاف شمارنده دقیقاً ناشی از نبود OIDهای اختصاصی HP در fixture هستند (TD-020)."""
    fixture = _load_fixture(fx_name)
    exporter_result, _ = _run_exporter_for_fixture(fixture)
    assert exporter_result is not None

    exporter_flat = flatten(exporter_result)
    golden_flat = flatten(fixture["expected"]["steps"][0]["return_value"])

    for gap_leaf in FIXTURE_GAPS:
        assert exporter_flat[gap_leaf] is None, f"{gap_leaf} در خروجی exporter باید None باشد"
        assert golden_flat[gap_leaf] is not None and golden_flat[gap_leaf] > 0, (
            f"{gap_leaf} در golden باید مقدار مثبت داشته باشد"
        )


@pytest.mark.parametrize("fx_name,brand", [
    ("enhanced_03_brother_toner_unknown", "brother"),
    ("enhanced_04_toshiba_offline_then_online", "toshiba"),
    ("enhanced_05_ecs100g_sensor", "sensor"),
])
def test_unsupported_brands_non_entry(fx_name, brand, caplog):
    """برای برندهای پشتیبانی‌نشده هیچ درخواستی به exporter ارسال نمی‌شود و هشدار مستند ثبت می‌شود."""
    fixture = _load_fixture(fx_name)
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        exporter_result, fake_http = _run_exporter_for_fixture(fixture)

    assert exporter_result is None
    assert fake_http.calls == []  # هیچ درخواست HTTP به exporter ارسال نشد

    warning_msgs = [r.message for r in caplog.records if r.name == "PrinterMonitor"]
    assert any(f"برندِ «{brand}» آینه‌سازی نشده" in msg for msg in warning_msgs), (
        f"هشدار مناسب برای برند {brand} ثبت نشد: {warning_msgs}"
    )


# ── ۴) تست‌های سطح ۲: مسیر کامل با Poller و پرچم USE_SNMP_EXPORTER=1 ─────────

def test_poller_collect_with_flag_on_uses_exporter_and_bypasses_snmp_sweep(monkeypatch):
    """در سطح Poller با پرچم روشن: مسیر exporter موفق شده و OID sweep کلس کلکتور کاملاً bypass می‌شود."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    monkeypatch.setattr(poller, "_exporter_client_cache", None, raising=False)

    fixture = _load_fixture("enhanced_01_hp_color_toner_full")
    phase = fixture["input"]["phases"][0]
    dev_map = phase.get("devices") or {}
    oids: dict = {}
    for dev in dev_map.values():
        oids.update(dev.get("oids") or {})

    per_module = {m: make_exposition_from_spec(m, oids) for m in MODS}
    fake_http = FakeExporterHttp(per_module)
    client = ExporterClient("http://127.0.0.1:9116", open_fn=fake_http)
    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: client)

    transport = build_transport(fixture)
    printer = dict(fixture["input"]["printer"])

    with tempfile.TemporaryDirectory() as tmpdir, isolated_db(tmpdir), frozen_time(), transport.installed() as t:
        # اجرای poll_one پرینتر با پرچم فعال
        poller.poll_one(dict(printer))

        # ۱) اعتبارسنجی فراخوانی‌های transport: فقط health probe اولیه اجرا شده (بدون OID sweep کلکتور)
        assert len(t.calls) <= 3, f"فراخوانی‌های بیش از حد health probe: {t.calls}"
        for call in t.calls:
            assert call["oid"] in (
                "1.3.6.1.2.1.1.1.0",
                "1.3.6.1.2.1.1.3.0",
                "1.3.6.1.2.1.1.5.0",
            ), f"فراخوانی غیرمجاز SNMP sweep: {call}"

        # ۲) بررسی ذخیره‌سازی در store.printer_data و الحاق فیلدهای nickname و device_type
        stored = store.printer_data.get(printer["ip"])
        assert stored is not None
        assert stored["nickname"] == "HP-رنگی"
        assert stored["device_type"] == "color"
        assert stored["counters"]["total"] == 84210
        assert stored["toners"]["black"]["level"] == 78

        # ۳) اعتبارسنجی رفتار مورد انتظار در دیتابیس: صفر ردیف در printer_counters (طراحی G.5)
        db_state = snapshot_db()
        assert db_state.get("printer_counters") == []
        assert db_state.get("toner_snapshots_v2") == []
        assert db_state.get("cartridge_state") == []


def test_poller_collect_with_flag_on_falls_back_for_unsupported_brands(monkeypatch):
    """با پرچم روشن، برای برندهای غیرپشتیبانی poller بی‌صدا و بدون خطا به مسیر legacy برمی‌گردد."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    monkeypatch.setattr(poller, "_exporter_client_cache", None, raising=False)

    fixture = _load_fixture("enhanced_03_brother_toner_unknown")
    transport = build_transport(fixture)
    http = FakeHttp([tuple(x) for x in fixture["input"].get("http_responses", [])])
    printer = dict(fixture["input"]["printer"])

    with tempfile.TemporaryDirectory() as tmpdir, isolated_db(tmpdir), frozen_time(), transport.installed() as t, http.installed():
        res = poller.collect(dict(printer))
        assert res is not None
        assert res["brand"] == "brother"
        assert res["counters"]["total"] == 25431
        # در fallback به legacy، فراخوانی‌های SNMP کامل انجام می‌شوند
        assert len(t.calls) > 20
