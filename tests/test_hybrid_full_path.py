# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_hybrid_full_path.py
# @brief:        Full-path tests with USE_SNMP_EXPORTER=1 against golden fixtures (step H.3).
# @details:      Responsibilities: Check the exporter-enabled path end to end.
#                Main API: _FakeResp, FakeExporterHttp, 7 test functions
#                Side effects: network I/O
# @dependencies: internal: core, core.collectors.exporter_adapter, core.exporter_client,
#                core.poller, tests.golden_harness, tests.golden_profiles,
#                tools.compare_snmp_sources | external: pytest, yaml
# @notes:        None.
# =============================================================================

"""Full path tests USE_SNMP_EXPORTER=1 against golden fixtures (step H.3).

This test measures the Extractable Leaves Contract at two levels:

  Level 1 — `collect_via_exporter' against golden fixtures:
    • 30 extractable pages of SNMP for HP color (zero difference in both full and empty states).
    • 2 volatile leaves (last_poll, poll_ms) that are dynamic.
    • 2 sheets of fixture gap (counters.full_color, counters.black_white) which OIDs of counters
      HP exclusives (11.2.3.9.6.1.1.*) are not present in the fixture (registered in TD-020).
    • 126 sheets of legacy richness for full mode (and 134 sheets for empty, including 8 synthesized warning keys).
    • Zero unclassified leaves (Unclassified) in both structures.
    Non-entry proof for unsupported brands (Brother, Toshiba, ECS100G)
      With zero exporter queries and registering exactly one warning for each brand.

  Level 2 — `poller.collect()` with flag `USE_SNMP_EXPORTER=1`:
    • For HP, no collector OID is read after a successful exporter response
      (The transport call sequence only includes the initial health probe).
    • `nickname' and `device_type' values ​​are appended to the output by the poller.
    • The output is stored in `store.printer_data`.
    • According to the G.5 design, the exporter route intentionally records no counter events and writes zero rows to the DB.
    • For unsupported brands, poller automatically by leaving the exporter path off
      It goes back to the legacy path.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import unittest
from pathlib import Path

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
    """Unittest Protocol: This file is not detected/executed by unittest."""
    return unittest.TestSuite()


ROOT = Path(__file__).resolve().parents[1]
MODS = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))["modules"]


# ── 1) leaf classification contracts

#: 30 sheets that can be extracted directly from standard SNMP MIBs for HP color.
#: These leaves have zero difference in both full (enhanced_01) and empty (enhanced_02) scenarios with golden.
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

#: 2 dynamic leaves that show poll execution time and delay (equivalent to VOLATILE_PATHS of G.6 tool).
VOLATILE_PATHS = frozenset({
    "last_poll",
    "poll_ms",
})

#: 2 pages of exclusive HP counters whose OIDs are (1.3.6.1.4.1.11.2.3.9.6.1.1.*)
#: They do not exist in the current fixtures and as a result they are equal to None in the output of the exporter (TD-020).
FIXTURE_GAPS = frozenset({
    "counters.full_color",
    "counters.black_white",
})

#: 126 pages of legacy richness for HP color in normal/full state (enhanced_01).
#: This data is generated from catalog, yield_engine and PrevStore and is not raw in MIB.
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

#: 8 sheets of artificial warnings generated by legacy in empty cartridge scenario (enhanced_02).
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


# ── 2) hermetic auxiliary functions (Exposure Generator + Fake HTTP) ─────────────

def _esc_prom(s: str) -> str:
    """Helper: esc prom."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def make_exposition_from_spec(module: str, oids: dict) -> str:
    """Generating Prometheus text output based on the actual monitoring/snmp.yml structure."""
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
    """Helper class: fake resp."""
    def __init__(self, body: str):
        """Initialize the instance."""
        self._body = body.encode("utf-8")
        self.status = 200

    def getcode(self):
        """Helper: getcode."""
        return self.status

    def read(self):
        """Helper: read."""
        return self._body

    def __enter__(self):
        """Enter the context and return the object."""
        return self

    def __exit__(self, *args):
        """Leave the context and release what was acquired."""
        return False


class FakeExporterHttp:
    """Isolated `open_fn` to simulate snmp_exporter based on fixture data."""

    def __init__(self, per_module: dict[str, str]):
        """Initialize the instance."""
        self.per_module = per_module
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float | None = None):
        """Call the object like a function."""
        self.calls.append(url)
        m = re.search(r"[?&]module=([^&]+)", url)
        if m is None:
            return _FakeResp("snmp_exporter\n")
        return _FakeResp(self.per_module.get(m.group(1), ""))


def _load_fixture(name: str) -> dict:
    """Helper: load fixture."""
    path = FIXTURE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _run_exporter_for_fixture(fixture: dict) -> tuple[dict | None, FakeExporterHttp]:
    """Helper: run exporter for fixture."""
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


# ── 3) Level 1 tests: matching of extractable and classified leaves ──────────────────

def test_leaf_classification_inventory_is_consistent():
    """Examining the comprehensiveness and non-overlapping of the 5 categories of leaves."""
    assert len(EXTRACTABLE_SNMP_LEAVES_HP_COLOR) == 30
    assert len(VOLATILE_PATHS) == 2
    assert len(FIXTURE_GAPS) == 2
    assert len(LEGACY_ENRICHMENT_PATHS) == 126
    assert len(SYNTHESIZED_ALERT_PATHS) == 8

    # No two categories should share
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
    """Every 30 extractable leaves in the exporter path is exactly equal to golden without any difference."""
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
    """There should be no unsorted leaves in the golden or exporter output."""
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

    # There are no additional keys in the output of the exporter outside of the set of golden keys
    extra_exporter_leaves = set(exporter_flat.keys()) - set(golden_flat.keys())
    assert not extra_exporter_leaves, f"کلیدهای پیش‌بینی‌نشده در exporter: {extra_exporter_leaves}"


@pytest.mark.parametrize("fx_name", [
    "enhanced_01_hp_color_toner_full",
    "enhanced_02_hp_color_toner_empty",
])
def test_fixture_gaps_are_documented_and_evaluated(fx_name):
    """Proof that the 2 counter differences are precisely due to missing HP specific OIDs in the fixture (TD-020)."""
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
    """For unsupported brands, no request is sent to the exporter and a document warning is recorded."""
    fixture = _load_fixture(fx_name)
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        exporter_result, fake_http = _run_exporter_for_fixture(fixture)

    assert exporter_result is None
    assert fake_http.calls == []  # No HTTP request was sent to the exporter

    warning_msgs = [r.message for r in caplog.records if r.name == "PrinterMonitor"]
    assert any(f"برندِ «{brand}» آینه‌سازی نشده" in msg for msg in warning_msgs), (
        f"هشدار مناسب برای برند {brand} ثبت نشد: {warning_msgs}"
    )


# ── 4) Level 2 tests: full path with Poller and flag USE_SNMP_EXPORTER=1 ─────────

def test_poller_collect_with_flag_on_uses_exporter_and_bypasses_snmp_sweep(monkeypatch):
    """At the poller level with the flag on: the exporter path is successful and the OID sweep of the collector class is completely bypassed."""
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
        # Run printer poll_one with enabled flag
        poller.poll_one(dict(printer))

        # 1) Validation of transport calls: only initial health probe is executed (no OID sweep collector)
        assert len(t.calls) <= 3, f"فراخوانی‌های بیش از حد health probe: {t.calls}"
        for call in t.calls:
            assert call["oid"] in (
                "1.3.6.1.2.1.1.1.0",
                "1.3.6.1.2.1.1.3.0",
                "1.3.6.1.2.1.1.5.0",
            ), f"فراخوانی غیرمجاز SNMP sweep: {call}"

        # 2) Checking storage in store.printer_data and appending nickname and device_type fields
        stored = store.printer_data.get(printer["ip"])
        assert stored is not None
        assert stored["nickname"] == "HP-رنگی"
        assert stored["device_type"] == "color"
        assert stored["counters"]["total"] == 84210
        assert stored["toners"]["black"]["level"] == 78

        # 3) validation of the expected behavior in the database: zero rows in printer_counters (design G.5)
        db_state = snapshot_db()
        assert db_state.get("printer_counters") == []
        assert db_state.get("toner_snapshots_v2") == []
        assert db_state.get("cartridge_state") == []


def test_poller_collect_with_flag_on_falls_back_for_unsupported_brands(monkeypatch):
    """With the flag on, the poller silently and error-free reverts to the legacy path for unsupported brands."""
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
        # In fallback to legacy, full SNMP calls are made
        assert len(t.calls) > 20
