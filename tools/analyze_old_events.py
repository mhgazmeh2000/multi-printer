#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/analyze_old_events.py
# @brief:        Analysis script for historical events: determines what each timestamp means.
# @details:      Responsibilities: Classify old event timestamps and report the cutover analysis.
#                Main API: resolve_cutover(), parse_ts(), classify(), analyze(), print_report(),
#                main()
#                Side effects: database access, runs external processes, file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""Analysis of historical events: which timestamp means what? — Step E.0.2.

Problem (Contradiction Z): Paragraph 4 of Section 6 of the document said ``docs/timezone-contract.md''
"Strings with Z and offsets intact" — i.e. assume Z is UTC. But section 3.5
The same document shows that ``CARTRIDGE_CHANGED'' lines were written **before D.6**
They were labeled ``Z'' and their value was local time (210 minute bug). So "Z = UTC"
It is incorrect for historical data and this inconsistency must be resolved before migration.

This tool is **read only** (connection with URI ``mode=ro'') and for each historical row
Determines which category is:

    naive ⇒ local time (Asia/Tehran)
    Z before cutover ⇒ fake tag; The value is actually local time ⚠
    Z after cutover ⇒ Actual UTC
    offset ⇒ ambiguous (for example +03:30 can also be "locally aware") ⚠
    Unbreakable ⇒ Manual review

"cutover" = the moment when the modified version of D.6 was activated on the same server (not the moment
commit). If ``--cutover'' is not given, the tool tries the commit date ``b548e99''
read from git and explicitly warns that this is only an approximation.

Execution:

    python tools/analyze_old_events.py --db logs.db
    python tools/analyze_old_events.py --db logs.db --cutover 2026-10-05T08:00:00+00:00
    python tools/analyze_old_events.py --db logs.db --json report.json

Exit codes:
    0 = all rows on (definitive interpretation)
    2 = There is a row/category requiring manual decision
    1 = executable error (DB not read)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

# Time tables and columns that this tool summarizes
TIME_TABLES = {
    "logs": ("timestamp", "type"),
    "security_events": ("timestamp", "event_type"),
    "toner_history": ("timestamp", None),
    "sensor_readings": ("timestamp", None),
    "printer_counters": ("updated_at", None),
}

_ISO_OFFSET = re.compile(r"[+-]\d{2}:?\d{2}$")
_D6_COMMIT = "b548e99"  # Commit to fix the timing bug of the cartridge engine (phase D.6)


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a command and return its stdout, or an empty string on error."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, shell=False, errors="replace")
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def resolve_cutover(explicit: str) -> tuple[datetime | None, str]:
    """Determining the moment of cutover: from CLI, otherwise approximate commit time D.6."""
    if explicit:
        try:
            return parse_ts(explicit), "--cutover (صریح)"
        except ValueError as exc:
            print(f"[WARN] --cutover نامعتبر است: {exc} ⇒ بدون cutover ادامه می‌دهیم")
    iso = _run(["git", "show", "-s", "--format=%cI", _D6_COMMIT])
    if iso:
        try:
            return parse_ts(iso), f"تقریب: زمان کامیت {_D6_COMMIT} در همین مخزن"
        except ValueError:
            pass
    return None, "نامعلوم"


def parse_ts(raw: str) -> datetime:
    """Converts ISO-8601 to datetime aware (UTC); Z and offset are supported."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("رشتهٔ خالی")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)  # naive ⇒ is interpreted as such
    return dt.astimezone(timezone.utc)


def classify(raw: str, cutover: datetime | None) -> str:
    """The handle of a timestamp value; Only with string evidence, no DB."""
    ts = (raw or "").strip()
    if not ts:
        return "empty"
    if ts.endswith(("Z", "z")):
        try:
            dt = parse_ts(ts)
        except ValueError:
            return "unparsable"
        if cutover is None:
            return "z_cutover_unknown"
        return "z_before_cutover" if dt < cutover else "z_after_cutover"
    if _ISO_OFFSET.search(ts[10:]):
        return "offset_aware"
    try:
        datetime.fromisoformat(ts)
        return "naive_local"
    except ValueError:
        return "unparsable"


_MEANING = {
    "naive_local": "ساعت محلی (Asia/Tehran)",
    "z_before_cutover": "⚠ برچسب Z جعلی؛ مقدار در واقع ساعت محلی است",
    "z_after_cutover": "UTC واقعی",
    "z_cutover_unknown": "⚠ Z دیده شد ولی cutover نامعلوم است ⇒ تصمیم دستی",
    "offset_aware": "⚠ مبهم (offset صریح؛ باید بازبینی شود)",
    "unparsable": "⚠ غیرقابل تجزیه ⇒ بازبینی دستی",
    "empty": "⚠ خالی",
}


def _table_summary(cur, table: str, ts_col: str, extra_col: str | None,
                   cutover: datetime | None, sample: int) -> dict:
    """count categories for a table; without any writing."""
    res = {"table": table, "exists": True, "total": 0, "by_class": {},
           "samples": [], "by_extra": {}}
    try:
        cur.execute(f"SELECT COUNT(1) FROM {table}")
        res["total"] = int(cur.fetchone()[0])
    except sqlite3.Error as exc:
        res["exists"] = False
        res["error"] = str(exc)
        return res
    if res["total"] == 0:
        return res

    cols = ts_col + (f", {extra_col}" if extra_col else "")
    try:
        cur.execute(f"SELECT {cols} FROM {table} ORDER BY rowid DESC LIMIT 5000")
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        res["error"] = f"select: {exc}"
        return res

    for row in rows:
        ts = str(row[0] or "")
        kind = classify(ts, cutover)
        res["by_class"][kind] = res["by_class"].get(kind, 0) + 1
        if extra_col:
            label = str(row[1] or "?")
            res["by_extra"].setdefault(label, 0)
            res["by_extra"][label] += 1
            if kind in ("z_before_cutover", "z_cutover_unknown",
                        "offset_aware", "unparsable") and \
                    len(res["samples"]) < sample:
                res["samples"].append(
                    {"timestamp": ts, extra_col: label, "class": kind,
                     "meaning": _MEANING.get(kind, kind)}
                )
    return res


def analyze(db_path: str, cutover: datetime | None, sample: int) -> dict:
    """Analyse historical event timestamps around a cutover time."""

    report = {"db": os.path.abspath(db_path), "exists": os.path.exists(db_path),
              "tables": [], "error": ""}
    if not report["exists"]:
        report["error"] = "فایل DB پیدا نشد"
        return report
    try:
        conn = sqlite3.connect(f"file:{report['db']}?mode=ro", uri=True,
                               timeout=10.0)
    except sqlite3.Error as exc:
        report["error"] = f"اتصال فقط‌خواندنی ناموفق: {exc}"
        return report
    try:
        cur = conn.cursor()
        for table, (ts_col, extra) in TIME_TABLES.items():
            report["tables"].append(
                _table_summary(cur, table, ts_col, extra, cutover, sample)
            )
    finally:
        conn.close()
    return report


def print_report(rep: dict, cutover: datetime | None, cutover_src: str) -> int:
    """Print the event-timestamp analysis report and return an exit code."""

    print("=" * 72)
    print("تحلیل رویدادهای تاریخی — فاز E.0.2 (فقط خواندن)")
    print("=" * 72)
    print(f"[INFO] DB: {rep['db']}")
    if cutover:
        print(f"[INFO] cutover ≈ {cutover.isoformat()}  ({cutover_src})")
        print("       ↳ ملاک واقعی، لحظهٔ فعال‌شدن نسخهٔ اصلاح‌شده روی همان سرور است،")
        print("         نه زمان کامیت؛ اگر deploy دیرتر بوده، --cutover را دقیق بدهید.")
    else:
        print("[WARN] cutover نامعلوم ⇒ ردیف‌های Z دستهٔ «نیاز به تصمیم دستی» می‌شوند.")

    if rep.get("error"):
        print(f"[FAIL] {rep['error']}")
        return 1

    manual = 0
    for t in rep["tables"]:
        if not t.get("exists"):
            print(f"[WARN] جدول {t['table']}: در دسترس نیست ({t.get('error', '')})")
            continue
        total = t["total"]
        if total == 0:
            print(f"[INFO] جدول {t['table']}: ۰ ردیف")
            continue
        parts = []
        for kind, n in sorted(t["by_class"].items()):
            parts.append(f"{kind}={n}")
            if kind in ("z_before_cutover", "z_cutover_unknown",
                        "offset_aware", "unparsable", "empty"):
                manual += n
        print(f"[INFO] جدول {t['table']}: {total} ردیف | " + ", ".join(parts))
        for kind, n in sorted(t["by_class"].items()):
            if kind in _MEANING:
                print(f"       · {kind}: {_MEANING[kind]}")
        if t["by_extra"]:
            top = sorted(t["by_extra"].items(), key=lambda kv: -kv[1])[:8]
            print("       · بر اساس نوع: " +
                  ", ".join(f"{k}={v}" for k, v in top))
        for s in t["samples"]:
            label = s.get("type") or s.get("event_type") or ""
            print(f"       » نمونه: {s['timestamp']}  [{label}] → {s['meaning']}")

    print("-" * 72)
    print("قاعدهٔ تفسیر در migration (خلاصهٔ سند قرارداد، بخش‌های ۲/۳.۵/۷):")
    print("  · naive بدون Z یا offset      ⇒ ZoneInfo(\"Asia/Tehran\") ⇒ TIMESTAMPTZ")
    print("  · Z پیش از cutover             ⇒ برچسب جعلی ⇒ ZoneInfo(\"Asia/Tehran\")")
    print("  · Z پس از cutover              ⇒ UTC واقعی ⇒ همان‌طور")
    print("  · offset صریح (+03:30 و…)      ⇒ بازبینی دستی (محلی‌آگاهانه یا UTC+3:30؟)")
    print("-" * 72)
    if manual:
        print(f"[WARN] {manual} ردیف نیازمند تصمیم/بازبینی دستی است ⇒ کد خروج 2")
        return 2
    print("[OK] هیچ ردیف مبهمی یافت نشد (تناقض Z در این DB صفر است).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point of the event-timestamp analysis."""

    ap = argparse.ArgumentParser(
        description="تحلیل معنی timestampهای تاریخی (فقط خواندن) — گام E.0.2")
    ap.add_argument("--db", default="logs.db", help="مسیر logs.db")
    ap.add_argument("--cutover", default="",
                    help="لحظهٔ فعال‌شدن نسخهٔ D.6 روی سرور (ISO-8601 با offset)")
    ap.add_argument("--sample", type=int, default=5,
                    help="تعداد نمونهٔ ردیف‌های مبهم (پیش‌فرض ۵)")
    ap.add_argument("--json", dest="json_path", default="",
                    help="نوشتن گزارش ماشینی در این مسیر")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    cutover, src = resolve_cutover(args.cutover)
    rep = analyze(args.db, cutover, args.sample)
    rep["cutover"] = cutover.isoformat() if cutover else None
    rep["cutover_source"] = src
    rep["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    code = print_report(rep, cutover, src)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
        print(f"[INFO] گزارش JSON نوشته شد: {args.json_path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
