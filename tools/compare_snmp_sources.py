#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""compare_snmp_sources.py — مقایسهٔ «دو منبع» برای یک پرینتر (گام G.6).

دو طرفِ مقایسه:
  ۱) خروجیِ snmp_exporter (متنِ exposition که از /snmp?target=… گرفته‌اید)
  ۲) snapshotِ collectorِ legacy (JSONِ همان ساختاری که collect_hp/collect_canon برمی‌گرداند؛
     خروجیِ `core.enhanced_collector.collect_enhanced` یا یک لاگِ snapshot در `logs.db`)

چرا از «آینه» عبور می‌کند: واحدِ مقایسه باید *مفهوم* باشد نه *نامِ متریک*. متریکِ
`hrPrinterTotal` در مقابلِ `counters.total` است، پس ابتدا capture با همان
`core.collectors.exporter_adapter.exporter_to_collector_shape` (آینهٔ تست‌شده در G.5) به
ساختارِ collector درمی‌آید و بعد *مسیرهایِ مشترکِ* دو ساختار سطر‌به‌سطر مقایسه می‌شوند.
این‌طور مقایسه دقیقاً همان چیزی را می‌سنجد که روشن‌کردنِ `USE_SNMP_EXPORTER=1` تغییر
می‌دهد — نه یک ترجمهٔ دستیِ تازه که خودش باگ داشته باشد.

نکتهٔ G.6 (طبقِ دستورِ مالک): اینجا **هیچ عددی دربارهٔ PDU در repo ثبت نمی‌شود**. ابزار
سنجه‌اش با fixtureهای مصنوعی است؛ عددِ واقعیِ PDU را خودِ مالک روی سرورش از همین گزارش
می‌خواند.

کاربرد:
    python3 tools/compare_snmp_sources.py \\
        --exporter-capture /tmp/exp_hp.txt \\
        --snmp-snapshot /tmp/snap.json \\
        [--brand hp] [--tolerance 5] [--json /tmp/cmp.json] [--md /tmp/cmp.md]

خروجیِ exit:
    0 = اختلافِ معنادار نیست (هیچ سطرِ diff)   1 = اختلافِ معنادار هست
    2 = ورودیِ ناقص/ناسازگار (فایل نیست، JSON خراب است، برند پشتیبانی‌نشده، صفرِ مسیرِ مشترک)
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

# شمارشگرهایِ PDU که exporter در همان پاسخِ exposition می‌گذارد. هیچ‌کدام در snmp.yml
# نبود؛ اگر capture شما آن‌ها را دارد (پیش‌فرضِ exporter) از همین‌جا خوانده می‌شود،
# وگرنه گزارش صریحاً N/A می‌نویسد (و ما هم در repo عددی ثبت نمی‌کنیم).
PDU_METRICS = ("snmp_scrape_packets_sent", "snmp_scrape_pdus_returned",
               "snmp_scrape_duration_seconds")

# فیلدهایِ ناپایدارِ *ساعتی* — حالتِ دستگاه را نمی‌سنجند، پس مقایسه نمی‌شوند (در گزارش
# صریحاً فهرست می‌شوند تا چیزی پنهان نباشد؛ با --include-volatile دوباره فعال می‌شوند).
VOLATILE_PATHS = ("last_poll", "poll_ms")

# کلیدهایی که «حالتِ دستگاه» نیستند (برچسبِ ما + ساعت‌ها)؛ برایِ گیتِ ورودی از شمارشِ مشترک
# بیرون گذاشته می‌شوند و در جدولِ زیرِ «کاستیِ آینه» هم گیج‌کننده نبودن‌شان مهم است.
IDENTITY_PATHS = ("brand", "ip", "name", "online")


# ─────────────────────────────────────────────────────────────────────────────
# ۱) صاف‌کردنِ ساختار → مسیرهایِ نقطه‌چین
# ─────────────────────────────────────────────────────────────────────────────
def flatten(obj, prefix=""):
    """dict/list را به {مسیر: مقدارِ اسکالر} تبدیل می‌کند (کاملاً قطعی، ترتیبِ کلید).

    مقدارهایِ غیراسکالرِ باقی‌مانده (dict/list تهی) با لیبلِ «(empty)» نگه داشته می‌شوند تا
    «کم» با «هستِ ولی تهی» اشتباه گرفته نشود.
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
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "num" if value is not None else "num"
    if isinstance(value, str):
        return "text"
    return "other"


# ─────────────────────────────────────────────────────────────────────────────
# ۲) مقایسه
# ─────────────────────────────────────────────────────────────────────────────
def compare_shapes(exporter_shape, snapshot_shape, tolerance_pct, ignore=VOLATILE_PATHS):
    """سطرهایِ مقایسه را می‌سازد. بازمی‌گرداند: (rows, summary).

    verdictها:
      ok      — برابر یا اختلافِ نسبی <= tolerance
      diff    — اختلافِ نسبی > tolerance (یا رشتهٔ نابرابر)
      missing — یک طرف مقدارِ واقعی ندارد (None / نبودنِ مسیر) → اختلافِ معنادار **نیست**
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
    # کلیدهایِ هویتی/ساعتی به‌خودی‌خود مشترک‌اند؛ «صفرِ مسیرِ مشترک» باید معنادارسنجیده شود،
    # وگرنه snapshotِ بی‌ربط (که فقط brand درست دارد) از گیتِ ورودی رد می‌شود.
    meaningful = [p2 for p2 in common if p2 not in IDENTITY_PATHS]
    summary = {"rows": len(rows), "common_paths": len(common),
               "meaningful_common_paths": len(meaningful), "ok": n_ok, "diff": n_diff,
               "missing": n_missing,
               "only_in_exporter": sorted(set(e_flat) - set(s_flat)),
               "only_in_snapshot": sorted(set(s_flat) - set(e_flat))}
    return rows, summary


def _num(v):
    """عدد را در JSON تمیز نگه می‌دارد (int اگر درست بود، وگرنه float؛ NaN/Inf → None)."""
    if isinstance(v, bool):
        return v
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    if f == int(f):
        return int(f)
    return round(f, 4)


# ─────────────────────────────────────────────────────────────────────────────
# ۳) شمارشِ PDU
# ─────────────────────────────────────────────────────────────────────────────
def read_pdu_counts(metrics, snapshot_doc):
    """شمارشگرهایِ exporter (جمع روی همهٔ moduleها) در برابرِ SNMP_PDU_COUNTِ snapshot.

    هیچ مقدارِ پیش‌فرضی اختراع نمی‌شود: هر کدام نبود، رشتهٔ "N/A" می‌ماند و در گزارش هم
    همان‌طور نوشته می‌شود. `per_series` نگه داشته می‌شود تا «جمع‌کردن» قابلِ بازبینی باشد.
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
# ۴) گزارش‌ها
# ─────────────────────────────────────────────────────────────────────────────
def render_markdown(rep):
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
# ۵) main
# ─────────────────────────────────────────────────────────────────────────────
class InputError(Exception):
    pass


def load_exporter_metrics(paths):
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

    # ip/name در ساختارِ collector جا دارند ولی از SNMP نمی‌آیند؛ برای اینکه مقایسهٔ آن‌ها
    # با خودِ snapshot بی‌معنی نشود، از همین snapshot برداشته می‌شوند (مگر --ip/--name).
    ip = args.ip or snapshot_shape.get("ip") or args.exporter_capture[0]
    name = args.name if args.name is not None else snapshot_shape.get("name", "")
    exporter_shape = exporter_to_collector_shape(ip, brand, metrics, name=name)
    if exporter_shape is None:          # عملی نیست؛ load_snapshot برند را سانس کرده
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
