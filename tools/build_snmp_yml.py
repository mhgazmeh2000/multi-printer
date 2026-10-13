#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/build_snmp_yml.py — ساخت/بررسی `monitoring/snmp.yml` از `monitoring/generator.yml`

چرا این ابزار هست: `snmp.yml` configِ *runtime* است (snmp_exporter، نه generatorِ رسمی)
و generator رسمی به MIB vendor (Canon/Brother/Toshiba) نیاز دارد که در دسترس نیست؛ پس
این فایل از spec پروژه مشتق می‌شود. ابزار عمداً **هیچ OIDی نمی‌سازد**: هر متریک یک
ردیفِ `generator.yml` است و هر `help:` همان `source:` را حمل می‌کند.

    python tools/build_snmp_yml.py            :: بازتولیدِ monitoring/snmp.yml
    python tools/build_snmp_yml.py --check     :: فقط بررسی (rc=1 اگر فایل کهنه است) + diff
    python tools/build_snmp_yml.py --report   :: شمارش‌ها روی stdout (ردیف/متریک/get/walk/typeها)

قاعده‌ها (سند: docs/snmp-exporter-setup.md §۴):
  ۱) schema = config/config.go نسخهٔ 0.26.0 و loader استِ `yaml.UnmarshalStrict` ⇒
     هیچ کلیدِ specی که runtime نمی‌شناسد نوشته نمی‌شود:
       • `ignore_values` → کامنتِ همان متریک (فیلترِ sentinel در exporter_adapter، گام G.۵)
       • `source`        → متنِ `help`
       • `lookups`/`row_index`/`label_oid` → حذف (بیرونِ `indexes` معنا ندارند)
  ۲) `auths` به نام‌های runtime نگاشت می‌شود: auth_passphrase→password،
     priv_passphrase→priv_password، AES128→AES (ConfigureSNMP این نام‌ها را match می‌کند).
     نامِ متغیرهای محیطی عوض نمی‌شود.
  ۳) `type`های اعلام‌شده در spec عیناً می‌نشینند؛ ردیف‌های بی‌type با INFER (زیر) و با
     شاهدِ خط‌به‌خط استنتاج می‌شوند و یک کامنتِ ردیابی روی‌شان می‌نشیند.
  ۴) خروجی **deterministic** است: همان ورودی ⇒ همان بایت‌ها (بدون timestamp).
     برای همین `--check` می‌تواند گیتِ CI باشد و اجرای دوباره هیچ diffی نمی‌سازد
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
TOSHIBA_DIFF_EVIDENCE = "base.py:325 typed `int`, diffed by _safe_delta (toshiba.py:419-425)"
INFER: dict = {
    ("printer_std", "bw_std"): ("counter", "canon.py:203 si(g(…43.10.2.1.4.1.3)) → black_white pages (canon.py:306)"),
    ("printer_canon", "alt_lbp_total"): ("counter", "canon.py:267 si(gc(…1.11.1.1.1.3.3)) → total pages"),
    ("printer_canon", "alt_total"): ("counter", "canon.py:216 si(gc(…1.11.2.2.1.3.1))"),
    ("printer_canon", "alt_copy"): ("counter", "canon.py:217 si(…1.11.2.2.1.3.6)"),
    ("printer_canon", "alt_print"): ("counter", "canon.py:218 si(…1.11.2.2.1.3.9)"),
    ("printer_canon", "alt_scan"): ("counter", "canon.py:219 si(…1.11.2.2.1.3.12)"),
    ("printer_hp", "general_oids"): ("gauge", "enhanced_collector.py:229-235 int(val) + 0..100 guard"),
    ("printer_hp", "general_oids_2"): ("gauge", "enhanced_collector.py:231 same int()/0..100 loop"),
    ("printer_hp", "hp_npcl_pct_oid"): ("gauge", "enhanced_collector.py:39 _HP_NPCL_PCT_OID → percent path"),
    ("printer_canon", "canon_alternate_oids"): ("gauge", "enhanced_collector.py:244-250 int(val) + 0..100 guard"),
    ("printer_canon", "canon_alternate_oids_2"): ("gauge", "enhanced_collector.py:244-250 same loop"),
    ("printer_brother", "brother_black"): ("gauge", "brother.py:169-172 si(...) + 0..100 → toner level"),
    ("printer_brother", "brother_drum_oid"): ("gauge", "enhanced_collector.py:66 BROTHER_DRUM_OID → drum life percent"),
    ("printer_brother", "val_val"): ("counter", "cartridge_id.py:305-310 int(str(val)) — Toner Replace Count, +1 per swap"),
    ("printer_toshiba", "print_list"): ("counter", "toshiba.py:429 _raw_int → toshiba.py:415-417 si(v)"),
    ("printer_toshiba", "scan_net_fc"): ("counter", "family of tsh_scan_fc (counter); read through _raw_int"),
    ("printer_toshiba", "scan_net_bw"): ("counter", "family of tsh_scan_bw (counter); read through _raw_int"),
    ("printer_toshiba", "uptime"): ("gauge", "base.py:375,396 prev_uptime carried forward — sys_uptime precedent"),
}
# Toshiba paper-size counters: 5 branches × (fc, bw, total) — exact spec names
TOSHIBA_PAPER = {
    "a3": ("oid_map.py:33-35", ("oids_a3_fc", "oids_a3_bw", "a3_total")),
    "a4": ("oid_map.py:30-32", ("oids_a4_fc", "oids_a4_bw", "a4_total")),
    "a4r": ("oid_map.py:36-38", ("a4r_fc", "a4r_bw", "a4r_total")),
    "a5": ("oid_map.py:39-41", ("oids_a5_fc", "oids_a5_bw", "a5_total")),
    "b4": ("oid_map.py:42-44", ("oids_b4_fc", "oids_b4_bw", "b4_total")),
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
    ("printer_std", "standard", "43.11.1.1.6 = prtMarkerSuppliesDescription column root — cartridge_discovery.py:62"),
    ("printer_hp", "candidate_subtrees_hp", "cartridge_discovery.py:65 — walk candidate, not a value"),
    ("printer_hp", "candidate_subtrees_hp_2", "cartridge_discovery.py:66 — walk candidate, not a value"),
    ("printer_brother", "candidate_subtrees_brother", "cartridge_discovery.py:69 — walk candidate, not a value"),
    ("printer_brother", "candidate_subtrees_brother_2", "cartridge_discovery.py:70 — walk candidate, not a value"),
):
    INFER[(_mod, _nm)] = ("DisplayString", _ev + "; no sample is emitted for the bare column OID")

# ── auth translation: spec keys → runtime keys (config/config.go v0.26.0) ─────
AUTH_KEY_MAP = {"auth_passphrase": "password", "priv_passphrase": "priv_password"}
AUTH_VALUE_MAP = {("priv_protocol", "AES128"): "AES"}
NAME_OK = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
PROBE_RE = re.compile(r"^ *# +([0-9]+(?:\.[0-9]+){6,}) +←", re.M)

# expected by tests/test_build_snmp_yml.py — اگر کسی این‌ها را عوض کند، آن تست می‌گوید
EXPECTED_COUNTS = {"modules": 6, "metrics": 136, "get_rows": 98, "walk_tables": 7,
                   "ignore_values": 31, "inferred_types": 60, "declared_types": 76}


def spec_rows(gen: dict):
    """(module, row, group_or_None) — ردیف‌های متریکِ spec، به همان ترتیبِ فایل."""
    for mod, md in gen["modules"].items():
        for r in md.get("get") or []:
            if isinstance(r, dict):
                yield mod, r, None
        for w in md.get("walk") or []:
            for r in w.get("metrics") or []:
                yield mod, r, w.get("name")


def check_inference_table(rows) -> None:
    """INFER باید دقیقاً روی ردیف‌های بی‌type باشد: نه کم، نه زیاد."""
    untyped = {(mod, r["name"]) for mod, r, _ in rows if r.get("type") is None}
    missing = sorted(untyped - set(INFER))
    if missing:
        raise SystemExit(f"کم است: این ردیف‌ها type ندارند و قاعدهٔ استنتاج هم ندارند: {missing}")
    unused = sorted(set(INFER) - untyped)
    if unused:
        raise SystemExit(f"اضافه است: قاعدهٔ استنتاجِ بی‌مصرف (ردیفِ type‌دار یا حذف‌شده): {unused}")


def emit_metric(row: dict, module: str, inferred: dict) -> list:
    """یک بلوکِ متریک + کامنت‌هایی که schemaِ runtime جا برایشان ندارد."""
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
    """ساختِ متنِ کاملِ snmp.yml از متنِ generator.yml (بدون نوشتن روی دیسک)."""
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
        "# monitoring/snmp.yml — پیکربندیِ runtime برای snmp_exporter (فاز G · گام G.3)",
        "#",
        "# مشتق‌شده از `monitoring/generator.yml` (spec) — تک‌منبعِ حقیقت. هیچ OIDی اینجا",
        "# اختراع نشده: هر metric یک ردیفِ spec دارد و هر `help:` به فایل:خطِ کد می‌رسد.",
        "# گیتِ پوشش: python tools/check_oid_coverage.py · گیتِ ساختارِ همین فایل:",
        "# python -m pytest tests/test_snmp_yml.py",
        "# بازتولید/بررسی: python tools/build_snmp_yml.py [--check]",
        "#",
        "# چرا دستی؟ generator رسمی به MIB vendor (Canon/Brother/Toshiba) نیاز دارد که در",
        "# دسترس نیست. شرح و جدولِ نگاشت: docs/snmp-exporter-setup.md §۴",
        "#",
        "# schema = config/config.go of snmp_exporter v0.26.0 and the loader is strict",
        "# (yaml.UnmarshalStrict) ⇒ هر کلیدِ ناشناخته اینجا، خطای load روی دستگاه مقصد است.",
        "# پس دو کلیدِ spec اینجا کلید نیستند:",
        "#   • ignore_values → کامنتِ همان متریک؛ فیلترِ sentinel در",
        "#     core/collectors/exporter_adapter.py (گام G.5) می‌کند، همان‌جا که امروز",
        "#     si() و گارد 0..100 در کدِ collector هست",
        "#   • source        → متنِ help:",
        "#",
        "# load با باینریِ رسمی در sandbox انجام **نشده** (تصمیم مالک: تست واقعی روی سرور",
        "# خودش در پایان پروژه) — monitoring/snmp-manifest.json → load_test_sandbox:",
        "# not_attempted. فایل مستقل از OS است: همان بایت‌ها روی Windows و لینوکس load",
        "# می‌شوند.",
        "#",
        f"# {len(rows)} متریک = {n_get} ردیفِ get + {n_walk_rows} ردیفِ جداولِ "
        f"{n_tables} تاییِ walk.",
        f"# {len(probes)} کاوشِ probe-only در spec کامنت‌اند و اینجا هم کامنت می‌مانند.",
        "",
        "version: 1",
        "",
        "auths:",
        "  # نام‌ها با قرارداد فاز G یکی‌اند: printer['exporter_auth'] == f\"public_{snmp_version}\"",
    ]
    for aname, adef in gen["auths"].items():
        o.append(f"  {aname}:")
        if adef.get("version") == 3:
            o.append("    # placeholder؛ مقدار واقعی از env خوانده می‌شود و هرگز commit نمی‌شود")
            o.append("    # runtime این کلیدها را می‌خواهد: password/priv_password (نه auth/priv_passphrase)")
            o.append("    # و --config.expand-env لازم است تا ${...}ها مقدار بگیرند")
        for k, v in adef.items():
            rk = AUTH_KEY_MAP.get(k, k)
            vv = AUTH_VALUE_MAP.get((rk, v), v)
            o.append(f"    {rk}: \"{vv}\"" if isinstance(vv, str) and vv.startswith("${") else f"    {rk}: {vv}")
        if adef.get("version") == 3:
            o.append("    # نکته: مقدارِ runtime «AES» است، نه AES128ِ spec")

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
            f"  # {mod} — {n_leaf} متریک · {len(gets)} get · {len(groups)} جدول walk",
            "  # " + "─" * 72,
            f"  {mod}:",
        ]
        if groups:
            o.append("    # یک GETNEXT-sweep روی این درخت‌ها، چند ردیف را یک‌جا می‌آورد")
            o.append("    walk:")
            for g in groups:
                o.append(f"    - {g['subtree']}          # {g['name']} — {len(g.get('metrics') or [])} سطرِ ماتریس زیر این درخت")
        if gets:
            o.append("    # scalarها: یک GET بسته، بدون walk")
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
    """شمارش‌ها از متنِ تولیدشده — همان چیزی که manifest و تست‌ها انتظار دارند."""
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
