#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/set_cutover_marker.py
# @brief:        Writes the cutover time marker to the database.
# @details:      Responsibilities: Store the cutover timestamp in the meta table.
#                Main API: parse_iso_utc(), commit_time(), default_db_path(), ensure_meta_table(),
#                read_marker(), write_marker(), count_rows(), main()
#                Side effects: database access, runs external processes
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""
tools/set_cutover_marker.py — writes the time cutover marker to the database.

why
----
The time contract of the project (`docs/timezone-contract.md`) is based on a reference moment:
commit `b548e99` (end of phase C). According to `docs/cutover-plan.md` §7, this moment should **in itself
The database** should be registered to:

  • Phase F migration can interpret old timestamps relative to the same reference;
  • Later tools know the "boundary" without reading the git history;
  • In phase E, the same row should be transferred to PostgreSQL.

What does it make?
----------------
``meta(key, value, updated_at)'' table — **the only schema addition in phase E.0** and
idempotent: re-execution does not create a duplicate row but updates the value
(`INSERT... ON CONFLICT(key) DO UPDATE' — this expression itself is also valid in PostgreSQL
and has the form of phase E from the beginning).

Typed key: `timezone_cutover_at` with value ISO-8601 **UTC aware**
(like `2026-09-16T09:31:00+00:00`).

Default date: `b548e99` commit time from git (`git show -s --format=%cI b548e99`).

Exit code:
    0 = written (or --show/--verify successful)
    1 = error (validate date, database, write)
    2 = requires human action (no database file; default date not available from git)

Example:
    python tools/set_cutover_marker.py --show
    python tools/set_cutover_marker.py
    python tools/set_cutover_marker.py --date 2026-10-05T00:00:00Z
    python tools/set_cutover_marker.py --db archives\\logs.db.bak --date 2026-10-05T00:00:00+00:00
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

MARKER_KEY = "timezone_cutover_at"
#: time contract reference moment: commit end of phase C (`docs/timezone-contract.md`).
DEFAULT_COMMIT = "b548e99"

DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

#:upset form — intentionally the same as valid in PostgreSQL (no INSERT OR REPLACE).
UPSERT_META = """
INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
"""


# ─────────────────────────────────────────────────────────────
# Pure functions
# ─────────────────────────────────────────────────────────────

def parse_iso_utc(text: str):
    """Accepts ISO string **only if it is aware and in UTC**.

    Output: (datetime, None) on success, or (None, clear_message) on failure.
    Why it's strict: The cutover token is the reference for interpreting all old timestamps; If
    saved locally, all phase F migrations start with the 3:30 error.
    """
    t = (text or "").strip()
    if not t:
        return None, "مقدار خالی است؛ یک ISO UTC بدهید مثل 2026-10-05T00:00:00Z"
    candidate = t[:-1] + "+00:00" if t.endswith(("Z", "z")) else t
    try:
        dt = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None, (f"«{text}» ISO معتبر نیست. نمونه‌ی درست: 2026-10-05T00:00:00Z "
                      f"یا 2026-10-05T00:00:00+00:00")
    if dt.tzinfo is None:
        return None, (f"«{text}» منطقه‌ی زمانی ندارد (naive). باید UTC باشد: "
                      f"2026-10-05T00:00:00Z")
    if dt.utcoffset() != _dt.timedelta(0):
        return None, (f"«{text}» در UTC نیست (offset={dt.utcoffset()}). اول به UTC تبدیل "
                      f"کنید، مثلاً با: python -c \"import datetime as d;print(d.datetime."
                      f"fromisoformat('{candidate}').astimezone(d.timezone.utc).isoformat())\"")
    return dt.astimezone(_dt.timezone.utc), None


def commit_time(commit: str = DEFAULT_COMMIT, repo: Path = None):
    """Commit time from git (`%cI` = ISO aware). Output: (string, None) or (None, message)."""
    cmd = ["git", "show", "-s", "--format=%cI", commit]
    try:
        out = subprocess.run(cmd, cwd=str(repo) if repo else None,
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"اجرای git ممکن نشد: {exc}"
    if out.returncode != 0:
        return None, (f"کامیت «{commit}» در این مخزن پیدا نشد "
                      f"({out.stderr.strip() or 'بدون پیام'})")
    value = out.stdout.strip()
    if not value:
        return None, f"خروجی git برای «{commit}» خالی بود"
    return value, None


def default_db_path() -> str:
    """Default database path from `config/settings.py` — with AST, no import (no side effects)."""
    import ast  # noqa: PLC0415
    settings = Path(__file__).resolve().parent.parent / "config" / "settings.py"
    try:
        tree = ast.parse(settings.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "logs.db"
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "DB_PATH" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return "logs.db"


# ─────────────────────────────────────────────────────────────
# Working with databases
# ─────────────────────────────────────────────────────────────

def ensure_meta_table(conn) -> None:
    """Create the meta table if it does not exist."""

    conn.execute(DDL_META)


def read_marker(conn, key: str = MARKER_KEY):
    """Return the stored cutover marker, or None."""

    try:
        row = conn.execute("SELECT key, value, updated_at FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return {"key": row[0], "value": row[1], "updated_at": row[2]} if row else None


def write_marker(conn, value_iso: str, now_iso: str, key: str = MARKER_KEY) -> None:
    """Store the cutover marker timestamp."""

    conn.execute(UPSERT_META, (key, value_iso, now_iso))


def count_rows(conn, table: str = "meta") -> int:
    """Return the number of rows in a table (default: meta)."""

    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# ─────────────────────────────────────────────────────────────
# main route
# ─────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    """Command-line entry point that writes the cutover marker."""

    p = argparse.ArgumentParser(
        prog="set_cutover_marker.py",
        description="ثبت نشانه‌ی cutover زمانی در جدول meta (idempotent؛ فاز E.0/E.0.13).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="مثال:\n"
               "  python tools/set_cutover_marker.py --show\n"
               "  python tools/set_cutover_marker.py                       # پیش‌فرض: زمان کامیت b548e99\n"
               "  python tools/set_cutover_marker.py --date 2026-10-05T00:00:00Z\n",
    )
    p.add_argument("--db", default="", help="مسیر فایل SQLite (پیش‌فرض: DB_PATH از config/settings.py)")
    p.add_argument("--date", default="", help="لحظه‌ی cutover به ISO **UTC** (پیش‌فرض: زمان کامیت b548e99)")
    p.add_argument("--commit", default=DEFAULT_COMMIT, help=f"کامیت مرجع برای تاریخ پیش‌فرض (پیش‌فرض: {DEFAULT_COMMIT})")
    p.add_argument("--key", default=MARKER_KEY, help=f"کلید نشانه در جدول meta (پیش‌فرض: {MARKER_KEY})")
    p.add_argument("--show", action="store_true", help="فقط نمایش مقدار فعلی؛ هیچ نوشتنی نمی‌کند")
    p.add_argument("--verify", action="store_true", help="بررسی وجود/اعتبار نشانه (exit 1 اگر نبود)")
    p.add_argument("--allow-create", action="store_true", help="اگر فایل دیتابیس نیست، بساز (پیش‌فرض: خطا)")
    args = p.parse_args(argv)

    db_path = args.db or default_db_path()
    exists = os.path.exists(db_path)

    if not exists and not args.allow_create:
        print(f"✗ فایل دیتابیس پیدا نشد: {db_path}", file=sys.stderr)
        print("  برای ساخت یک فایل تازه (مثلاً روی کپی بکاپ): --allow-create", file=sys.stderr)
        return EXIT_NEEDS_ACTION

    if args.show or args.verify:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            marker = read_marker(conn, args.key)
        except sqlite3.OperationalError as exc:
            print(f"✗ خواندن از دیتابیس ممکن نشد: {exc}", file=sys.stderr)
            return EXIT_ERROR
        finally:
            conn.close()
        if not marker:
            print(f"• نشانه‌ی «{args.key}» در {db_path} ثبت نشده است.")
            return EXIT_ERROR if args.verify else EXIT_OK
        ok = parse_iso_utc(marker["value"])[0] is not None
        print(f"کلید       : {marker['key']}")
        print(f"مقدار      : {marker['value']}  {'(ISO UTC معتبر ✔)' if ok else '(⚠ نامعتبر!)'}")
        print(f"به‌روزرسانی: {marker['updated_at']}")
        print(f"دیتابیس    : {db_path}")
        return EXIT_OK if (ok or not args.verify) else EXIT_ERROR

    # ─── Determining the value ───
    if args.date:
        dt, err = parse_iso_utc(args.date)
        if err:
            print(f"✗ {err}", file=sys.stderr)
            return EXIT_ERROR
        value = dt.isoformat()
        source = "--date"
    else:
        value, err = commit_time(args.commit, repo=Path(__file__).resolve().parent.parent)
        if err:
            print(f"✗ تاریخ پیش‌فرض از git خوانده نشد: {err}", file=sys.stderr)
            print("  راه‌حل: تاریخ را صریح بدهید: --date 2026-10-05T00:00:00Z", file=sys.stderr)
            return EXIT_NEEDS_ACTION
        dt, err = parse_iso_utc(value)
        if err:
            print(f"✗ خروجی git قابل تفسیر نبود ({value}): {err}", file=sys.stderr)
            return EXIT_ERROR
        value = dt.isoformat()
        source = f"git {args.commit}"

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        ensure_meta_table(conn)
        before = read_marker(conn, args.key)
        write_marker(conn, value, now, args.key)
        conn.commit()
        after = read_marker(conn, args.key)
        n_rows = count_rows(conn)
    except sqlite3.Error as exc:
        print(f"✗ نوشتن نشانه ناموفق بود: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        conn.close()

    action = "به‌روزرسانی شد" if before else "ثبت شد"
    print(f"✔ نشانه‌ی cutover {action}")
    print(f"   دیتابیس : {db_path}")
    print(f"   کلید    : {args.key}")
    print(f"   مقدار   : {after['value']}   (منبع: {source})")
    print(f"   زمان ثبت: {after['updated_at']}")
    print(f"   سطرهای meta: {n_rows}  (idempotent: اجرای دوباره سطر جدید نمی‌سازد)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
