#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP 2 helper for cartridge_test.bat (read-only, ASCII-safe output).

Reads from the project's logs.db next to the script/server:
  a) printer_counters: manual_override flag + cartridge_id_state
     (identity evidence persisted by the chip-ID detector)
  b) logs: newest CARTRIDGE_CHANGED / REFILL events for the given printer IP

Usage:
    python tools/cartridge_test_step2.py <printer_ip> [db_path]

Output is forced to ASCII: non-ASCII (Persian) chars are escaped as \\uXXXX
so any Windows console shows the data without encoding errors.
"""
import os
import sqlite3
import sys


def esc(value):
    if value is None:
        return "-"
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def connect_ro(db):
    try:
        return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db), uri=True)
    except Exception:
        return sqlite3.connect(db)


def main():
    if len(sys.argv) < 2:
        print("usage: cartridge_test_step2.py <printer_ip> [db_path]")
        return 2
    ip = sys.argv[1]
    db = sys.argv[2] if len(sys.argv) > 2 else "logs.db"
    if not os.path.exists(db) and os.path.exists("logs.db.active"):
        db = "logs.db.active"  # legacy-era safety, new code uses logs.db
    print("[2] db = %s" % os.path.abspath(db))
    if not os.path.exists(db):
        print("[2] ERROR: database not found. Run this from the project folder")
        print("    where logs.db lives (next to run.py).")
        return 1

    conn = connect_ro(db)

    # == a) isolation/override state ==========================================
    try:
        row = conn.execute(
            "SELECT manual_override, cartridge_id_state FROM printer_counters WHERE ip=?",
            (ip,),
        ).fetchone()
    except Exception as exc:
        print("[2] printer_counters read failed: %s" % exc)
        row = None
    if row:
        mo, cid_state = row
        print("[2] manual_override  = %s" % mo)
        if mo:
            print("    >>> manual_override = 1  =>  AUTOMATIC detection is DISABLED on this device.")
            print("    >>> if a real refill/replacement happened, release the manual override")
            print("        from the dashboard and test again.")
        else:
            print("    manual override is OFF (automatic detection allowed).")
        print("[2] cartridge_id_state = %s" % esc(cid_state))
    else:
        print("[2] no printer_counters row for %s (device may not be monitored yet)" % ip)

    # == b) newest cartridge-related events for this device ===================
    try:
        rows = conn.execute(
            """SELECT timestamp, type, severity, pages, message
               FROM logs
               WHERE printer_ip=? AND type IN ('CARTRIDGE_CHANGED','REFILL')
               ORDER BY timestamp DESC LIMIT 12""",
            (ip,),
        ).fetchall()
    except Exception as exc:
        print("[2] logs read failed: %s" % exc)
        rows = []
    print("[2] newest CARTRIDGE_CHANGED/REFILL events for %s: %d" % (ip, len(rows)))
    for ts, typ, sev, pages, msg in rows:
        print("    %s | %-18s | sev=%-7s | pages=%s | %s"
              % (ts, typ, sev, pages if pages is not None else "-", esc(msg)))
    if not rows:
        print("    >>> none found yet for this device")

    # == b2) auto-detected WARNINGs (chip-stuck hints) around this device =====
    try:
        warns = conn.execute(
            """SELECT timestamp, message FROM logs
               WHERE printer_ip=? AND type='WARNING'
               ORDER BY timestamp DESC LIMIT 5""",
            (ip,),
        ).fetchall()
        if warns:
            print("[2] newest WARNING events:")
            for ts, msg in warns:
                print("    %s | %s" % (ts, esc(msg)))
    except Exception:
        pass
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
