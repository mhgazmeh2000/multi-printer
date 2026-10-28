# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_exporter_adapter.py
# @brief:        Tests for the snmp_exporter-to-collector adapter (step G.5.3).
# @details:      Responsibilities: Check metric mapping for each brand.
#                Main API: _FakeClient, 37 test functions
#                Side effects: network I/O
# @dependencies: internal: core, core.collectors, core.collectors.canon, core.collectors.hp,
#                core.enhanced_collector, core.exporter_client | external: pytest
# @notes:        None.
# =============================================================================

"""
`core/collectors/exporter_adapter.py` tests (step G.5.3).

The data is the handwritten fixture (`tests/fixtures/exporter_response_*.txt`) and the expectations
They are explicit numbers — not a recalculation of the adapter formula, otherwise the test would only validate itself.
The same "explicit number" measures the mirroring of formulas with `collect_hp`/`collect_canon`:

  1) output keys = return keys of the collectors themselves (from AST, not from memory);
  2) counters/toner/tray/warning with the same thresholds and the same defaults;
  3) `ignore_values` exactly the same 31 metrics as `monitoring/snmp.yml` and not one more/less;
  4) None routes: unmirrored brand, empty response, exporter error — and none
     The side effect (event/DB/web) is not in the adapter.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.collectors import exporter_adapter as ea
from core.exporter_client import ExporterError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SNMP_YML = ROOT / "monitoring" / "snmp.yml"


def metrics_for(*names: str) -> dict:
    """Helper: metrics for."""
    from core.exporter_client import parse_prometheus_text

    merged: dict = {}
    for name in names:
        merged.update(parse_prometheus_text((FIXTURES / name).read_text(encoding="utf-8")))
    return merged


@pytest.fixture(autouse=True)
def no_web_by_default(monkeypatch):
    """Web scraps are useless *throughout* this file, unless a test explicitly replaces them.

    Without this, any full path test (collect_via_exporter) would call 172.16.0.5 —
    That is, the offline repo suite was dependent on the network.
    """
    originals = {"hp": ea._scrape_hp_toners, "canon": ea._scrape_canon_toners,
                  "key": ea._hp_toner_key}      # The identity test requires these
    calls = []
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda ip, timeout=4.0: calls.append(("hp", ip)) or None)
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda ip, timeout=3.0: calls.append(("canon", ip)) or None)
    ea._WARNED_UNSUPPORTED.clear()          # A "one time" alert should not leak between tests
    originals["calls"] = calls
    return originals


@pytest.fixture()
def hp_metrics():
    """Helper: hp metrics."""
    return metrics_for("exporter_response_sample.txt", "exporter_response_std.txt")


@pytest.fixture()
def canon_metrics():
    """Helper: canon metrics."""
    return metrics_for("exporter_response_canon.txt", "exporter_response_canon_std.txt")


def _returned_keys(collector_file: str, func_name: str) -> set[str]:
    """return dict keys of a successful path of a collector — from AST, without import."""
    tree = ast.parse((ROOT / "core" / "collectors" / collector_file).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, f"{func_name} ساختارِ try/except ندارد"
    last = tries[0].body[-1]
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Dict)
    return {k.value for k in last.value.keys}


# ── 1) Fig.
def test_hp_shape_has_exactly_the_collect_hp_keys(hp_metrics):
    """Verifies hp shape has exactly the collect hp keys."""
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics, name="HP-05")
    assert shape is not None
    assert set(shape) == _returned_keys("hp.py", "collect_hp")
    assert set(shape["counters"]) == {"total", "full_color", "black_white", "printer", "copy", "fax"}
    assert shape["brand"] == "hp" and shape["online"] is True
    assert shape["name"] == "HP-05" and shape["paper_sizes"] == {}


def test_canon_shape_has_exactly_the_collect_canon_keys(canon_metrics):
    """Verifies canon shape has exactly the collect canon keys."""
    shape = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics, name="iR")
    assert set(shape) == _returned_keys("canon.py", "collect_canon")
    assert shape["brand"] == "canon"


def test_name_falls_back_to_ip_and_poll_ms_is_zero_without_start(hp_metrics):
    """Verifies name falls back to ip and poll ms is zero without start."""
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)
    assert shape["name"] == "172.16.0.5" and shape["poll_ms"] == 0
    assert shape["last_poll"].endswith("Z")            # Repository timestamp contract


# ── 2) Numbers
def test_hp_counters_are_the_fixture_arithmetic(hp_metrics):
    """Verifies hp counters are the fixture arithmetic."""
    counters = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["counters"]
    # bro_total 12345 · hp_print_color 1200 → black_white = 12345 - 1200 (hp formula)
    assert counters["total"] == 12345
    assert counters["full_color"] == 1200
    assert counters["black_white"] == 11145
    assert counters["printer"] == 12345
    assert counters["copy"] == 930          # hp_copy_mono 900 + hp_copy_color 30
    assert counters["fax"] is None          # hp_fax is not responding ⇒ default is -1 ⇒ None


def test_hp_device_strings_and_uptime(hp_metrics):
    """Verifies hp device strings and uptime."""
    device = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["device"]
    assert device == {
        "model": "HP LaserJet Pro M402dn",
        "serial": "CNCVC0245A",
        "firmware": "20190418",
        "uptime_str": "1d 01:40",          # 9245678/100 = 92456 s
    }


def test_hp_toner_percent_and_status(hp_metrics):
    """Verifies hp toner percent and status."""
    toners = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["toners"]
    assert toners["black"] == {
        "level": 42, "status": "ok", "name": "Black Toner", "remaining": 1344, "max": 3200,
    }


def test_hp_drum_slot_uses_the_generic_key(hp_metrics):
    """Slot 2 named "Imaging Drum" does not receive any color ⇒ `toner_2` key.

    This test previously pinned the buggy behavior (TD-016: substring matching, "m" in
    "iMaging" ⇒ `magenta'). By fixing `_hp_toner_key` in `core/collectors/hp.py`, expect
    It was changed and it keeps the **property of being a mirror** considered: the adapter function of the same collector
    imports, so every change there is immediately reflected here, and this test is the same
    It turns red so that the two paths do not move separately. `tests/test_hp_toner_key.py` rule
    It locks in the collector itself.
    """
    toners = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["toners"]
    assert toners["toner_2"] == {
        "level": None, "status": "unknown", "name": "Imaging Drum", "remaining": -2, "max": 100,
    }
    assert "magenta" not in toners and "cyan" not in toners


def test_hp_trays_status_thresholds(hp_metrics):
    """Verifies hp trays status thresholds."""
    trays = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["trays"]
    assert trays == [
        {"name": "Tray 1", "level": 130, "capacity": 250, "status": "medium"},   # 52 %
        {"name": "Manual Feed Tray", "level": -9, "capacity": 150, "status": "empty"},
    ]


def test_canon_counters_and_mf_precedence(canon_metrics):
    """Verifies canon counters and mf precedence."""
    counters = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics)["counters"]
    # canon_total (MF) is preferred over bro_total; copy/printer from the MF branch
    assert counters["total"] == 12000
    assert counters["printer"] == 9000            # canon_print
    assert counters["copy"] == 2500               # canon_copy
    assert counters["full_color"] == 4000         # bro_color (< total ⇒ valid)
    assert counters["black_white"] == 8000        # bw_std
    assert counters["fax"] is None                # Canon does not have it in legacy


def test_canon_toners_and_open_cover_alert(canon_metrics):
    """Verifies canon toners and open cover alert."""
    shape = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics)
    assert shape["toners"]["black"]["level"] == 62 and shape["toners"]["black"]["status"] == "ok"
    assert shape["toners"]["cyan"]["level"] == 18 and shape["toners"]["cyan"]["status"] == "low"
    assert shape["toners"]["magenta"]["source"] == "snmp_unavailable"
    assert shape["toners"]["magenta"]["level"] is None
    assert shape["trays"] == [{"name": "Cassette 1", "level": 500, "capacity": 700,
                               "status": "medium"}]           # tray2_* was not there ⇒ skip
    assert shape["alerts"] == [{"message": "درب پرینتر باز است", "code": 3}]
    assert shape["device"]["uptime_str"] == "2d 00:00"        # 17280000/100 = 172800


def test_canon_lbp_path_when_no_mf_counters(canon_metrics):
    """Without canon_total/alt_total, the LBP branch comes: total = canon_lbp_total."""
    del canon_metrics["canon_total"]
    del canon_metrics["canon_copy"]
    del canon_metrics["canon_print"]
    del canon_metrics["canon_scan"]
    counters = ea.exporter_to_collector_shape("172.16.0.9", "canon", canon_metrics)["counters"]
    assert counters["total"] == 9000
    assert counters["printer"] == 9000 and counters["copy"] is None
    assert counters["full_color"] == 400           # canon_lbp_color < total
    assert counters["black_white"] == 8000


# 3) ignore_values
def _ignored_names_in_spec() -> set[str]:
    """Helper: ignored names in spec."""
    lines = SNMP_YML.read_text(encoding="utf-8").split("\n")
    names, current = set(), None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- name:"):
            current = stripped.split("- name:")[1].strip()
        elif "ignore_values" in stripped and current:
            names.add(current)
    return names


def test_ignore_values_set_is_exactly_the_31_spec_metrics():
    """Verifies ignore values set is exactly the 31 spec metrics."""
    from_spec = _ignored_names_in_spec()
    assert ea.IGNORED_METRICS == from_spec, (
        f"کم: {sorted(from_spec - ea.IGNORED_METRICS)} · اضافه: "
        f"{sorted(ea.IGNORED_METRICS - from_spec)}")
    assert len(from_spec) == 31


def test_ignored_sentinel_falls_back_to_the_collector_default(hp_metrics):
    # toner_remain_2 = -2 with ignore comment ⇒ the same default as collect_hp (-2) ⇒ unknown
    """Verifies ignored sentinel falls back to the collector default."""
    toners = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)["toners"]
    assert toners["toner_2"]["status"] == "unknown"
    # general_oids = -2 ⇒ does not appear anywhere in the figure (not 0, not -2)
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", hp_metrics)
    assert "general_oids" not in str(shape["counters"])


def test_sentinel_on_unmarked_metric_is_kept(hp_metrics):
    """a `-2` on a metric without `# ignore_values:` is a value, not a gap."""
    assert ea._int(hp_metrics, "toner_remain_2", -99) == -99    # marked: dropped ⇒ default
    marked_free = {"hp_copy_mono": [({}, -2.0)]}
    assert ea._int(marked_free, "hp_copy_mono", -1) == -2       # unmarked: kept
    assert ea._int({"bro_total": [({}, float("nan"))]}, "bro_total", 7) == 7


def test_documented_divergence_hp_no_sensor_becomes_empty(hp_metrics):
    """The only conscious divergence (doc §9): prtInputCurrent = -2.

    collect_hp takes `no_sensor` from live OID; Here is the metric sentinel
    tray*_level (which is ignored in spec) is discarded and default is -9 ⇒ `empty`.
    If this one matters, remove `tray1_level`/`tray2_level` from IGNORED_METRICS —
    And this test intentionally pins the same so that silent change is not possible.
    """
    metrics = dict(hp_metrics)
    metrics["tray1_level"] = [({}, -2.0)]
    tray = ea.exporter_to_collector_shape("172.16.0.5", "hp", metrics)["trays"][0]
    assert tray["status"] == "empty" and tray["level"] == -9


# 4) None routes
def test_unsupported_brands_and_empty_answers_yield_none():
    """Verifies unsupported brands and empty answers yield none."""
    assert ea.exporter_to_collector_shape("1.1.1.1", "toshiba", {"tsh_total": [({}, 5.0)]}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "brother", {}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "sensor", {"sys_uptime": [({}, 1.0)]}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "hp", {}) is None          # Blank answer
    assert ea.exporter_to_collector_shape("1.1.1.1", "hp", {"unrelated": [({}, 1.0)]}) is None
    assert ea.exporter_to_collector_shape("1.1.1.1", "", {"bro_total": [({}, 1.0)]}) is None
    assert ea.SUPPORTED_BRANDS == ("hp", "canon")


def test_poll_ms_is_measured_when_start_given(hp_metrics):
    """Verifies poll ms is measured when start given."""
    import time
    shape = ea.exporter_to_collector_shape("1.1.1.1", "hp", hp_metrics, start=time.time() - 0.2)
    assert 0 < shape["poll_ms"] < 5000


class _FakeClient:
    """Fake client: only implements query_brand (adapter contract)."""

    def __init__(self, metrics=None, exc=None):
        """Initialize the instance."""
        self._metrics, self._exc = metrics, exc
        self.calls: list[tuple] = []

    def query_brand(self, ip, brand, auth="public_v2"):
        """Helper: query brand."""
        self.calls.append((ip, brand, auth))
        if self._exc is not None:
            raise self._exc
        return self._metrics


@pytest.mark.parametrize("exc", [ExporterError("target unreachable"), TimeoutError("slow")])
def test_collect_via_exporter_turns_every_error_into_none(exc, hp_metrics):
    """Verifies collect via exporter turns every error into none."""
    client = _FakeClient(exc=exc)
    assert ea.collect_via_exporter(client, "1.1.1.1", "x", "hp") is None
    assert len(client.calls) == 1        # One attempt, without a retry loop


def test_collect_via_exporter_skips_unsupported_brand_without_network(hp_metrics):
    """Verifies collect via exporter skips unsupported brand without network."""
    client = _FakeClient(hp_metrics)
    assert ea.collect_via_exporter(client, "1.1.1.1", "x", "toshiba") is None
    assert client.calls == []                       # No query has been sent


def test_collect_via_exporter_success_and_missing_signal(hp_metrics):
    """Verifies collect via exporter success and missing signal."""
    client = _FakeClient(hp_metrics)
    shape = ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "HP")
    assert shape["counters"]["total"] == 12345 and client.calls == [
        ("172.16.0.5", "HP", "public_v2")]
    assert ea.collect_via_exporter(_FakeClient({}), "172.16.0.5", "HP-05", "hp") is None


# ── 8) Step H.1 — Toner web scrap in the path of the exporter
def _hp_without_toner_numbers(hp_metrics):
    """SNMP "is present but does not give a number": `toner_remain_1 = -2` is the sentinel of `ignore_values`.

    We don't *remove* the record for slot 1 on purpose: then `collect_hp' will completely replace the `black' key.
    doesn't build and the branch of "creating new toner" is tested, not the branch of "filling empty level" which
    The legacy rule is more sensitive on it (status thresholds flow there).
    """
    m = dict(hp_metrics)
    m["toner_remain_1"] = [({}, -2.0)]
    return m


def test_adapter_uses_the_real_scrapers_not_copies(no_web_by_default):
    """"reuse" means the same function, not a second copy — owner's decision for H.1: **zero copies of scrapers**.

    This identity pin holds the entire H.1 decision: until our `_scrape_hp_toners` *own*
    is `core.collectors.hp', any EWS scraper modifications to the collector will automatically go to the path
    Exporter also arrives. (The opposite is also true: if one day someone posts an independent copy here,
    It breaks the test.) You can't close the merge/trigger rule like this - they are in
    are `core/enhanced_collector.py`, whose import means pulling `core.store`/`core.database`
    to the exporter path; So we have a *text* pin for them (`test_merge_functions_are_faithful_copies', template TD-016)
    + *behavior* pin (`test_merge_and_missing_rules_match_enhanced_collector`). Two styles of locks for
    Two types of reproduction: *identity* lock for what is imported (scrapers), *text* lock for what
    Copied (two rules). Why are scrapers import and not copy? `_scrape_canon_toners` inside its body
    has `import requests` + `urllib.request.urlopen` and the G.5 gateway includes both names in the adapter
    has prohibited; Its "faithful" copy means breaking the gate, so either import (choose this step) or
    Rewrite transport that is no longer faithful. **Owner's final decision (step H.1): the same as import** —
    Domain table, reason for "deleg" and rejection of other options: `docs/phase-h-decisions.md` §H.1.
    """
    from core.collectors.canon import _scrape_canon_toners as real_canon
    from core.collectors.hp import _hp_toner_key, _scrape_hp_toners as real_hp

    # "Binded origin" is read before the fixture is patched
    orig = no_web_by_default
    assert orig["hp"] is real_hp
    assert orig["canon"] is real_canon
    assert orig["key"] is _hp_toner_key       # The color key is the same, not the second version


def test_merge_functions_are_faithful_copies():
    """"Faithful copy" means text — same as pattern TD-016 (`test_rule_matches_the_web_helper`).

    The two alternatives required for comparison are both *counted* and documented; Except for them, the text should be
    be exactly the same - and the purpose of the test is the "countability of the difference", not the overall tolerance:

    1. `raw_level`: The `collect_hp`/`collect_canon` format does not have this key (the
       is `collect_enhanced'). Lines that *only* read/write it are removed and
       Convert `.get("raw_level")` to `.get("level")` — the same match as in dox
       `_toner_level_missing`/`_merge_scraped_toners` This file is written.
    2. annotation: there `typing.Dict`, here `dict` - because `from typing import Dict` means
       The new import and G.5 import gateway is intentionally closed.

    If one day someone changes one of the two rules on one side, this test breaks; Behavioral tests
    (`test_merge_and_missing_rules_match_enhanced_collector`) the same through input/output
    They get - one for "twin text", one for "twin behavior".
    """
    import inspect
    import re as _re

    from core import enhanced_collector as ec

    RAW_BLOCK = 'if t.get("raw_level") is None:\n            t["raw_level"] = pct\n'

    def norm(fn):
        """Helper: norm."""
        src = inspect.getsource(fn)
        src = src.replace(RAW_BLOCK, "")                       # raw_level block in merge
        src = src.replace('"raw_level": pct, ', "")            # The raw_level key in the new dictionary
        src = src.replace('t["raw_level"] = pct\n', "")       # If someone has written separately from if
        src = src.replace('"raw_level"', '"level"')           # The condition "Do we have a number?" On the field of any shape
        src = _re.sub(r'""".*?"""', "", src, flags=_re.S)      # docstring
        src = _re.sub(r"(?m)^\s*#.*$", "", src)                # comment
        src = _re.sub(r"\bDict\b", "dict", src)                # annotation
        lines = [_re.sub(r"\s+", " ", ln).rstrip() for ln in src.split("\n")]
        return "\n".join(ln for ln in lines if ln.strip())

    for fname in ("_toner_level_missing", "_merge_scraped_toners"):
        mine, theirs = norm(getattr(ea, fname)), norm(getattr(ec, fname))
        if mine != theirs:
            diff = [f"  ours={a!r}\n  legacy={b!r}" for a, b in zip(mine.split("\n"), theirs.split("\n")) if a != b]
            raise AssertionError(f"{fname} دیگر کپیِ وفادار نیست:\n" + "\n".join(diff))
    # And these are two *separate* functions (not aliases) — otherwise the text comparison is pointless
    assert ea._toner_level_missing is not ec._toner_level_missing
    assert ea._merge_scraped_toners is not ec._merge_scraped_toners
    # merge is *really* consumed in the mirror (not an idle copy) — and only from the single `_merge_scraped`:
    # The runtime route should not itself call the rules branch by branch (decision of clause 1-3: dispatcher).
    run = inspect.getsource(ea.apply_toner_http_fallback)
    disp = inspect.getsource(ea._merge_scraped)
    assert "_merge_scraped(toners, brand, scraped)" in run, run
    assert "_merge_scraped_toners" not in run and "_prefer_scraped_canon_toners" not in run
    assert "_prefer_scraped_canon_toners" in disp and "_merge_scraped_toners" in disp


def test_hp_http_fallback_when_snmp_incomplete(monkeypatch, hp_metrics):
    """SNMP does not have a number for toner ⇒ Web Scrap, with its legacy merge rule."""
    seen = []

    def fake_scrape(ip, timeout=4.0):
        """Helper: fake scrape."""
        seen.append((ip, timeout))
        return {"black": 61, "cyan": 5, "_used": ["black"]}     # "_used" is metadata

    monkeypatch.setattr(ea, "_scrape_hp_toners", fake_scrape)
    metrics = _hp_without_toner_numbers(hp_metrics)
    client = _FakeClient(metrics)
    shape = ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "hp")

    assert seen == [("172.16.0.5", 4.0)], "timeout = پیش‌فرضِ خودِ collector (4.0)"
    assert ea.HP_HTTP_TIMEOUT == 4.0 and ea.CANON_HTTP_TIMEOUT == 3.0, "عددِ legacy، نه سلیقهٔ ما"
    t = shape["toners"]
    assert t["black"]["level"] == 61 and t["black"]["status"] == "ok"
    assert t["black"]["source"] == "hp_web"
    assert t["black"]["name"] == "Black Toner", "کلیدِ موجود نامِ خودش را نگه می‌دارد"
    assert t["black"]["remaining"] == -2 and t["black"]["max"] == 3200, "فیلدهایِ SNMP دست‌نخورده"
    # "Fresh toner" branch is exactly enhanced_collector: status still unknown and default remaining/max
    assert t["cyan"] == {"level": 5, "status": "unknown", "name": "cyan toner",
                         "remaining": -1, "max": -1, "source": "hp_web"}
    assert "_used" not in t and all("_used" not in v for v in t.values())
    # And no new keys are added to the output form (contract "12 collect_hp keys").
    assert set(shape) == _returned_keys("hp.py", "collect_hp")
    assert not any("raw_level" in v for v in t.values()), "raw_level مالِ شکلِ enhanced است"
    assert shape["counters"]["total"] == 12345             # SNMP numbers in place


def test_hp_no_http_call_when_snmp_has_numbers(monkeypatch, hp_metrics):
    """The toner has a number ⇒ no web request should go (extra cost on healthy device)."""
    def boom(ip, timeout=4.0):
        """Helper: boom."""
        raise AssertionError("وب‌اسکرپ نباید صدا زده شود")

    monkeypatch.setattr(ea, "_scrape_hp_toners", boom)
    client = _FakeClient(hp_metrics)
    shape = ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "hp")
    assert shape["toners"]["black"] == {"level": 42, "status": "ok", "name": "Black Toner",
                                        "remaining": 1344, "max": 3200}


def test_hp_drum_number_blocks_fallback_like_the_rule(monkeypatch, hp_metrics):
    """Aware regression: `_toner_level_missing` only exempts `drum`/`opc`;

    In the HP path, the drum number sits on the `toner_2' key (not `drum'), so "healthy drum" is like
    The legacy rule itself prevents fallback. This test locks the same behavior as if
    One day we wanted to exempt `toner_2', it should be *decision* not forgetting - Document:
    `docs/phase-h-decisions.md` §H.1 "Known Limitations".
    """
    def boom(ip, timeout=4.0):
        """Helper: boom."""
        raise AssertionError("با عددِ درام (toner_2=90) قاعدهٔ فعلی fallback نمی‌کند")

    monkeypatch.setattr(ea, "_scrape_hp_toners", boom)
    m = dict(hp_metrics)                     # In real fixture: slot 2 = not drum with 90%,
    m["toner_max_2"] = [({}, 100.0)]         # So we number the same and slot 1
    m["toner_remain_2"] = [({}, 90.0)]       # We keep sentinel (unnumbered).
    m["toner_remain_1"] = [({}, -2.0)]
    shape = ea.collect_via_exporter(_FakeClient(m), "172.16.0.5", "x", "hp")
    assert shape["toners"]["toner_2"]["level"] == 90
    assert shape["toners"]["black"]["level"] is None      # And the real toner is still unknown


def test_canon_http_fallback_when_snmp_absent(monkeypatch, canon_metrics, caplog):
    """canon with tonerless SNMP: all four keys remain `snmp_unavailable' unless web gives a number."""
    import logging

    caplog.set_level(logging.DEBUG, logger="PrinterMonitor")
    seen = []

    def fake_scrape(ip, timeout=3.0):
        """Helper: fake scrape."""
        seen.append((ip, timeout))
        return {"black": 55, "cyan": 12, "magenta": None}   # "None" means that the web does not have a number

    monkeypatch.setattr(ea, "_scrape_canon_toners", fake_scrape)
    m = dict(canon_metrics)
    for idx in (1, 2, 3, 4):
        for base in ("toner_name", "toner_max", "toner_remain"):
            m.pop(f"{base}_{idx}", None)
    shape = ea.collect_via_exporter(_FakeClient(m), "10.0.20.12", "C-12", "canon")

    assert seen == [("10.0.20.12", 3.0)], "collect_canon صریح timeout=3.0 می‌دهد"
    # "Succeeded" should be silent, not masked with an overarching `except': if ever the HTTP preference
    # Finish with a TypeError, the same debug log says.
    import logging

    assert caplog.records == [] or not [r for r in caplog.records if "انجام نشد" in r.getMessage()]
    t = shape["toners"]
    assert t["black"] == {"level": 55, "status": "ok", "name": "Black Toner",
                          "remaining": None, "max": 100, "source": "http_scrape"}
    assert t["cyan"]["level"] == 12 and t["cyan"]["status"] == "low", "آستانه‌هایِ canon (≤25 ⇒ low)"
    assert t["magenta"]["source"] == "snmp_unavailable" and t["magenta"]["level"] is None, \
        "مقدارِ None اولویتِ HTTP را بی‌اثر می‌کند (همان `if level is not None` در collect_canon)"
    assert set(shape) == _returned_keys("canon.py", "collect_canon")


def test_canon_divergence_from_the_collector_is_pinned(monkeypatch, canon_metrics):
    """*Selected* Divergence Guards canon mirror like HP, `collect_canon' does not.

    The program of step H.1 asked for the uniform rule ("Only when SNMP has no data or is incomplete"), so on
    healthy canon - which has SNMP numbers and Remote UI - the mirror does not web at all and SNMP numbers
    they stay; But `core/collectors/canon.py:337` hits the script there and prefers HTTP
    gives This behavioral difference is deliberately tested so that the "deficiency" does not turn into a silent bug, and until
    The return should be one line (`_toner_level_missing` guard from the canon branch in
    `apply_toner_http_fallback` is removed). Record format when the web *runs* still
    Same as `collect_canon`: `test_canon_http_fallback_when_snmp_absent`.
    """
    import inspect

    calls = []
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda ip, timeout=3.0: calls.append(ip) or {"black": 7})
    shape = ea.collect_via_exporter(_FakeClient(canon_metrics), "10.0.20.12", "C-12", "canon")
    assert calls == [], "SNMP عدد دارد ⇒ آینه وب نمی‌زند (قاعدهٔ یکنواختِ H.1)"
    assert shape["toners"]["black"]["source"] == "snmp"
    assert shape["toners"]["black"]["level"] == 62, "عددِ SNMPِ fixture دست‌نخورده می‌ماند"

    # Witness collector from its own *text* (not our claim): collect_canon has no guard
    from core.collectors.canon import collect_canon

    src = inspect.getsource(collect_canon)
    assert "_scrape_canon_toners(ip, timeout=3.0)" in src
    assert "_toner_level_missing" not in src, "collectorِ canon گاردِ «ناقص‌بودن» ندارد"


def test_fallback_keys_off_the_normalized_brand(monkeypatch, canon_metrics):
    """Regression of a bug that produced *the same step* and the gates didn't get: `shape["brand"]` not the raw argument.

    poller gives the brand from the DB column, and in the repository it is really ``HP-05''/``CANON''; If
    fallback compares with the same string, `"CANON"!= "canon"` and the canon web scraper silently shuts down
    It remains (SNMP is also incomplete, no one understands).
    """
    hits = []
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda ip, timeout=3.0: hits.append(ip) or {"black": 9})
    m = dict(canon_metrics)
    for idx in (1, 2, 3, 4):
        for base in ("toner_name", "toner_max", "toner_remain"):
            m.pop(f"{base}_{idx}", None)
    shape = ea.collect_via_exporter(_FakeClient(m), "10.0.20.12", "C-12", "CANON")
    assert hits == ["10.0.20.12"] and shape["toners"]["black"]["source"] == "http_scrape"


def test_fallback_never_raises_and_keeps_snmp_numbers(monkeypatch, hp_metrics):
    """The web did something wrong (connect refused, timeout, weird HTML) ⇒ snapshot doesn't die."""
    def explode(ip, timeout=4.0):
        """Helper: explode."""
        raise OSError("connection refused")

    monkeypatch.setattr(ea, "_scrape_hp_toners", explode)
    shape = ea.collect_via_exporter(_FakeClient(_hp_without_toner_numbers(hp_metrics)),
                                    "172.16.0.5", "HP-05", "hp")
    assert shape["toners"]["black"] == {"level": None, "status": "unknown",
                                         "name": "Black Toner", "remaining": -2, "max": 3200}, \
        "رکوردهایِ SNMP عیناً می‌مانند؛ فقط «از وب عدد گرفتیم» حذف می‌شود"
    assert shape["counters"]["total"] == 12345


def test_hp_sentinel_only_device_still_creates_missing_rows(monkeypatch, hp_metrics):
    """Complete deletion of slot 1 ⇒ `black` is not created at all (behavior of collect_hp) and merge it
    *builds* — with status=unknown of the rule itself, not the thresholds (faithful copy of the same branch)."""
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda ip, timeout=4.0: {"black": 44})
    m = dict(hp_metrics)
    for name in ("toner_name_1", "toner_max_1", "toner_remain_1"):
        m.pop(name, None)
    shape = ea.collect_via_exporter(_FakeClient(m), "172.16.0.5", "x", "hp")
    assert shape["toners"]["black"] == {"level": 44, "status": "unknown", "name": "black toner",
                                        "remaining": -1, "max": -1, "source": "hp_web"}


def test_pure_shape_function_stays_offline(monkeypatch, hp_metrics):
    """`exporter_to_collector_shape` remains offline — `tools/compare_snmp_sources.py`
    It uses the same function offline; If the fallback was included, the comparison of G.6 would be networked."""
    def boom(*a, **k):
        """Helper: boom."""
        raise AssertionError("تابعِ خالص نباید وب بزند")

    monkeypatch.setattr(ea, "_scrape_hp_toners", boom)
    monkeypatch.setattr(ea, "_scrape_canon_toners", boom)
    shape = ea.exporter_to_collector_shape("172.16.0.5", "hp", _hp_without_toner_numbers(hp_metrics))
    assert shape["toners"]["black"]["level"] is None      # No web, same SNMP numbers
    assert shape["toners"]["black"]["status"] == "unknown"


def _assert_does_not_enter_exporter_path(brand, monkeypatch, caplog):
    """Common body of three guards: no query, no web scrap; An explicit warning (and not a flood of logs).

    These three tests are not intentionally *fake success*: the H.1 domain is two brands and `_toners()`/`_trays()`
    For other brands, it has a different output shape, so the only honest claim is that "input."
    It does not become the exporter route. Registered border: TD-018.
    """
    import logging

    web_calls = []
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda *a, **k: web_calls.append("hp") or None)
    monkeypatch.setattr(ea, "_scrape_canon_toners",
                        lambda *a, **k: web_calls.append("canon") or None)
    client = _FakeClient({})
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        assert ea.collect_via_exporter(client, "10.0.20.9", "X-09", brand) is None
    assert client.calls == [], "برندِ آینه‌سازی‌نشده نباید از exporter بپرسد"
    assert web_calls == [], "نباید حتی وب‌اسکرپ هم اجرا شود"
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warns) == 1 and "آینه‌سازی نشده" in warns[0] and "TD-018" in warns[0]
    # The second cycle for the same (brand, ip): no duplicate warning (no query either)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        assert ea.collect_via_exporter(client, "10.0.20.9", "X-09", brand) is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_toshiba_does_not_enter_exporter_path(monkeypatch, caplog):
    """Toshiba: 63 lines of `oid:' in `monitoring/snmp.yml' are not mirrored ⇒ legacy path."""
    _assert_does_not_enter_exporter_path("toshiba", monkeypatch, caplog)


def test_brother_does_not_enter_exporter_path(monkeypatch, caplog):
    """Brother: 9 rows of `oid:` + EWS not mirrored ⇒ legacy path."""
    _assert_does_not_enter_exporter_path("brother", monkeypatch, caplog)


def test_sensor_does_not_enter_exporter_path(monkeypatch, caplog):
    """``sensor'' has no `toners' at all; Even so, he closes the guard *before every query*."""
    _assert_does_not_enter_exporter_path("sensor", monkeypatch, caplog)


def test_merge_scraped_delegates_per_brand():
    """`_merge_scraped` is only in merge and selects *only* (paragraphs 1-3 of the program).

    - `hp` → filling in empty fields + tag `hp_web` (and invalid SNMP number does not skip)
    - `canon` → embed HTTP record with `remaining=None`/`max=100`
    - Other brand → the same rule as HP; In `collect_via_exporter` it never gets here because
      `SUPPORTED_BRANDS' guard gives ``None'' earlier (three named guards get the same)
    """
    blank = {"level": None, "status": "unknown", "name": "Black Toner",
             "remaining": -1, "max": -1, "source": "snmp"}

    hp = {"black": dict(blank)}
    assert ea._merge_scraped(hp, "hp", {"black": 61}) == 1
    assert hp["black"]["level"] == 61 and hp["black"]["source"] == "hp_web"
    assert hp["black"]["remaining"] == -1, "HP فیلدهایِ دیگر را دست نمی‌زند"

    cn = {"black": dict(blank)}
    assert ea._merge_scraped(cn, "canon", {"black": 4}) == 1
    assert cn["black"] == {"level": 4, "status": "critical", "name": "Black Toner",
                           "remaining": None, "max": 100, "source": "http_scrape"}, \
        "canon رکورد را *جایگذاری* می‌کند، نه پر"

    other = {"black": dict(blank)}
    assert ea._merge_scraped(other, "unknown-brand", {"black": 12}) == 1
    assert other["black"]["source"] == "hp_web"


def test_merge_and_missing_rules_match_enhanced_collector():
    """The merge/trigger rule must not be separated from `core/enhanced_collector.py`.

    Instead of comparing the source text, the *behavior* of the two versions is measured on the same inputs —
    With the only allowed difference: our shape does not have a `raw_level'.
    """
    from core.enhanced_collector import _merge_scraped_toners as legacy_merge
    from core.enhanced_collector import _toner_level_missing as legacy_missing

    scraped = {"black": 61, "cyan": 5, "magenta": 0, "yellow": 100, "_used": ["black"], "drum": "x"}
    for ours_keys, legacy_keys in (
        ({"black": {"level": None, "status": "unknown", "name": "Black Toner", "remaining": -1,
                    "max": -1}},
         {"black": {"level": None, "raw_level": None, "status": "unknown", "name": "Black Toner",
                    "remaining": -1, "max": -1}}),
        ({"black": {"level": 30, "status": "ok", "name": "x", "remaining": 1, "max": 2}},
         {"black": {"level": 30, "raw_level": 30, "status": "ok", "name": "x", "remaining": 1,
                    "max": 2}}),
        ({}, {}),
        ({"drum": {"level": 90, "status": "ok"}}, {"drum": {"level": 90, "status": "ok"}}),
    ):
        mine, theirs = dict(ours_keys), dict(legacy_keys)
        n_mine = ea._merge_scraped_toners(mine, scraped, "hp_web")
        n_theirs = legacy_merge(theirs, scraped, "hp_web")
        assert n_mine == n_theirs, ours_keys
        assert set(mine) == set(theirs)
        for k in mine:
            for field in ("level", "status", "name", "remaining", "max", "source"):
                assert mine[k].get(field) == theirs[k].get(field), (ours_keys, k, field)
        # The condition "Do we have a number?" On the field of any shape
        assert ea._toner_level_missing(mine) == legacy_missing(theirs), ours_keys


def test_adapter_has_no_side_effects():
    """Event/DB is not in the *code* of this file; The web is only imported from two scrapers.

    The check is performed on the AST (not the silent text), because the docstring is intentionally named
    Returns `_counters_event` and explains why it is not called.

    Step H.1 changed one thing and we close it here: mirroring is now *allowed*
    WebScrap calls the device itself, but only through two functions imported from
    own collectors (`_scrape_hp_toners`/`_scrape_canon_toners`). That means no
    `urlopen`/`requests` in this file, no DB, no events — the same three G.5 "don'ts"
    They are in their place and the list of imports has only made room for one more collector module.
    """
    tree = ast.parse((ROOT / "core" / "collectors" / "exporter_adapter.py").read_text(encoding="utf-8"))
    called, imported, touched = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                called.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                called.add(fn.attr)
                touched.add(fn.value.id if isinstance(fn.value, ast.Name) else type(fn.value).__name__)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            touched.add(node.id)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned = {"_counters_event", "add_event", "yield_engine", "urlopen", "requests",
              "save_printers", "update_prev"}
    assert not (called & banned), called & banned
    assert not (touched & (banned | {"store", "database", "sqlite3"})), sorted(touched)
    for name in called | touched:
        assert not any(b in name for b in ("INSERT", "sqlite")), name
    assert imported <= {"__future__", "logging", "re", "time", "core.collectors.base",
                        "core.collectors.hp", "core.collectors.canon",
                        "core.exporter_client", "core.time_utils"}, imported
    # H.1: The web portal is *opened*, but only from these two doors; And both should really be called
    # (If fallback is ever silently removed, this is what it says).
    assert {"_scrape_hp_toners", "_scrape_canon_toners"} <= called, called
    assert not (called & {"fetch_first_web_page", "Request"}), called
