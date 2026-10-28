#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/compare_snmp_sources.py
# @brief:        Compares two SNMP sources (exporter vs snapshot) for one printer (step G.6).
# @details:      Responsibilities: Flatten both sources, compare shapes and tolerances, and render
#                a report.
#                Main API: flatten(), kind_of(), compare_shapes(), read_pdu_counts(),
#                render_markdown(), InputError, load_exporter_metrics(), load_snapshot() (+1 more)
#                Side effects: network I/O, file I/O
# @dependencies: internal: core.collectors.exporter_adapter, core.exporter_client
# @notes:        Can be run directly as a script.
# =============================================================================

"""compare_snmp_sources.py — Compare "two sources" for a printer (step G.6).

Two sides of the comparison:
  1) The output of snmp_exporter (the exposition text you got from /snmp?target=…)
  2) legacy collector snapshot (JSON of the same structure that collect_hp/collect_canon returns;
     output of `core.enhanced_collector.collect_enhanced` or a snapshot log in `logs.db`)

Why it passes "mirror": the unit of comparison should be the *concept*, not the *metric name*. metric
`hrPrinterTotal` is opposite to `counters.total`, so first capture with the same
`core.collectors.exporter_adapter.exporter_to_collector_shape` (mirror tested in G.5) to
The collector structure is created and then the *common paths* of the two structures are compared line by line.
This way the comparison measures exactly what turning on `USE_SNMP_EXPORTER=1` changes.
gives — not a fresh manual translation that itself has bugs.

Note G.6 (according to the owner's instructions): Here **no number about the PDU is recorded in the repo**. tools
It is measured with artificial fixtures; The real number of PDU from the owner himself on his server from this report
reads

Application:
    python3 tools/compare_snmp_sources.py \\
        --exporter-capture /tmp/exp_hp.txt \\
        --snmp-snapshot /tmp/snap.json \\
        [--brand hp] [--tolerance 5] [--json /tmp/cmp.json] [--md /tmp/cmp.md]

exit output:
    0 = no significant difference (no diff line) 1 = significant difference
    2 = incomplete/inconsistent input (no file, broken JSON, unsupported brand, zero shared path)
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.exporter_client import ExporterError, parse_prometheus_text  # noqa: E402
from core.collectors.exporter_adapter import exporter_to_collector_shape  # noqa: E402

SUPPORTED_BRANDS = ("hp", "canon")

# *Hourly* volatile fields — do not measure device state, so are not compared (in log
# They are listed explicitly so that nothing is hidden; are re-enabled with --include-volatile).
VOLATILE_PATHS = ("last_poll", "poll_ms")

# Keys that are not "device mode" (our tag + clocks); For the input gate of the common count
# They are left out, and it is important that they are not confusing in the table below "mirror deficiency".
IDENTITY_PATHS = ("brand", "ip", "name", "online")


# ─────────────────────────────────────────────────────────────────────────────
# 1) smoothing the structure → dotted lines
# ─────────────────────────────────────────────────────────────────────────────
def flatten(obj, prefix=""):
    """Converts dict/list to {path: scalar value} (absolutely deterministic, key-ordered).

    The remaining non-scalar values (empty dict/list) are kept with the label "(empty)" until
    "Less" should not be confused with "there is but nothing".
    """
    out = {}
    if isinstance(obj, dict):
        for key in sorted(obj, key=lambda k: str(k)):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten(obj[key], path))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            out.update(flatten(item, f"{prefix}.{idx}"))
    else:
        out[prefix] = obj
    return out


def kind_of(value):
    """Classify a value as bool, num, text, other or missing."""

    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "num" if value is not None else "num"
    if isinstance(value, str):
        return "text"
    return "other"


# ─────────────────────────────────────────────────────────────────────────────
# 2) Comparison
# ─────────────────────────────────────────────────────────────────────────────
def compare_shapes(exporter_shape, snapshot_shape, tolerance_pct, ignore=VOLATILE_PATHS):
    """Creates comparison lines. Returns: (rows, summary).

    Verdicts:
      ok — equal or relative difference <= tolerance
      diff — relative difference > tolerance (or unequal string)
      missing — one side does not have a real value (None / the absence of the path) → there is **not** a meaningful difference
    """
    e_flat = flatten(exporter_shape)
    s_flat = flatten(snapshot_shape)
    e_flat = {k: v for k, v in e_flat.items() if v is not None and k not in ignore}
    s_flat = {k: v for k, v in s_flat.items() if v is not None and k not in ignore}

    rows = []
    n_ok = n_diff = n_missing = 0
    for path in sorted(set(e_flat) | set(s_flat)):
        in_e, in_s = path in e_flat, path in s_flat
        ev, sv = e_flat.get(path), s_flat.get(path)
        if not (in_e and in_s):
            rows.append({"path": path, "exporter": ev if in_e else None,
                         "snapshot": sv if in_s else None, "kind": kind_of(ev if in_e else sv),
                         "rel_diff_pct": None,
                         "verdict": "missing",
                         "note": ("only_in_exporter" if in_e else "only_in_snapshot")})
            n_missing += 1
            continue
        ek, sk = kind_of(ev), kind_of(sv)
        if ek != sk or ek not in ("num", "bool", "text"):
            rows.append({"path": path, "exporter": ev, "snapshot": sv, "kind": f"{ek}/{sk}",
                         "rel_diff_pct": None, "verdict": "diff", "note": "kind_mismatch"})
            n_diff += 1
            continue
        if ek == "text":
            same = str(ev).strip() == str(sv).strip()
            rows.append({"path": path, "exporter": ev, "snapshot": sv, "kind": "text",
                         "rel_diff_pct": None,
                         "verdict": "ok" if same else "diff", "note": "" if same else "text_mismatch"})
            n_ok += same
            n_diff += not same
            continue
        if ek == "bool":
            same = bool(ev) == bool(sv)
            rows.append({"path": path, "exporter": ev, "snapshot": sv, "kind": "bool",
                         "rel_diff_pct": None,
                         "verdict": "ok" if same else "diff", "note": "" if same else "bool_mismatch"})
            n_ok += same
            n_diff += not same
            continue
        a, b = float(ev), float(sv)
        base = max(abs(a), abs(b))
        if base == 0:
            rel = 0.0
        else:
            rel = abs(a - b) / base * 100.0
        verdict = "ok" if rel <= tolerance_pct else "diff"
        rows.append({"path": path, "exporter": _num(ev), "snapshot": _num(sv), "kind": "num",
                     "rel_diff_pct": round(rel, 2), "verdict": verdict,
                     "note": "" if verdict == "ok" else "over_tolerance"})
        n_ok += verdict == "ok"
        n_diff += verdict == "diff"

    common = sorted(set(e_flat) & set(s_flat))
    # Identity/time keys are shared by themselves; "Zero of the common path" should be meaningfully measured,
    # Otherwise, the irrelevant snapshot (which only has the correct brand) will be rejected from the input gate.
    meaningful = [p2 for p2 in common if p2 not in IDENTITY_PATHS]
    summary = {"rows": len(rows), "common_paths": len(common),
               "meaningful_common_paths": len(meaningful), "ok": n_ok, "diff": n_diff,
               "missing": n_missing,
               "only_in_exporter": sorted(set(e_flat) - set(s_flat)),
               "only_in_snapshot": sorted(set(s_flat) - set(e_flat))}
    return rows, summary


def _num(v):
    """Keeps the number in clean JSON (int if true, float otherwise; NaN/Inf → None)."""
    if isinstance(v, bool):
        return v
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    if f == int(f):
        return int(f)
    return round(f, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 3) PDU count
# ─────────────────────────────────────────────────────────────────────────────
def read_pdu_counts(metrics, snapshot_doc):
    """Exporter counters (total over all modules) against snapshot's SNMP_PDU_COUNT.

    No default value is invented: if there were none, the string "N/A" remains, and so does the report
    It is written the same way. `per_series' is kept so that the ``aggregation'' can be reviewed.
    """
    out = {"exporter": {"packets_sent": "N/A", "pdus_returned": "N/A"},
           "snapshot": "N/A", "per_series": {}, "source": {"snapshot": "N/A"}}
    for key, name in (("packets_sent", "snmp_scrape_packets_sent"),
                      ("pdus_returned", "snmp_scrape_pdus_returned")):
        series = metrics.get(name) or []
        if not series:
            continue
        vals = [_num(v) for _, v in series]
        vals = [v for v in vals if v is not None]
        out["exporter"][key] = sum(vals)
        out["per_series"][name] = [{"labels": dict(lab), "value": v}
                                   for lab, v in zip([labels for labels, _ in series], vals)]
    if isinstance(snapshot_doc, dict):
        for key in ("SNMP_PDU_COUNT", "pdu_count", "snmp_pdu_count"):
            if snapshot_doc.get(key) is not None:
                out["snapshot"] = _num(snapshot_doc[key])
                out["source"]["snapshot"] = key
                break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 4) Reports
# ─────────────────────────────────────────────────────────────────────────────
def render_markdown(rep):
    """Render the comparison report as Markdown."""

    L = []
    L.append("# گزارشِ مقایسهٔ دو منبعِ SNMP (G.6)")
    L.append("")
    L.append(f"- برند: `{rep['brand']}` — تلورانسِ نسبی: **{rep['tolerance_pct']}٪**")
    L.append(f"- ورودی‌ها: `exporter-capture={rep['inputs']['exporter_capture']}`، "
             f"`snmp-snapshot={rep['inputs']['snmp_snapshot']}`")
    s = rep["summary"]
    if rep["ignored_paths"]:
        L.append(f"- نادیده‌گرفته‌شده (ناپایدار): {', '.join('`%s`' % v for v in rep['ignored_paths'])}"
                 " — با `--include-volatile` مقایسه می‌شوند")
    L.append(f"- سطرها: {s['rows']} (مشترک: {s['common_paths']}, معنادار: "
             f"{s['meaningful_common_paths']}) — ok {s['ok']} / "
             f"**diff {s['diff']}** / missing {s['missing']}")
    pdu = rep["pdu"]
    L.append("")
    exp = pdu["exporter"]
    L.append("| شمارشگر | مقدار |")
    L.append("|---|---|")
    L.append(f"| exporter — `snmp_scrape_packets_sent` (جمعِ روی همهٔ moduleها) | "
             f"`{exp['packets_sent']}` |")
    L.append(f"| exporter — `snmp_scrape_pdus_returned` | `{exp['pdus_returned']}` |")
    L.append(f"| snapshot collector — `SNMP_PDU_COUNT` (کلیدِ JSON) | `{pdu['snapshot']}` |")
    L.append("")
    L.append("> اگر مقدار `N/A` است یعنی آن عدد در هیچ‌یک از دو ورودی نبود؛ ابزار چیزی "
             "اختراع نمی‌کند. برای عددِ قابل‌اتکا، `snmp_scrape_packets_sent` را در capture "
             "بگذارید (پیش‌فرضِ exporter هست) یا `SNMP_PDU_COUNT` را در snapshot.")
    L.append("")
    if rep["diff_rows"]:
        L.append("## اختلاف‌هایِ معنادار")
        L.append("")
        L.append("| مسیر | exporter | snapshot | اختلافِ نسبی |")
        L.append("|---|---|---|---|")
        for r in rep["diff_rows"]:
            rel = f"{r['rel_diff_pct']}٪" if r["rel_diff_pct"] is not None else r["note"]
            L.append(f"| `{r['path']}` | `{r['exporter']}` | `{r['snapshot']}` | {rel} |")
        L.append("")
    else:
        L.append("**هیچ اختلافِ معناداری دیده نشد** (در تلورانسِ تعریف‌شده).")
        L.append("")
    if s["only_in_snapshot"]:
        L.append(f"<details><summary>{len(s['only_in_snapshot'])} مسیری که exporter "
                 f"تولید نمی‌کند (کاستیِ ساختاریِ آینه، نه خرابیِ دستگاه)</summary>")
        L.append("")
        for p in s["only_in_snapshot"]:
            L.append(f"- `{p}`")
        L.append("")
        L.append("</details>")
    return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# 5) main
# ─────────────────────────────────────────────────────────────────────────────
class InputError(Exception):
    """Raised when an input file of the comparison tool is invalid."""

    pass


def load_exporter_metrics(paths):
    """Load exporter metric text from the given files."""

    texts = []
    for p in paths:
        path = pathlib.Path(p)
        if not path.is_file():
            raise InputError(f"exporter-capture پیدا نشد: {path}")
        texts.append(path.read_text(encoding="utf-8"))
    metrics = {}
    for text in texts:
        try:
            parsed = parse_prometheus_text(text)
        except ExporterError as exc:
            raise InputError(f"متنِ exporter قابلِ پارس نیست: {exc}") from exc
        for name, series in parsed.items():
            metrics.setdefault(name, []).extend(series)
    if not metrics:
        raise InputError("capture هیچ متریکی ندارد (متنِ خالی یا فقط کامنت)")
    return metrics


def load_snapshot(path, brand_override):
    """Load the snapshot file that the exporter output is compared against."""

    p = pathlib.Path(path)
    if not p.is_file():
        raise InputError(f"snmp-snapshot پیدا نشد: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise InputError(f"snmp-snapshot JSON معتبر نیست: {exc}") from exc
    if not isinstance(doc, dict):
        raise InputError("ریشهٔ snmp-snapshot باید object باشد")
    brand = brand_override or doc.get("brand")
    shape = doc.get("snapshot") if isinstance(doc.get("snapshot"), dict) else doc
    if "counters" not in shape and not any(
            k in shape for k in ("toners", "trays", "status", "counters_by_source")):
        raise InputError("ساختارِ snapshot شبیهِ خروجیِ collector نیست "
                         "(هیچِ counters/toners/trays/status ندارد)")
    if not brand:
        raise InputError("brand نه در snapshot هست و نه با --brand داده شد")
    if brand not in SUPPORTED_BRANDS:
        raise InputError(f"brand پشتیبانی‌نشده برای مقایسه: {brand!r} "
                         f"(این آینه فقط {SUPPORTED_BRANDS} را دارد)")
    return doc, shape, brand


def main(argv=None):
    """Command-line entry point of the comparison tool."""

    ap = argparse.ArgumentParser(
        description="مقایسهٔ خروجیِ snmp_exporter با snapshotِ collectorِ legacy")
    ap.add_argument("--exporter-capture", action="append", required=True,
                    help="فایلِ متنِ exposition (از /snmp?target=…)؛ قابلِ تکرار برای چند module")
    ap.add_argument("--snmp-snapshot", required=True,
                    help="JSONِ ساختارِ collect_hp/collect_canon")
    ap.add_argument("--brand", choices=SUPPORTED_BRANDS,
                    help="پیش‌فرض: کلیدِ brand در snapshot")
    ap.add_argument("--tolerance", type=float, default=5.0,
                    help="تلورانسِ اختلافِ نسبی به درصد (پیش‌فرض ۵)")
    ap.add_argument("--name", default=None, help="برای خواناییِ گزارش (پیش‌فرض: nameِ snapshot)")
    ap.add_argument("--ip", default=None,
                    help="آدرسِ دستگاه در ساختارِ collector (پیش‌فرض: ipِ snapshot)")
    ap.add_argument("--include-volatile", action="store_true",
                    help=f"فیلدهایِ {VOLATILE_PATHS} را هم مقایسه کن (پیش‌فرض: نادیده)")
    ap.add_argument("--json", dest="json_out", help="مسیرِ نوشتنِ گزارشِ JSON")
    ap.add_argument("--md", dest="md_out", help="مسیرِ نوشتنِ گزارشِ markdown")
    args = ap.parse_args(argv)

    try:
        metrics = load_exporter_metrics(args.exporter_capture)
        doc, snapshot_shape, brand = load_snapshot(args.snmp_snapshot, args.brand)
    except InputError as exc:
        print(f"ورودیِ ناقص/ناسازگار: {exc}", file=sys.stderr)
        return 2

    # ip/name are included in the collector structure, but they do not come from SNMP; To compare them
    # Don't mess with the snapshot itself, they are removed from this snapshot (except --ip/--name).
    ip = args.ip or snapshot_shape.get("ip") or args.exporter_capture[0]
    name = args.name if args.name is not None else snapshot_shape.get("name", "")
    exporter_shape = exporter_to_collector_shape(ip, brand, metrics, name=name)
    if exporter_shape is None:          # It is not practical; load_snapshot censored the brand
        print("آینه خروجیِ None داد (برند پشتیبانی‌نشده؟)", file=sys.stderr)
        return 2

    ignored = () if args.include_volatile else VOLATILE_PATHS
    rows, summary = compare_shapes(exporter_shape, snapshot_shape, args.tolerance,
                                   ignore=ignored)
    rep = {
        "brand": brand,
        "tolerance_pct": args.tolerance,
        "inputs": {"exporter_capture": [str(p) for p in args.exporter_capture],
                   "snmp_snapshot": str(args.snmp_snapshot)},
        "pdu": read_pdu_counts(metrics, doc),
        "ignored_paths": list(ignored),
        "summary": summary,
        "rows": rows,
        "diff_rows": [r for r in rows if r["verdict"] == "diff"],
    }
    payload = json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=1,
                         default=str) + "\n"
    md = render_markdown(rep)
    if args.json_out:
        pathlib.Path(args.json_out).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    if args.md_out:
        pathlib.Path(args.md_out).write_text(md, encoding="utf-8")
    else:
        print(md)

    print(f"خلاصه: {summary['rows']} سطر / مشترک {summary['common_paths']} / "
          f"ok {summary['ok']} / diff {summary['diff']} / missing {summary['missing']} "
          f"— PDU exporter: {rep['pdu']['exporter']['packets_sent']}")
    if summary["meaningful_common_paths"] == 0:
        print("هیچِ مسیرِ مشترکِ معناداری (حالتِ دستگاه) پیدا نشد؛ ورودی‌ها با هم نمی‌خوانند.",
              file=sys.stderr)
        return 2
    return 0 if summary["diff"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
