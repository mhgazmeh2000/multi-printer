#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/cleanup_fake_gaps.py
# @brief:        Removes fake historical PRINT_GAP events.
# @details:      Responsibilities: Find and delete fake gap events from the log table.
#                Main API: find_fake_gaps(), main()
#                Side effects: database access
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""
Cleaning up fake historical PRINT_GAPs (tools/cleanup_fake_gaps.py)

Background: Older versions of commit 24, after actual reboot of some template printers
"Counter→0 then return to NVRAM value" is counted as "New Print" and an event
They recorded a similar PRINT_GAP estimate (usually several thousand pages). These events are none
They don't have real consumption counters (audit_pages audit detected them with exact signature).

Fake GAP signature (all three terms):
  1) details.prev_total == 0 (the interval starts from the zero counter after the reboot)
  2) A COUNTER_RESET event is the same device ≤15 minutes ago
     its prev_total matches the current_total of the chat (± tolerance)
  3) Chat pages == current_total

Modes:
    python tools/cleanup_fake_gaps.py # preview (dry-run) — makes no changes
    python tools/cleanup_fake_gaps.py --apply # real deletion (with automatic backup of logs.db.bak_<ts>)
    python tools/cleanup_fake_gaps.py --db path/to/logs.db --apply
"""
import argparse
import datetime as _dt
import json
import os
import shutil
import sqlite3
import sys

WINDOW_MIN = 15      # The maximum distance between reset and gap
TOL_ABS, TOL_RATIO = 200, 0.005


def _fmt(n):
    """Format an integer with thousands separators."""
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def find_fake_gaps(conn):
    """List of PRINT_GAP lines signed by reboot-restore."""
    resets_by_ip = {}
    for ip, ts, det in conn.execute("SELECT printer_ip, timestamp, details FROM logs WHERE type='COUNTER_RESET'"):
        try:
            d = json.loads(det or "{}")
        except (ValueError, TypeError):
            continue
        resets_by_ip.setdefault(ip, []).append((ts, d.get("prev_total")))

    fakes, unknown = [], []
    for rid, ip, ts, pages, det in conn.execute(
            "SELECT id, printer_ip, timestamp, pages, details FROM logs WHERE type='PRINT_GAP' ORDER BY timestamp"):
        try:
            d = json.loads(det or "{}")
        except (ValueError, TypeError):
            d = {}
        pv, cv = d.get("prev_total"), d.get("current_total")
        matched = None
        if pv == 0 and cv is not None:
            for rts, rprev in resets_by_ip.get(ip, []):
                if rprev is None:
                    continue
                try:
                    dt = (_dt.datetime.fromisoformat(ts[:19]) - _dt.datetime.fromisoformat(rts[:19])).total_seconds()
                except ValueError:
                    continue
                tol = max(TOL_ABS, int(rprev * TOL_RATIO))
                if 0 <= dt <= WINDOW_MIN * 60 and abs(cv - rprev) <= tol:
                    matched = (rts, rprev)
                    break
        row = {"id": rid, "ip": ip, "ts": ts, "pages": pages, "cur": cv, "reset": matched, "details": d}
        (fakes if matched else unknown).append(row)
    return fakes, unknown


def main():
    """Command-line entry point that removes fake PRINT_GAP events."""

    ap = argparse.ArgumentParser(description="پاک‌سازی PRINT_GAPهای جعلی reboot-restore")
    ap.add_argument("--db", default="logs.db")
    ap.add_argument("--apply", action="store_true", help="بدون این پرچم فقط پیش‌نمایش انجام می‌شود")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ دیتابیس پیدا نشد: {args.db}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(args.db)
    fakes, unknown = find_fake_gaps(conn)

    total_pages = sum(r["pages"] or 0 for r in fakes)
    print("═" * 92)
    print(f"🧹 پاک‌سازی PRINT_GAPهای جعلی (امضای reboot-restore) | DB: {args.db}")
    print("═" * 92)
    print(f"شناسایی شد: {len(fakes)} رویداد جعلی | مجموع صفحات ساختگی: {_fmt(total_pages)}")
    if fakes:
        print("─" * 92)
        for r in fakes:
            print(f"  [{r['ts'][:19]}] {r['ip']:<14} {_fmt(r['pages']):>8} صفحه"
                  f"  ← ریست [{r['reset'][0][:19]}] prev={_fmt(r['reset'][1])}")
    if unknown:
        print("─" * 92)
        print("⚠️ GAPهایی با امضای ناشناخته (دست‌نخورده می‌مانند — خودتان بررسی کنید):")
        for r in unknown:
            print(f"  [{r['ts'][:19]}] {r['ip']:<14} {_fmt(r['pages']):>8} صفحه  prev={r['details'].get('prev_total')} cur={r['cur']}")

    if not args.apply:
        print("\nحالت پیش‌نمایش (dry-run). برای حذف واقعی:  --apply")
        conn.close()
        return

    if not fakes:
        print("\nچیزی برای حذف نیست.")
        conn.close()
        return

    # Compulsory backup
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{args.db}.bak_{ts}"
    conn.close()
    shutil.copy2(args.db, bak)
    print(f"\n💾 بکاپ اجباری: {bak}")

    conn = sqlite3.connect(args.db)
    ids = [r["id"] for r in fakes]
    q = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM logs WHERE id IN ({q}) AND type='PRINT_GAP'", ids)
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    print(f"🗑 حذف شد: {deleted} سطر (صفحات ساختگی: {_fmt(total_pages)})")
    print("   حالا ممیزی را دوباره اجرا کنید:  python tools/audit_pages.py --days 7")
    if unknown:
        print("   (GAPهای ناشناخته دست‌نخورده ماندند.)")


if __name__ == "__main__":
    main()
