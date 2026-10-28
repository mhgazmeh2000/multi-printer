#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/build_snmp_yml.py
# @brief:        Builds and checks monitoring/snmp.yml from monitoring/generator.yml.
# @details:      Responsibilities: Map the spec to the runtime config, write the file and check it
#                for staleness.
#                Main API: spec_rows(), check_inference_table(), emit_metric(), render(),
#                measure(), main()
#                Side effects: file I/O
# @dependencies: external: yaml
# @notes:        monitoring/snmp.yml is generated from generator.yml; do not edit snmp.yml by
#                hand. Can be run directly as a script.
# =============================================================================

"""
tools/build_snmp_yml.py — build/check `monitoring/snmp.yml` from `monitoring/generator.yml`

Why this tool exists: `snmp.yml` is the *runtime* config (snmp_exporter, not the official generator).
And the official generator requires the MIB vendor (Canon/Brother/Toshiba), which is not available; so
This file is derived from the project spec. The tool intentionally **does not generate any OIDs**: each metric one
row is `generator.yml` and each `help:` carries the same `source:`.

    python tools/build_snmp_yml.py:: reproduce monitoring/snmp.yml
    python tools/build_snmp_yml.py --check:: check only (rc=1 if file is old) + diff
    python tools/build_snmp_yml.py --report:: counts on stdout (row/metric/get/walk/types)

Rules (document: docs/snmp-exporter-setup.md §4):
  1) schema = config/config.go version 0.26.0 and loader is `yaml.UnmarshalStrict` ⇒
     Any spec keys that the runtime does not recognize will not be written:
       • `ignore_values` → comment of the same metric (sentinel filter in exporter_adapter, step G.5)
       • `source` → `help` text
       • `lookups`/`row_index`/`label_oid` → delete (they have no meaning outside `indexes`)
  2) `auths' is mapped to runtime names: auth_passphrase→password,
     priv_passphrase→priv_password, AES128→AES (ConfigureSNMP matches these names).
     The name of the environment variables is not changed.
  3) ``types'' declared in the spec are exactly the same; Rows without type with INFER (below) and with
     They witness line by line inferences and a tracking comment sits on them.
  4) Output is **deterministic**: same as input ⇒ same bytes (without timestamp).
     That's why `--check' can be a CI gate and running it again won't create any diff
     (idempotent).
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

import yaml  # requirements-dev.txt

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "monitoring" / "generator.yml"
DEFAULT_OUT = ROOT / "monitoring" / "snmp.yml"

# ── type inference for the spec rows that carry no `type:` ───────────────────
# Never invent an ASN.1 claim: each entry cites either (a) the parse site that
# consumes the value, or (b) the precedent of an already-typed row in the same
# module. Families: counter = cumulative page/job/gen counts (code diffs them),
# gauge = levels/percents/capacities/status codes/sizes/uptime,
# DisplayString = identity text and table roots.
TOSHIBA_DIFF_EVIDENCE = "base.py:344 typed `int`, diffed by _safe_delta (toshiba.py:434-440)"
INFER: dict = {
    ("printer_std", "bw_std"): ("counter", "canon.py:216 si(g(…43.10.2.1.4.1.3)) → black_white pages (canon.py:316)"),
    ("printer_canon", "alt_lbp_total"): ("counter", "canon.py:278 si(gc(…1.11.1.1.1.3.3)) → total pages"),
    ("printer_canon", "alt_total"): ("counter", "canon.py:229 si(gc(…1.11.2.2.1.3.1))"),
    ("printer_canon", "alt_copy"): ("counter", "canon.py:230 si(…1.11.2.2.1.3.6)"),
    ("printer_canon", "alt_print"): ("counter", "canon.py:231 si(…1.11.2.2.1.3.9)"),
    ("printer_canon", "alt_scan"): ("counter", "canon.py:232 si(…1.11.2.2.1.3.12)"),
    ("printer_hp", "general_oids"): ("gauge", "enhanced_collector.py:243-249 int(val) + 0..100 guard"),
    ("printer_hp", "general_oids_2"): ("gauge", "enhanced_collector.py:245 same int()/0..100 loop"),
    ("printer_hp", "hp_npcl_pct_oid"): ("gauge", "enhanced_collector.py:53 _HP_NPCL_PCT_OID → percent path"),
    ("printer_canon", "canon_alternate_oids"): ("gauge", "enhanced_collector.py:258-264 int(val) + 0..100 guard"),
    ("printer_canon", "canon_alternate_oids_2"): ("gauge", "enhanced_collector.py:258-264 same loop"),
    ("printer_brother", "brother_black"): ("gauge", "brother.py:183-186 si(...) + 0..100 → toner level"),
    ("printer_brother", "brother_drum_oid"): ("gauge", "enhanced_collector.py:80 BROTHER_DRUM_OID → drum life percent"),
    ("printer_brother", "val_val"): ("counter", "cartridge_id.py:316-321 int(str(val)) — Toner Replace Count, +1 per swap"),
    ("printer_toshiba", "print_list"): ("counter", "toshiba.py:444 _raw_int → toshiba.py:430-432 si(v)"),
    ("printer_toshiba", "scan_net_fc"): ("counter", "family of tsh_scan_fc (counter); read through _raw_int"),
    ("printer_toshiba", "scan_net_bw"): ("counter", "family of tsh_scan_bw (counter); read through _raw_int"),
    ("printer_toshiba", "uptime"): ("gauge", "base.py:394,415 prev_uptime carried forward — sys_uptime precedent"),
}
# Toshiba paper-size counters: 5 branches × (fc, bw, total) — exact spec names
TOSHIBA_PAPER = {
    "a3": ("oid_map.py:43-45", ("oids_a3_fc", "oids_a3_bw", "a3_total")),
    "a4": ("oid_map.py:40-42", ("oids_a4_fc", "oids_a4_bw", "a4_total")),
    "a4r": ("oid_map.py:46-48", ("a4r_fc", "a4r_bw", "a4r_total")),
    "a5": ("oid_map.py:49-51", ("oids_a5_fc", "oids_a5_bw", "a5_total")),
    "b4": ("oid_map.py:52-54", ("oids_b4_fc", "oids_b4_bw", "b4_total")),
}
for _stem, (_lines, _names) in TOSHIBA_PAPER.items():
    for _suf, _name in zip(("fc", "bw", "total"), _names):
        INFER[("printer_toshiba", _name)] = ("counter", f"{_suf} sub-counter of branch {_stem} ({_lines}); {TOSHIBA_DIFF_EVIDENCE}")
for _c in ("cyan", "magenta", "yellow", "black"):
    INFER[("printer_toshiba", f"toner_{_c}_usage")] = ("gauge", "block of tsh_blk_st, typed gauge in the spec")
    INFER[("printer_toshiba", f"toner_{_c}_status")] = ("gauge", "block of tsh_cyan_st, typed gauge in the spec")
for _n in ("1", "2", "3"):
    INFER[("printer_toshiba", f"alert{_n}_code")] = ("gauge", "status code, sibling of the alert*_msg text rows")
    INFER[("printer_toshiba", f"alert{_n}_msg")] = ("DisplayString", "alert message text")
for _n in ("1", "2", "3", "6"):
    INFER[("printer_toshiba", f"tray{_n}_status")] = ("gauge", "tsh_tray_table status code (int in the dict the collector reads)")
for _n in ("3", "6"):
    INFER[("printer_toshiba", f"tray{_n}_level")] = ("gauge", "family of tsh_t1_level, typed gauge in the spec")
    INFER[("printer_toshiba", f"tray{_n}_size")] = ("gauge", "family of tsh_t1_size, typed gauge in the spec")
# walk / probe roots: the bare column OID is never a value on a device
for _mod, _nm, _ev in (
    ("printer_std", "standard", "43.11.1.1.6 = prtMarkerSuppliesDescription column root — cartridge_discovery.py:76"),
    ("printer_hp", "candidate_subtrees_hp", "cartridge_discovery.py:79 — walk candidate, not a value"),
    ("printer_hp", "candidate_subtrees_hp_2", "cartridge_discovery.py:80 — walk candidate, not a value"),
    ("printer_brother", "candidate_subtrees_brother", "cartridge_discovery.py:83 — walk candidate, not a value"),
    ("printer_brother", "candidate_subtrees_brother_2", "cartridge_discovery.py:84 — walk candidate, not a value"),
):
    INFER[(_mod, _nm)] = ("DisplayString", _ev + "; no sample is emitted for the bare column OID")

# ── auth translation: spec keys → runtime keys (config/config.go v0.26.0) ─────
AUTH_KEY_MAP = {"auth_passphrase": "password", "priv_passphrase": "priv_password"}
AUTH_VALUE_MAP = {("priv_protocol", "AES128"): "AES"}
NAME_OK = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
PROBE_RE = re.compile(r"^ *# +([0-9]+(?:\.[0-9]+){6,}) +←", re.M)

# expected by tests/test_build_snmp_yml.py — if someone changes these, that test says
EXPECTED_COUNTS = {"modules": 6, "metrics": 136, "get_rows": 98, "walk_tables": 7,
                   "ignore_values": 31, "inferred_types": 60, "declared_types": 76}


def spec_rows(gen: dict):
    """(module, row, group_or_None) — metric rows of the spec, in the same order as the file."""
    for mod, md in gen["modules"].items():
        for r in md.get("get") or []:
            if isinstance(r, dict):
                yield mod, r, None
        for w in md.get("walk") or []:
            for r in w.get("metrics") or []:
                yield mod, r, w.get("name")


def check_inference_table(rows) -> None:
    """INFER must be exactly on untyped rows: not too few, not too many."""
    untyped = {(mod, r["name"]) for mod, r, _ in rows if r.get("type") is None}
    missing = sorted(untyped - set(INFER))
    if missing:
        raise SystemExit(f"کم است: این ردیف‌ها type ندارند و قاعدهٔ استنتاج هم ندارند: {missing}")
    unused = sorted(set(INFER) - untyped)
    if unused:
        raise SystemExit(f"اضافه است: قاعدهٔ استنتاجِ بی‌مصرف (ردیفِ type‌دار یا حذف‌شده): {unused}")


def emit_metric(row: dict, module: str, inferred: dict) -> list:
    """A metric block + comments that the runtime schema has no room for."""
    name, oid, typ = row["name"], row["oid"], row.get("type")
    note = ""
    if typ is None:
        typ, note = inferred[(module, name)]
    ind = "    "
    out = [
        f"{ind}- name: {name}",
        f"{ind}  oid: {oid}",
        f"{ind}  type: {typ}",
        f"{ind}  help: \"PrintGuard {module}: {name} [{row.get('source', 'no source')}]\"",
    ]
    if note:
        out.append(f"{ind}  # type inferred, not declared in code — {note}")
    if "ignore_values" in row:
        vals = ", ".join(f'"{v}"' for v in row["ignore_values"])
        out.append(f"{ind}  # ignore_values: [{vals}]")
    return out


def render(spec_text: str) -> str:
    """Generating the full text of snmp.yml from the text of generator.yml (without writing to disk)."""
    gen = yaml.safe_load(spec_text)
    probes = PROBE_RE.findall(spec_text)
    rows = list(spec_rows(gen))
    check_inference_table(rows)
    for _mod, r, _grp in rows:
        if not NAME_OK.match(r["name"]):
            raise SystemExit(f"metric name isn't a valid Prometheus name: {r['name']!r}")

    n_get = sum(1 for _m, _r, g in rows if g is None)
    n_walk_rows = len(rows) - n_get
    n_tables = sum(len([w for w in (md.get("walk") or []) if isinstance(w, dict)]) for md in gen["modules"].values())

    o = [
        "#",
        "# monitoring/snmp.yml — runtime configuration for snmp_exporter (phase G · step G.3)",
        "#",
        "# Derived from `monitoring/generator.yml` (spec) — the single source of truth. No OID is invented here:",
        "# every metric has a spec row, and every `help:` points to a file:line of code.",
        "# Coverage gate: python tools/check_oid_coverage.py · structure gate of this file:",
        "# python -m pytest tests/test_snmp_yml.py",
        "# Regenerate/check: python tools/build_snmp_yml.py [--check]",
        "#",
        "# Why manual? The official generator needs the vendor MIBs (Canon/Brother/Toshiba), which are",
        "# not available. Description and mapping table: docs/snmp-exporter-setup.md §4",
        "#",
        "# schema = config/config.go of snmp_exporter v0.26.0 and the loader is strict",
        "# (yaml.UnmarshalStrict) ⇒ any unknown key here is a load error on the target device.",
        "# So the two spec keys are not keys here:",
        "#   • ignore_values → a comment on the same metric; the sentinel filter is in",
        "#     core/collectors/exporter_adapter.py (step G.5), where today",
        "#     si() and the 0..100 guard are in the collector code",
        "#   • source        → the help: text",
        "#",
        "# Load with the official binary in the sandbox was **not** done (owner decision: a real test on the server",
        "# itself at the end of the project) — monitoring/snmp-manifest.json → load_test_sandbox:",
        "# not_attempted. The file is OS-independent: the same bytes are loaded on Windows and Linux",
        "# alike.",
        "#",
        f"# {len(rows)} metrics = {n_get} get rows + {n_walk_rows} rows of {n_tables} walk tables.",
        f"# {len(probes)} probe-only probes are comments in the spec and stay comments here too.",
        "",
        "version: 1",
        "",
        "auths:",
        "  # names follow the phase G convention: printer['exporter_auth'] == f\"public_{snmp_version}\"",
    ]
    for aname, adef in gen["auths"].items():
        o.append(f"  {aname}:")
        if adef.get("version") == 3:
            o.append("    # placeholder; the real value is read from env and is never committed")
            o.append("    # the runtime wants these keys: password/priv_password (not auth/priv_passphrase)")
            o.append("    # and --config.expand-env is needed so that ${...} values get filled in")
        for k, v in adef.items():
            rk = AUTH_KEY_MAP.get(k, k)
            vv = AUTH_VALUE_MAP.get((rk, v), v)
            o.append(f"    {rk}: \"{vv}\"" if isinstance(vv, str) and vv.startswith("${") else f"    {rk}: {vv}")
        if adef.get("version") == 3:
            o.append("    # note: the runtime value is \"AES\", not the AES128 of the spec")

    o += ["", "modules:"]
    stats = {"modules": 0, "metrics": 0, "get_rows": 0, "walk_tables": 0,
             "ignore_values": 0, "inferred_types": 0, "declared_types": 0}
    for mod, md in gen["modules"].items():
        groups = [w for w in md.get("walk") or [] if isinstance(w, dict)]
        gets = [g for g in md.get("get") or [] if isinstance(g, dict)]
        n_leaf = len(gets) + sum(len(g.get("metrics") or []) for g in groups)
        stats["modules"] += 1
        stats["metrics"] += n_leaf
        stats["get_rows"] += len(gets)
        stats["walk_tables"] += len(groups)
        stats["ignore_values"] += sum(1 for r in gets if "ignore_values" in r) + \
            sum(1 for g in groups for r in g.get("metrics") or [] if "ignore_values" in r)
        stats["inferred_types"] += sum(1 for r in gets if r.get("type") is None) + \
            sum(1 for g in groups for r in g.get("metrics") or [] if r.get("type") is None)
        stats["declared_types"] += sum(1 for r in gets if r.get("type") is not None) + \
            sum(1 for g in groups for r in g.get("metrics") or [] if r.get("type") is not None)
        o += [
            "  # " + "─" * 72,
            f"  # {mod} — {n_leaf} metrics · {len(gets)} get · {len(groups)} walk tables",
            "  # " + "─" * 72,
            f"  {mod}:",
        ]
        if groups:
            o.append("    # a GETNEXT sweep over these trees brings several rows at once")
            o.append("    walk:")
            for g in groups:
                o.append(f"    - {g['subtree']}          # {g['name']} — {len(g.get('metrics') or [])} matrix rows under this tree")
        if gets:
            o.append("    # scalars: one closed GET, no walk")
            o.append("    get:")
            for r in gets:
                o.append(f"    - {r['oid']}          # {r['name']}")
        o.append("    metrics:")
        for r in gets:
            o += emit_metric(r, mod, INFER)
        for g in groups:
            o.append(f"    # ── {g['name']} (subtree {g['subtree']}) ──")
            for r in g.get("metrics") or []:
                o += emit_metric(r, mod, INFER)
        o.append("")

    return "\n".join(o).rstrip() + "\n", stats, len(rows)


def measure(text: str) -> dict:
    """Counts from generated text — what the manifest and tests expect."""
    doc = yaml.safe_load(text)
    metrics = [m for md in (doc.get("modules") or {}).values() for m in md.get("metrics") or []]
    return {
        "modules": len(doc.get("modules") or {}),
        "metrics": len(metrics),
        "unique_oids": len({str(m["oid"]) for m in metrics}),
        "unique_names": len({m["name"] for m in metrics}),
        "get_rows": sum(len(md.get("get") or []) for md in (doc.get("modules") or {}).values()),
        "walk_tables": sum(len(md.get("walk") or []) for md in (doc.get("modules") or {}).values()),
        "ignore_values": len(re.findall(r"^ *# ignore_values: ", text, re.M)),
        "live_ignore_keys": len(re.findall(r"^ *ignore_values:", text, re.M)),
        "inferred_types": len(re.findall(r"^ *# type inferred", text, re.M)),
    }


def main(argv=None) -> int:
    """Command-line entry point: build or check monitoring/snmp.yml."""

    ap = argparse.ArgumentParser(description="build/verify monitoring/snmp.yml from monitoring/generator.yml")
    ap.add_argument("--generator", default=str(DEFAULT_SPEC), help="مسیر spec (پیش‌فرض: monitoring/generator.yml)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="مسیر خروجی (پیش‌فرض: monitoring/snmp.yml)")
    ap.add_argument("--check", action="store_true", help="فقط بررسی؛ اگر فایل با spec نمی‌خواند rc=1 و diff چاپ می‌شود")
    ap.add_argument("--report", action="store_true", help="شمارش‌های اندازه‌گیری‌شده را چاپ کن")
    args = ap.parse_args(argv)

    spec_path, out_path = Path(args.generator), Path(args.out)
    if not spec_path.exists():
        print(f"FAIL: spec not found: {spec_path}")
        return 1
    spec_text = spec_path.read_text(encoding="utf-8")
    text, stats, n_rows = render(spec_text)
    got = measure(text)

    if args.check:
        if not out_path.exists():
            print(f"FAIL: {out_path} وجود ندارد — اجرا بدون --check")
            return 1
        old = out_path.read_text(encoding="utf-8")
        if old == text:
            print(f"OK: {out_path.name} با spec می‌خواند ({got['metrics']} متریک، "
                  f"{got['unique_oids']} OID یکتا، {got['ignore_values']} ignore_values، "
                  f"{got['inferred_types']} type استنتاجی)")
            return 0
        print(f"FAIL: {out_path.name} کهنه است — اول `python tools/build_snmp_yml.py`")
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), text.splitlines(keepends=True),
            fromfile=f"{out_path.name} (on disk)", tofile=f"{out_path.name} (from spec)"))
        return 1

    if args.report:
        print(f"spec: {spec_path.relative_to(ROOT) if spec_path.is_relative_to(ROOT) else spec_path}")
        print(f"  rows={n_rows} modules={stats['modules']} metrics={stats['metrics']} "
              f"get={stats['get_rows']} walk_tables={stats['walk_tables']}")
        print(f"  ignore_values={stats['ignore_values']} type declared={stats['declared_types']} "
              f"inferred={stats['inferred_types']}")
        print(f"  generated file: {got['unique_oids']} unique OIDs, {got['live_ignore_keys']} live ignore_values keys")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"wrote {out_path.name}: {got['metrics']} metrics, {got['unique_oids']} unique OIDs, "
          f"{got['walk_tables']} walk tables, {got['ignore_values']} ignore_values comments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
