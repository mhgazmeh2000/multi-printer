# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_build_snmp_yml.py
# @brief:        Tests for tools/build_snmp_yml.py (step G.4.1).
# @details:      Responsibilities: Check deterministic regeneration, --check behaviour and spec-
#                versus-file consistency.
#                Main API: 13 test functions
#                Side effects: runs external processes, file I/O
# @dependencies: external: pytest, yaml
# @notes:        None.
# =============================================================================

"""
`tools/build_snmp_yml.py' tests (step G.4.1).

The three things that are established here are what make snmp.yml "manual".
"Tastefulness" separates:

  1) **deterministic/idempotent** — two consecutive executions must be identical byte for byte and
     Reproduce no diff on the committed file (`--check' ⇒ rc 0). without this,
     Each reproduction means changing 853 lines for no reason.
  2) **v3 mapping** — AUTH_KEY_MAP/AUTH_VALUE_MAP should change the exact same three names;
     No more: the spec intentionally keeps the *generator* config form, and this decision in
     The comment above `auths:` has been registered.
  3) **zero invention and zero network** — the tool only writes spec OIDs and no code
     No download/network (phase G rule).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "build_snmp_yml.py"
GENERATOR = ROOT / "monitoring" / "generator.yml"
SNMP_YML = ROOT / "monitoring" / "snmp.yml"


def _tool():
    """Helper: tool."""
    spec = importlib.util.spec_from_file_location("build_snmp_yml", TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args: str) -> subprocess.CompletedProcess:
    """Helper: run."""
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


# ── 1) Stable output
def test_regeneration_is_deterministic_and_idempotent(tmp_path):
    """Verifies regeneration is deterministic and idempotent."""
    a, b = tmp_path / "a.yml", tmp_path / "b.yml"
    for out in (a, b):
        p = _run("--out", str(out))
        assert p.returncode == 0, p.stdout + p.stderr
    ta = a.read_text(encoding="utf-8")
    assert ta == b.read_text(encoding="utf-8"), "دو اجرای پیاپی فرق کرد ⇒ خروجی deterministic نیست"
    assert ta == SNMP_YML.read_text(encoding="utf-8"), (
        "خروجیِ ابزار با فایلِ commit‌شده نمی‌خواند؛ اجرا: python tools/build_snmp_yml.py")


def test_check_mode_passes_on_the_committed_file():
    """Verifies check mode passes on the committed file."""
    p = _run("--check")
    assert p.returncode == 0, (p.stdout + p.stderr)[:2000]
    assert "136" in p.stdout, "پیام باید شمارشِ پوشش را بگوید"


def test_check_mode_fails_on_a_stale_file(tmp_path):
    """Verifies check mode fails on a stale file."""
    stale = tmp_path / "snmp.yml"
    stale.write_text(SNMP_YML.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")
    p = _run("--check", "--out", str(stale))
    assert p.returncode == 1, "گیتِ کهنه‌بودن باید قرمز شود، نه سبز"
    assert "کهنه" in p.stdout, "پیامِ فارسیِ راهنما نیست"
    assert "---" in p.stdout and "+++" in p.stdout, "diff برای آدم چاپ نشده"


def test_check_mode_fails_when_spec_gained_a_row(tmp_path):
    """If a line is added to the spec, the file is out of date and the tool should say (not be silent)."""
    spec = yaml.safe_load(GENERATOR.read_text(encoding="utf-8"))
    spec["modules"]["sensor_ecs100g"]["get"].append(
        {"oid": "1.3.6.1.4.1.47206.999.0", "name": "brand_new_row", "type": "gauge",
         "source": "core/oid/catalog.py:12"})
    fake_spec = tmp_path / "generator.yml"
    fake_spec.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    out = tmp_path / "snmp.yml"
    p = _run("--out", str(out), "--generator", str(fake_spec))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "brand_new_row" in out.read_text(encoding="utf-8"), "ردیفِ تازه به فایل نیامده"
    p2 = _run("--check", "--out", str(SNMP_YML), "--generator", str(fake_spec))
    assert p2.returncode == 1, "با specِ تازه، فایلِ commit‌شده باید کهنه شمرده شود"


def test_report_numbers_are_the_published_counts():
    """Verifies report numbers are the published counts."""
    mod = _tool()
    p = _run("--report")
    assert p.returncode == 0, p.stdout + p.stderr
    nums = {k: int(v) for k, v in re.findall(r"(\w+)=(-?\d+)", p.stdout)}
    assert nums == {"rows": 136, "modules": 6, "metrics": 136, "get": 98, "walk_tables": 7,
                    "ignore_values": 31, "declared": 76, "inferred": 60}
    assert mod.EXPECTED_COUNTS == {"modules": 6, "metrics": 136, "get_rows": 98, "walk_tables": 7,
                                   "ignore_values": 31, "inferred_types": 60, "declared_types": 76}
    text = SNMP_YML.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    metrics = [m for md in doc["modules"].values() for m in md["metrics"]]
    assert len(metrics) == mod.EXPECTED_COUNTS["metrics"]
    assert len({m["oid"] for m in metrics}) == 136
    assert len(re.findall(r"^ *# type inferred", text, re.M)) == 60


# ── 2) auth mapping
def test_auth_mapping_translates_exactly_the_documented_three_names():
    """Verifies auth mapping translates exactly the documented three names."""
    mod = _tool()
    assert mod.AUTH_KEY_MAP == {"auth_passphrase": "password", "priv_passphrase": "priv_password"}
    assert mod.AUTH_VALUE_MAP == {("priv_protocol", "AES128"): "AES"}
    gen = yaml.safe_load(GENERATOR.read_text(encoding="utf-8"))
    doc = yaml.safe_load(SNMP_YML.read_text(encoding="utf-8"))
    for name, spec_auth in gen["auths"].items():
        want = {}
        for k, v in spec_auth.items():
            rk = mod.AUTH_KEY_MAP.get(k, k)
            want[rk] = mod.AUTH_VALUE_MAP.get((rk, v), v)
        assert doc["auths"][name] == want, f"auths.{name}: نگاشتِ ابزار با فایل یکی نیست"
    # The spec deliberately kept the generator form (decision G.4.1), so the runtime key is not in the spec.
    v3 = gen["auths"]["secure_v3"]
    assert v3["auth_passphrase"] == "${SNMP_V3_AUTH_PASSPHRASE}" and v3["priv_passphrase"] == "${SNMP_V3_PRIV_PASSPHRASE}"
    assert v3["priv_protocol"] == "AES128"
    assert "password" not in v3 and "priv_password" not in v3


def test_generator_yml_documents_the_v3_mapping_above_auths():
    """Verifies generator yml documents the v 3 mapping above auths."""
    text = GENERATOR.read_text(encoding="utf-8")
    head, sep, _rest = text.partition("auths:")
    assert sep, "بلوک auths پیدا نشد"
    for needle in ("auth_passphrase  → password", "priv_passphrase  → priv_password",
                   "AES128 → AES", "tools/build_snmp_yml.py"):
        assert needle in head, f"کامنتِ نگاشتِ v3 بالای auths نیست: {needle}"
    assert "name of the environment variables" in head, "The fact that SNMP_V3_* is not changed is not recorded"


# ── 3) Zero invention, zero network
def test_tool_writes_only_spec_oids():
    """Verifies tool writes only spec oids."""
    mod = _tool()
    gen = yaml.safe_load(GENERATOR.read_text(encoding="utf-8"))
    spec_oids = {str(r["oid"]) for _m, r, _g in mod.spec_rows(gen)}
    doc = yaml.safe_load(SNMP_YML.read_text(encoding="utf-8"))
    file_oids = {str(m["oid"]) for md in doc["modules"].values() for m in md["metrics"]}
    assert file_oids - spec_oids == set(), "ابزار OID بیرونِ spec می‌سازد"
    assert spec_oids - file_oids == set(), "ابزار ردیفِ spec را جا می‌اندازد"
    assert len(spec_oids) == 136


def test_tool_source_contains_no_download_or_network_code():
    """Verifies tool source contains no download or network code."""
    src = TOOL.read_text(encoding="utf-8")
    for banned in ("urllib", "requests", "httpx", "socket", "subprocess", "curl ", "wget ",
                   "Invoke-WebRequest", "http://", "https://"):
        assert banned not in src, f"ابزار نباید این را داشته باشد ({banned}) — صفر دانلود/شبکه در فاز G"


def test_inference_table_covers_exactly_the_untyped_spec_rows():
    """Verifies inference table covers exactly the untyped spec rows."""
    mod = _tool()
    gen = yaml.safe_load(GENERATOR.read_text(encoding="utf-8"))
    untyped = {(m, r["name"]) for m, r, _g in mod.spec_rows(gen) if r.get("type") is None}
    assert set(mod.INFER) == untyped, "INFER با ردیف‌های بی‌typeِ spec یکی نیست"
    assert len(untyped) == mod.EXPECTED_COUNTS["inferred_types"] == 60


def test_missing_inference_rule_fails_loudly():
    """Without this, a row with no type would be ``None'' or an unwitnessed guess."""
    mod = _tool()
    key = sorted(mod.INFER)[0]
    saved = mod.INFER.pop(key)
    try:
        with pytest.raises(SystemExit) as exc:
            mod.render(GENERATOR.read_text(encoding="utf-8"))
        assert "کم است" in str(exc.value) and key[1] in str(exc.value)
    finally:
        mod.INFER[key] = saved


def test_unused_inference_rule_fails_loudly():
    """Verifies unused inference rule fails loudly."""
    mod = _tool()
    mod.INFER[("printer_std", "sys_descr")] = ("gauge", "unused rule")
    try:
        with pytest.raises(SystemExit) as exc:
            mod.render(GENERATOR.read_text(encoding="utf-8"))
        assert "اضافه است" in str(exc.value)
    finally:
        mod.INFER.pop(("printer_std", "sys_descr"))


def test_output_path_defaults_inside_the_repo():
    """Verifies output path defaults inside the repo."""
    mod = _tool()
    assert mod.DEFAULT_OUT == SNMP_YML
    assert mod.DEFAULT_SPEC == GENERATOR
    assert str(mod.ROOT) == str(ROOT)
