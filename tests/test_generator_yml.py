# -*- coding: utf-8 -*-
"""
تست‌های `monitoring/generator.yml` (گام G.2).

تست ۲ گیتِ اصلی (`tools/check_oid_coverage.py`) را اجرا می‌کند؛ بقیه مواردی را می‌سنجند
که گیت *قاعده‌اش* را می‌داند ولی اسم/ساختار فایل را نه: ماژول‌ها، authها، جای
`ignore_values`، و اینکه یتیم‌ها فقط کامنت‌اند.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import yaml   # requirements-dev.txt — بدون آن گیت‌های این فایل معنا ندارد

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "monitoring" / "generator.yml"
CHECKER = ROOT / "tools" / "check_oid_coverage.py"
MATRIX_TOOL = ROOT / "tools" / "build_oid_matrix.py"

EXPECTED_MODULES = {"printer_std": 24, "printer_hp": 14, "printer_canon": 16,
                    "printer_brother": 9, "printer_toshiba": 63, "sensor_ecs100g": 10}
SENTINELS = ["-2", "-3"]
ENV_PLACEHOLDER = re.compile(r"^\$\{[A-Z0-9_]+\}$")


def _load_yaml() -> dict:
    return yaml.safe_load(GENERATOR.read_text(encoding="utf-8"))


def _matrix_modules() -> dict:
    """ماتریس را از *ابزار* می‌خواند (نه از سند) تا generator با خودِ کد سنجیده شود."""
    spec = importlib.util.spec_from_file_location("build_oid_matrix", MATRIX_TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build()


def _all_entries(cfg: dict) -> list:
    out = list(cfg.get("get") or [])
    for w in cfg.get("walk") or []:
        out.extend(w.get("metrics") or [])
    return out


def test_generator_is_valid_yaml_with_exactly_six_modules():
    doc = _load_yaml()
    assert isinstance(doc, dict), "generator.yml mapping نیست"
    assert doc.get("version") == 1
    modules = doc.get("modules") or {}
    assert set(modules) == set(EXPECTED_MODULES), f"ماژول‌ها: {sorted(modules)}"
    for name, expected in EXPECTED_MODULES.items():
        entries = _all_entries(modules[name])
        oids = {str(e["oid"]) for e in entries}
        assert len(oids) == expected, f"{name}: {len(oids)} ردیف، انتظار {expected}"


def test_coverage_gate_passes_and_reports_full_matrix():
    proc = subprocess.run([sys.executable, str(CHECKER), "--json"], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    import json
    report = json.loads(proc.stdout)
    assert report["ok"] is True and report["problems"] == []
    s = report["stats"]
    assert s["covered_oids"] == s["matrix_oids"] == sum(EXPECTED_MODULES.values())
    assert s["live_stray_oids"] == []
    assert s["orphans_commented"] == s["orphans_in_doc"] > 0
    assert s["ignore_values_actual"] == s["ignore_values_expected"] > 0
    assert s["auths"] == ["public_v1", "public_v2", "secure_v3"]


def test_every_oid_the_code_reads_is_in_the_generator_and_none_is_invented():
    modules, stats = _matrix_modules()
    doc = _load_yaml()
    generator_oids = {str(e["oid"]) for cfg in doc["modules"].values() for e in _all_entries(cfg)}
    matrix_oids = {r["oid"] for rows in modules.values() for r in rows}
    assert stats["unique_oids"] == len(matrix_oids) == len(generator_oids)
    assert matrix_oids - generator_oids == set(), "کد این‌ها را می‌خواند و generator ندارد"
    assert generator_oids - matrix_oids == set(), "generator OIDی بیرون از کد دارد"
    # تنها OIDِ مجازِ بیرونِ ماتریس، `subtree`هایند و باید پدرِ ردیف‌های خودشان باشند
    for mod, cfg in doc["modules"].items():
        covered = {str(x["oid"]) for w in (cfg.get("walk") or []) for x in (w.get("metrics") or [])}
        for w in cfg.get("walk") or []:
            sub = str(w["subtree"])
            assert sub not in matrix_oids or sub in covered, f"{mod}: subtree {sub} ردیف نیست"
            assert all(o == sub or o.startswith(sub + ".") for o in
                       {str(x["oid"]) for x in (w.get("metrics") or [])}), f"{mod}: {sub} پدر سطرهایش نیست"


def test_auths_are_the_three_expected_names_and_v3_holds_no_real_secrets():
    auths = _load_yaml()["auths"]
    assert set(auths) == {"public_v1", "public_v2", "secure_v3"}
    for name in ("public_v1", "public_v2"):
        assert auths[name]["community"] == "public"
        assert auths[name]["version"] == int(name.rsplit("_v", 1)[1])
    v3 = auths["secure_v3"]
    assert v3["version"] == 3 and v3["security_level"] == "authPriv"
    for key in ("username", "auth_passphrase", "priv_passphrase"):
        assert ENV_PLACEHOLDER.match(str(v3[key])), f"secure_v3.{key} placeholder نیست"
    # نام‌ها باید با قرارداد G.6 بخوانند: exporter_auth = f"public_{snmp_version}"
    assert {f"public_v{v}" for v in (1, 2)} <= set(auths)


def test_ignore_values_sit_only_on_numeric_sentinel_metrics():
    doc = _load_yaml()
    carried, seen_str = set(), set()
    for mod, cfg in doc["modules"].items():
        for e in _all_entries(cfg):
            name = str(e.get("name", ""))
            if e.get("type") == "DisplayString":
                seen_str.add(f"{mod}/{name}")
            if e.get("ignore_values"):
                assert list(e["ignore_values"]) == SENTINELS, f"{mod}/{name}"
                carried.add(f"{mod}/{name}")
    assert carried, "هیچ متریکی ignore_values ندارد"
    assert not (carried & seen_str), "ignore_values روی متریک رشته‌ای نشسته است"
    # متریک‌های سطح/درصدِ کاتالوگ (category=supply) باید sentinel‌ها را رد کنند
    modules, _stats = _matrix_modules()
    expected = set()
    for mod, rows in modules.items():
        cfg = doc["modules"].get(mod) or {}
        by_oid = {str(e["oid"]): e for e in _all_entries(cfg)}
        for r in rows:
            e = by_oid.get(r["oid"])
            if e is not None and r["category"] == "supply" and r["type"] != "str":
                expected.add(f"{mod}/{e['name']}")
    assert expected <= carried, f"کم‌کار گذاشته شده: {sorted(expected - carried)}"


def test_walk_entries_are_indexed_tables_and_lookups_come_from_the_matrix():
    doc = _load_yaml()
    modules, _stats = _matrix_modules()
    entries = [(m, w) for m, cfg in doc["modules"].items() for w in (cfg.get("walk") or [])]
    assert len(entries) == 7, (
        f"تعداد جدول walk: {len(entries)}، انتظار ۷ = std 2 · hp 2 · toshiba 2 · brother 1")
    for mod, w in entries:
        members = [str(x["oid"]) for x in (w.get("metrics") or [])]
        kinds = {r["oid"]: r["kind"] for r in modules[mod]}
        # تک‌سطری فقط وقتی مجاز است که خودِ کد آن را walk کند (پوششِ یک درخت، نه یک برگ)
        assert len(members) >= 2 or (len(members) == 1 and kinds.get(members[0]) == "walk"), (
            f"{mod}/{w.get('name')}: walk تک‌برگی است و در کد هم walk نیست")
        parts = [o.split(".") for o in members]
        common = []
        for tup in zip(*parts):
            if len(set(tup)) != 1:
                break
            common.append(tup[0])
        assert str(w["subtree"]) == ".".join(common), f"{mod}/{w.get('name')}: subtree ≠ LCP"
        for lk in w.get("lookups") or []:
            label_oid = str(lk["label_oid"])
            assert label_oid in {r["oid"] for r in modules[mod]}, \
                f"{mod}/{w.get('name')}: lookup {label_oid} ردیفِ ماتریس نیست"


def test_probe_only_orphans_are_comments_never_live_metrics():
    _modules, stats = _matrix_modules()
    orphans = set(stats["orphans"])
    assert orphans, "ماتریس یتیم ندارد؟!"
    text = GENERATOR.read_text(encoding="utf-8")
    commented = set(re.findall(r"^\s*#\s*(\d+(?:\.\d+){6,})\s+\u2190", text, re.M))
    assert orphans <= commented, f"یتیم‌های بی‌کامنت: {sorted(orphans - commented)}"
    live = {str(e["oid"]) for cfg in _load_yaml()["modules"].values() for e in _all_entries(cfg)}
    assert not (orphans & live), "یتیم به متریک زنده تبدیل شده است"
    # برادر: ۸ یتیمِ probe-only و ۹ ردیفِ زنده — عددِ تصمیم فاز
    bro_orph = {o for o in orphans if o.startswith("1.3.6.1.4.1.2435")}
    assert len(bro_orph) == 8 and len(_load_yaml()["modules"]["printer_brother"]["get"]) == 4
