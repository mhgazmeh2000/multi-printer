#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/migrate_timestamps_to_utc.py
# @brief:        Migrates SQLite timestamps to the standard UTC 'Z' format (phase E.3).
# @details:      Responsibilities: Convert timestamp values in place and write a manifest.
#                Main API: split_awareness(), convert_value(), connect_ro(), table_columns(),
#                read_cutover(), sha256_of(), scan(), migrate_rows() (+6 more)
#                Side effects: database access, runs external processes, file I/O
# @dependencies: internal: config, core.time_utils
# @notes:        Reads environment variables: DB_BACKEND, PG_DSN. Can be run directly as a script.
# =============================================================================

"""
tools/migrate_timestamps_to_utc.py — Migration of SQLite timestamps to standard UTC-Z format (phase E.3).

what does it do
----------
All the time values of the program tables to **standard project format**
(``2026-10-03T12:00:00.123456Z'' — 27 characters, always UTC, always ``Z'') converts.
The interpretation rule is the same as the locked rule ``docs/timezone-contract.md'' §8.3 and
Classification from the **same function** of the E.0.2 tool (``tools/analyze_old_events.py::classify'')
It comes so that the two tools are never separated:

| The input value | Commentary Output
|---|---|---|
| ``2026-10-01T08:51:39.937749'' (naive) | ``Asia/Tehran'' local time | ``2026-10-01T05:21:39.937749Z'' |
| ``...Z'' before the cutover sign | Fake Label (Bug D.6) ⇒ Local Time | converted |
| ``...Z'' after the cutover sign | Actual UTC | Normalized to standard format

⚠ **Known blind spot of this rule:** The fake row at **3:30 before** the moment of cutover
written real, its local value (which is 3:30 ahead) crosses the mark and "UTC real"
is recognized. This limitation is accepted in the document ``docs/timezone-contract.md'' §8.3
(the sign is the moment of installation of the modified version) and its number with the tool E.0.2
(``tools/analyze_old_events.py'') seen before migration; In the DB of this repository there are zero rows
``Z'' exists, so it has no practical effect.
| ``2026-10-03T09:55:51+00:00'' | explicit offset ⇒ ditto (true UTC) | Standardized
| ``2026-10-03T11:00:00+03:30'' | Explicit offset ⇒ ditto (measured and counted in manifest) | Standardized
| Corrupt / indestructible | **Intact** + Warning | skip |

idempotency (why re-execution doesn't break anything)
---------------------------------------------------
The rule "``Z'' before cutover ⇒ fake" is only true for **old** data, but rows
Migrated can also have a time before cutover (the whole date!). Separation with
**Format** is done, not by time:

  • ``Z'' with 6 microsecond digits (``.ffffff``) ⇒ E.3 standard format ⇒ **True UTC** ⇒ no-op;
  • ``Z'' without microseconds ⇒ old cartridge motor writers
    (``strftime("%Y-%m-%dT%H:%M:%SZ")``) ⇒ the cutover rule is applied.

So run again: every row falls in ``already_standard'' category and no UPDATE is done
Can't (test)

safety
-----
  • ``--dry-run'': only counting, no writing (and no backup);
  • Before each write: backup ``<db>-<YYYYmmdd-HHMMSS>.e3bak'' (named with ``logs.db-*''
    Compatible ⇒ falls in ``.gitignore'' and doesn't pollute git status);
  • Total update in **one transaction**; Any unexpected error ⇒ rollback and exit code 1;
  • ``meta.value'' remains intact: the cutover token itself is a reference to interpretation and should not
    convert (only ``meta.updated_at'' is converted like the others).

exit codes
----------
  0 = migration done (or dry-run). Garbage/Empty rows are only warnings and appear in the manifest.
  1 = execution error (DB missing/unreadable, write error — with rollback)
  2 = action required: no cutover token, or ``Z'' row without reliable token
      (``z_cutover_unknown'') ⇒ automatic interpretation is not possible.

sample
-----
    python tools\\migrate_timestamps_to_utc.py --db logs.db --dry-run
    python tools\\migrate_timestamps_to_utc.py --db logs.db
    python tools\\migrate_timestamps_to_utc.py --db logs.db --manifest e3-manifest.json --quiet
    python tools\\migrate_timestamps_to_utc.py --db logs.db --backup-only
    python tools\\migrate_timestamps_to_utc.py --db logs.db --pg-dsn "host=… dbname=printguard" --backup-only

Backup (E.6.2)
------------
Before writing, it is backed up and **none of the two silent paths are bypassed**:

* **SQLite** (always): physical copy next to file, template ``logs.db-<stamp>.e3bak'' — same
  Previous behavior, no change.
* **PostgreSQL**: if DB_BACKEND=postgresql and ``PG_DSN'' is set (or ``--pg-dsn'' data
  ``pg_dump --format=custom'' in ``printguard-pg-<stamp>.dump'' and then **confirm** with
  ``pg_restore --list'': dump empty/half/no table ⇒ explicit error and stop. No ``pg_dump'' either
  error (with installation message); ``--skip-pg-backup'' is the only way to consciously skip it.
* ``--backup-only'': backup only (PG: verified dump / SQLite: copy) and exit, no scan and
  Without any writing on the database.

sha256 PG backup is registered in manifest (key ``pg_backup'' with toc/tables/sha256); password
libpq is passed to ``PGPASSWORD'' so that it is not seen in command line arguments and manifests.
The reason for this design: finding E.4.11 (paragraph 7.1 docs/postgresql-strictness.md) showed "backup"
On the PG it could be silent **none** and reported as such.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.time_utils import DEFAULT_DISPLAY_TZ, ISO_Z_FORMAT  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

CUTOVER_KEY = "timezone_cutover_at"

#: Time columns of each table — extracted from the actual DDL of `logs.db`
#: (`PRAGMA table_info`), not from memory. If there is no table/column in DB, give warning
#: yes and no (compatibility with older versions of schema).
TIME_COLUMNS = {
    "logs": ["timestamp"],
    "security_events": ["timestamp", "created_at"],
    "toner_history": ["timestamp"],
    "sensor_readings": ["timestamp"],
    "printer_counters": ["updated_at"],
    "cartridge_state": ["updated_at", "learned_at", "last_refill_at"],
    "cartridge_yield_profiles": ["updated_at"],
    "yield_samples": ["created_at"],
    "toner_snapshots_v2": ["timestamp"],
    "users": ["created_at", "updated_at", "last_login_at"],
    "meta": ["updated_at"],
}

#: Columns intentionally left intact (reference to commentary, not historical data).
PROTECTED_COLUMNS = {("meta", "value")}

#: `Z' with 6 microsecond digits ⇒ E.3 standard format.
_STANDARD_Z = re.compile(r"\.\d{6}Z$")

TEHRAN = ZoneInfo(DEFAULT_DISPLAY_TZ)


def _load_classify():
    """
    Imports the ``classify'' function from the E.0.2 utility (one implementation, two consumers).

    Why file-oriented import: the ``tools/'' folder is not a package; with ``spec_from_file_location''
    The same module is loaded independently, and if the categorization logic ever changes, both tools
    They change together (not separately).
    """
    path = PROJECT_ROOT / "tools" / "analyze_old_events.py"
    spec = importlib.util.spec_from_file_location("analyze_old_events", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


classify = _load_classify()


# ─────────────────────────────────────────────────────────────
# Convert values
# ─────────────────────────────────────────────────────────────

def split_awareness(raw: str) -> tuple:
    """
    returns (string, handle) for a raw value; Apart from rule E.3 for idempotency.

    Returns:
        ``(type, value)'' where the type is one of:
        ``standard'' (E.3 standard format — no-op), ``naive'', ``z'', ``offset'',
        ``empty'', ``unparsable''.
    """
    ts = (raw or "").strip()
    if not ts:
        return "empty", ts
    if ts.endswith(("Z", "z")):
        if _STANDARD_Z.search(ts):
            return "standard", ts
        return "z", ts
    if re.search(r"[+-]\d{2}:?\d{2}$", ts[10:]):
        return "offset", ts
    try:
        datetime.fromisoformat(ts)
    except ValueError:
        return "unparsable", ts
    return "naive", ts


def convert_value(raw: str, cutover: datetime | None) -> tuple:
    """
    Converts a time value to standard format.

    Args:
        raw: the raw value of the column.
        cutover: cutover signal moment (UTC aware) to distinguish fake ``Z'' from real.

    Returns:
        ``(new_value, category, changed)'' — ``new_value'' for values
        Unreachable is the same as the raw value (no deletion is performed).
    """
    kind, ts = split_awareness(raw)
    if kind == "empty":
        return raw, "empty", False
    if kind == "standard":
        # standard format ⇒ actual UTC; Normalization only (usually no change)
        dt = datetime.strptime(ts, ISO_Z_FORMAT).replace(tzinfo=timezone.utc)
        new = dt.strftime(ISO_Z_FORMAT)
        return new, "already_standard", False
    if kind == "unparsable":
        return raw, "unparsable", False
    if kind == "naive":
        dt = datetime.fromisoformat(ts).replace(tzinfo=TEHRAN)   # Installation local time
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "naive_local", True
    if kind == "offset":
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "offset_aware", True
    # kind == "z": old Z label → cutover rule
    category = classify(ts, cutover)
    if category == "z_cutover_unknown":
        return raw, "z_cutover_unknown", False
    if category == "z_before_cutover":
        # Fake tag: The value is actually local time
        naive = datetime.fromisoformat(ts[:-1])
        dt = naive.replace(tzinfo=TEHRAN)
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "z_before_cutover", True
    if category == "z_after_cutover":
        dt = datetime.fromisoformat(ts[:-1] + "+00:00")
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "z_after_cutover", True
    return raw, "unparsable", False


# ─────────────────────────────────────────────────────────────
# DB auxiliaries
# ─────────────────────────────────────────────────────────────

def connect_ro(path: str) -> sqlite3.Connection:
    """Read-only connection (analysis without any write risk)."""
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def table_columns(conn: sqlite3.Connection, table: str) -> list:
    """Return the column names of a table."""

    try:
        conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1')
    except sqlite3.Error:
        return []
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def read_cutover(conn: sqlite3.Connection) -> tuple:
    """
    Reads the cutover token from the ``meta'' table.

    Returns:
        ``(datetime|None, str)'' — value and source/reason.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (CUTOVER_KEY,)).fetchone()
    except sqlite3.Error:
        return None, "جدول meta نیست"
    if not row or not row[0]:
        return None, f"کلید {CUTOVER_KEY} در meta نیست"
    raw = str(row[0]).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"مقدار نشانه قابل تجزیه نیست: {raw!r}"
    if dt.tzinfo is None:
        return None, f"مقدار نشانه naive است: {raw!r}"
    return dt.astimezone(timezone.utc), f"meta.{CUTOVER_KEY} = {raw}"


def sha256_of(path: str) -> str:
    """Return the SHA-256 hex digest of a file."""

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────
# immigration
# ─────────────────────────────────────────────────────────────

def scan(db_path: str, cutover: datetime, tables: list) -> dict:
    """Batch counting without any writing (dry-run and manifest base)."""
    report = {"tables": {}, "totals": {}, "skipped_samples": []}
    conn = connect_ro(db_path)
    try:
        for table in tables:
            cols = table_columns(conn, table)
            if not cols:
                report["tables"][table] = {"present": False}
                continue
            entry = {"present": True, "columns": {}, "rows": 0,
                     "converted": 0, "skipped": 0, "already_standard": 0,
                     "needs_decision": 0}
            wanted = [c for c in TIME_COLUMNS[table] if c in cols]
            missing = [c for c in TIME_COLUMNS[table] if c not in cols]
            if missing:
                entry["missing_columns"] = missing
            for col in wanted:
                if (table, col) in PROTECTED_COLUMNS:
                    continue
                cats = {}
                for (value,) in conn.execute(f'SELECT "{col}" FROM "{table}"'):
                    _, category, changed = convert_value(value, cutover)
                    cats[category] = cats.get(category, 0) + 1
                    if category == "already_standard":
                        entry["already_standard"] += 1
                    elif changed:
                        entry["converted"] += 1
                    elif category in ("unparsable", "z_cutover_unknown"):
                        entry["skipped"] += 1
                        if category == "z_cutover_unknown":
                            entry["needs_decision"] += 1
                        if len(report["skipped_samples"]) < 20:
                            report["skipped_samples"].append(
                                {"table": table, "column": col, "value": value,
                                 "category": category})
                entry["columns"][col] = cats
            n_rows = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            entry["rows"] = n_rows
            report["tables"][table] = entry
    finally:
        conn.close()
    for key in ("rows", "converted", "skipped", "already_standard", "needs_decision"):
        report["totals"][key] = sum(t.get(key, 0) for t in report["tables"].values())
    return report


def migrate_rows(db_path: str, cutover: datetime, tables: list) -> dict:
    """Execution of conversion in **one transaction**; Output: Dictionary of additions of each table."""
    applied = {}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        for table in tables:
            cols = table_columns(conn, table)
            if not cols:
                continue
            wanted = [c for c in TIME_COLUMNS[table] if c in cols
                      and (table, c) not in PROTECTED_COLUMNS]
            if not wanted:
                continue
            # rowid to identify the row; We do not have tables WITHOUT ROWID (project DDL).
            select = ", ".join(f'"{c}"' for c in wanted)
            rows = conn.execute(f'SELECT rowid, {select} FROM "{table}"').fetchall()
            updates = 0
            for row in rows:
                rowid, values = row[0], row[1:]
                for col, value in zip(wanted, values):
                    new_value, _, changed = convert_value(value, cutover)
                    if changed and new_value != value:
                        conn.execute(
                            f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?',
                            (new_value, rowid))
                        updates += 1
            applied[table] = updates
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return applied


def make_backup(db_path: str) -> str:
    """
    Physical backup before writing.

    The default name is ``<db>-<YYYYmmdd-HHMMSS>.e3bak''; Because with the pattern
    ``logs.db-*'' falls in ``.gitignore'', ``git status'' is not infected.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"{db_path}-{stamp}.e3bak"
    shutil.copy2(db_path, backup)
    return backup


class BackupError(RuntimeError):
    """Backup/Failed — with action message (not traceback)."""


def _tool_path(name: str) -> str:
    """Locate an external tool on PATH and return its path."""
    from shutil import which

    found = which(name)
    if not found:
        raise BackupError(
            f"ابزار `{name}` در PATH نیست. روی Debian/Ubuntu: "
            f"`apt install postgresql-client-16` (یا نسخهٔ منطبق با سرور).")
    return found


def _env_without_password(dsn: str) -> dict:
    """
    Transfers the password from the DSN to the environment so that it is not visible in `ps`/shell history.

    libpq looks at the environment variables before reading the DSN; So PGPASSWORD is the only way
    Pass the password to pg_dump/pg_restore without writing it in the command line argument.
    """
    env = dict(os.environ)
    m = re.search(r"(?:^|\s)password=(\S+)", dsn)
    if m:
        env["PGPASSWORD"] = m.group(1).strip("'\"")
        dsn = re.sub(r"(?:^|\s)password=\S+", " ", dsn).strip()
        env["_PG_DUMP_DSN"] = dsn          # DSN without password, for logging
    else:
        env["_PG_DUMP_DSN"] = dsn
    env.setdefault("PGCONNECT_TIMEOUT", "15")
    return env, dsn


def _discard_unverified(path: str) -> None:
    """Deletes the unverified backup file; If it doesn't, Laveh warns."""
    try:
        os.remove(path)
    except OSError as exc:
        print(f"⚠ فایل بکاپِ نامعتبر پاک نشد ({path}): {exc}", file=sys.stderr)


def verify_pg_dump(path: str, *, expect_tables=()) -> list:
    """
    With `pg_restore --list` it is ensured that the file is indeed a restoreable dump.

    Why this-stage: The successful output of `pg_dump -Fc` is only "big file"; A half-cut dump
    (disk full, server restarted) has both size and is rejected here. ``expect_tables''
    It is also optional: if given, the absence of any name ⇒ error (so "empty dump of wrong schema"
    It does not pass through this gate either).
    """
    pg_restore = _tool_path("pg_restore")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise BackupError(f"فایل dump خالی یا ناموجود است: {path}")
    proc = subprocess.run([pg_restore, "--list", path], capture_output=True, text=True,
                          timeout=300)
    if proc.returncode != 0:
        raise BackupError(
            f"pg_restore --list خطا داد (dump معتبر نیست): {proc.stderr.strip()[:300]}")
    # TOC line format in pg_restore --list: <entry>; <catalog> <oid> TABLE <schema> <name> <owner>
    # (in pg_dump the plain/text of the table and "TABLE DATA" are separated; the second line is intentionally omitted, otherwise
    # The wrong name was returned instead of the table.)
    entries = re.findall(r"^\s*\d+;\s+\d+\s+\d+\s+TABLE(?!\s+DATA)\s+\S+\s+(\S+)",
                         proc.stdout, re.M)
    if not entries:
        raise BackupError("dump هیچ جدولی ندارد — schema هدف را بررسی کنید "
                          "(search_path/نقش/dbname)")
    missing = sorted(set(expect_tables) - set(entries))
    if missing:
        raise BackupError(f"جدول‌های موردانتظار در dump نیستند: {', '.join(missing)}")
    return entries


def take_pg_backup(dsn: str, out_dir: str, *, expect_tables=()) -> dict:
    """
    `pg_dump --format=custom` + Verify with `pg_restore --list`.

    Why custom and not plain: `-Fc` is compressed, selectable with `pg_restore -t/-n`
    is returned, and — more importantly for an operational witness — with `--list` without binding to
    The server is auditable. File name `printguard-pg-<UTC stamp>.dump` in `out_dir`.
    """
    pg_dump = _tool_path("pg_dump")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(out_dir, f"printguard-pg-{stamp}.dump")
    env, safe_dsn = _env_without_password(dsn)
    try:
        proc = subprocess.run([pg_dump, "--format=custom", "--file", out_path, safe_dsn],
                              capture_output=True, text=True, timeout=1800, env=env)
        if proc.returncode != 0:
            raise BackupError(
                f"pg_dump ناموفق بود: {proc.stderr.strip()[:300] or '(بدون stderr)'}")
        entries = verify_pg_dump(out_path, expect_tables=expect_tables)
    except BackupError:
        # `pg_dump -Fc' makes the file **before** success; So a half-baked performance
        # (disk full, connection broken, connection refused) leaves a 0-byte or incomplete file. If
        # Stay, tomorrow someone will see it in `ls printguard-pg-*.dump' and "backup"
        # Assumes - Same "fake backup" error as logged in E.4.11.
        _discard_unverified(out_path)
        raise
    return {
        "kind": "pg_dump_custom",
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "sha256": sha256_of(out_path),
        "toc_entries": len(entries),
        "tables": sorted(set(entries)),
        "dsn": env.get("_PG_DUMP_DSN", ""),        # No password — for manifest
        "taken_at": datetime.now(timezone.utc).strftime(ISO_Z_FORMAT),
    }


def parse_args(argv=None):
    """Parse the command-line arguments of the timestamp migration."""

    ap = argparse.ArgumentParser(
        prog="migrate_timestamps_to_utc.py",
        description="مهاجرت timestampهای SQLite به فرمت استاندارد UTC-Z (فاز E.3).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="کد خروج: 0=انجام شد، 1=خطای اجرایی، 2=نیازمند اقدام (نبود نشانه یا ردیف Z مبهم)\n")
    ap.add_argument("--db", default="", help="مسیر فایل SQLite (پیش‌فرض: config.settings.DB_PATH)")
    ap.add_argument("--cutover", default="",
                    help="نشانهٔ cutover (ISO آگاه)؛ پیش‌فرض: خواندن از جدول meta")
    ap.add_argument("--tables", default="", help="فهرست جدول‌ها با کاما (پیش‌فرض: همه)")
    ap.add_argument("--dry-run", action="store_true", help="فقط شمارش؛ بدون بکاپ و بدون نوشتن")
    ap.add_argument("--no-backup", action="store_true", help="بدون بکاپ (پیش‌فرض: بکاپ می‌گیرد)")
    ap.add_argument("--pg-dsn", default="",
                    help="اگر دیتابیس زنده PostgreSQL است: نشانی libpq برای "
                         "pg_dump -Fc تأییدشده (پیش‌فرض: از PG_DSN وقتی DB_BACKEND=postgresql)")
    ap.add_argument("--backup-out", default="",
                    help="پوشهٔ خروجی بکاپ PG (پیش‌فرض: پوشهٔ جاری)")
    ap.add_argument("--backup-only", action="store_true",
                    help="فقط بکاپ (PG: pg_dump تأییدشده / SQLite: copy فیزیکی) و خارج شدن")
    ap.add_argument("--skip-pg-backup", action="store_true",
                    help="با وجود PG_DSN، بکاپ PG نگیر (برای dry-run روی کپی؛ حالت پیش‌فرض: رد)")
    ap.add_argument("--manifest", default="", help="مسیر فایل manifest JSON")
    ap.add_argument("--quiet", action="store_true", help="خروجی کوتاه")
    return ap.parse_args(argv)


def _pg_dsn_from_env() -> str:
    """``PG_DSN'' only when the application itself is set to postgresql."""
    if (os.environ.get("DB_BACKEND", "") or "").strip().lower() not in ("postgresql", "postgres", "pg"):
        return ""
    return (os.environ.get("PG_DSN", "") or "").strip()


def _resolve_db_path(args) -> str:
    """``--db'' or ``config.settings.DB_PATH'' (lazy import as before)."""
    db_path = (args.db or "").strip()
    if db_path:
        return db_path
    from config import settings
    return getattr(settings, "DB_PATH", "logs.db")


def main(argv=None) -> int:
    """Command-line entry point of the timestamp migration."""

    args = parse_args(argv)
    try:
        db_path = _resolve_db_path(args)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ مسیر دیتابیس معلوم نشد: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # ── E.6.2: PostgreSQL backup, verified with pg_restore --list ─────────
    # This tool overwrites the SQLite file; So "live database" when it is PG,
    # It is exposed to the risk of *importing* modified data, not write exposure
    # directly Decision: Before doing anything, get a recoverable witness from PG. If
    # pg_dump was missing or gave an error ⇒ execution **stops** (no silent backup
    # "taken" does not count; The same lesson that was taken from findings E.4.11).
    pg_backup = None
    dsn = (args.pg_dsn or "").strip() or ("" if args.skip_pg_backup else _pg_dsn_from_env())
    if dsn:
        # Default backup folder: **next to database file**, not current folder - otherwise
        # A typical run from the root of the repository dumps the binary backup in the middle of the working tree
        # (This was revealed by the PG suite itself in E.6.2: 26 dump files in the root).
        pg_out_dir = args.backup_out or os.path.dirname(os.path.abspath(db_path))
        try:
            pg_backup = take_pg_backup(dsn, pg_out_dir)
        except BackupError as exc:
            print(f"✗ بکاپ PostgreSQL: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if not args.quiet:
            print(f"✔ بکاپ PG: {pg_backup['path']} | {pg_backup['bytes']} بایت | "
                  f"{pg_backup['toc_entries']} ورودی TOC | sha256={pg_backup['sha256'][:16]}…")
        if args.backup_only:
            if args.manifest:
                _write_manifest(args.manifest, db_path, {"pg_backup": pg_backup})
            return EXIT_OK

    if args.backup_only:
        if not os.path.exists(db_path):
            print(f"✗ فایل دیتابیس پیدا نشد: {db_path}", file=sys.stderr)
            return EXIT_ERROR
        backup = make_backup(db_path)
        if os.path.abspath(backup) == os.path.abspath(db_path):
            print("✗ بکاپ هم‌نام خودِ دیتابیس است؛ متوقف شد.", file=sys.stderr)
            return EXIT_ERROR
        print(f"✔ بکاپ SQLite: {backup} | sha256={sha256_of(backup)[:16]}… "
              f"(مقابل فایل: {'مطابق' if sha256_of(backup) == sha256_of(db_path) else 'DIFF!'}، "
              f"بدون هیچ نوشتنی روی DB)")
        if args.manifest:
            _write_manifest(args.manifest, db_path, {"sqlite_backup": {
                "kind": "file_copy", "path": backup, "sha256": sha256_of(backup)}})
        return EXIT_OK

    if not os.path.exists(db_path):
        print(f"✗ فایل دیتابیس پیدا نشد: {db_path}", file=sys.stderr)
        return EXIT_ERROR

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or list(TIME_COLUMNS)

    # Cutover sign
    conn = connect_ro(db_path)
    try:
        marker, marker_src = read_cutover(conn)
    finally:
        conn.close()
    if args.cutover.strip():
        try:
            marker = datetime.fromisoformat(args.cutover.strip().replace("Z", "+00:00"))
            if marker.tzinfo is None:
                raise ValueError("naive")
            marker = marker.astimezone(timezone.utc)
            marker_src = f"--cutover = {args.cutover.strip()}"
        except ValueError:
            print(f"✗ مقدار --cutover قابل تجزیه نیست یا naive است: {args.cutover!r}",
                  file=sys.stderr)
            return EXIT_NEEDS_ACTION
    if marker is None:
        print("✗ نشانهٔ cutover در دست نیست ⇒ تفسیر ردیف‌های Z ممکن نیست.\n"
              f"  دلیل: {marker_src}\n"
              "  اقدام: python tools/set_cutover_marker.py --db <db>  یا  --cutover <ISO آگاه>",
              file=sys.stderr)
        return EXIT_NEEDS_ACTION

    if not args.quiet:
        print("──── migrate_timestamps_to_utc ────")
        print(f"DB      : {db_path}")
        print(f"cutover : {marker.isoformat()}  ({marker_src})")
        print(f"حالت    : {'dry-run (بدون نوشتن)' if args.dry_run else 'اجرای واقعی'}")

    # Scanning
    report = scan(db_path, marker, tables)
    if not args.quiet:
        print("\n── دسته‌بندی مقادیر ─────────────────────────────")
        for table, entry in report["tables"].items():
            if not entry.get("present"):
                print(f"   {table:24} (در DB نیست — رد شد)")
                continue
            cats = {}
            for col_cats in entry["columns"].values():
                for k, v in col_cats.items():
                    cats[k] = cats.get(k, 0) + v
            summary = ", ".join(f"{k}={v}" for k, v in sorted(cats.items())) or "—"
            print(f"   {table:24} rows={entry['rows']:<5} {summary}")
        tot = report["totals"]
        print(f"\n   مجموع: rows={tot['rows']} converted={tot['converted']} "
              f"already_standard={tot['already_standard']} skipped={tot['skipped']} "
              f"needs_decision={tot['needs_decision']}")
        for sample in report["skipped_samples"]:
            print(f"   ⚠ دست‌نخورده ({sample['category']}): "
                  f"{sample['table']}.{sample['column']} = {sample['value']!r}")

    if report["totals"]["skipped"] and not args.dry_run:
        print(f"\n⚠ {report['totals']['skipped']} ردیف دست‌نخورده ماند (زباله/مبهم)؛ "
              "همه در manifest فهرست شده‌اند تا بازبینی دستی شوند.", file=sys.stderr)

    # ── dry-run ───────────────────────────────────────────────
    if args.dry_run:
        manifest = dict(report)
        manifest["mode"] = "dry-run"
        manifest["cutover"] = marker.isoformat()
        manifest["db"] = os.path.abspath(db_path)
        manifest["sha256_before"] = sha256_of(db_path)
        manifest_path = _write_manifest(args.manifest, db_path, manifest, verbose=not args.quiet)
        if not args.quiet:
            print("\n✔ dry-run: هیچ تغییری اعمال نشد.")
        return _exit_code(report)

    if report["totals"]["converted"] == 0:
        manifest = dict(report)
        manifest.update({"mode": "no-op", "cutover": marker.isoformat(),
                         "db": os.path.abspath(db_path),
                         "sha256_before": sha256_of(db_path)})
        manifest_path = _write_manifest(args.manifest, db_path, manifest, verbose=not args.quiet)
        if args.quiet:
            print(f"[migrate] هیچ تبدیلی لازم نبود (rows={report['totals']['rows']}، "
                  f"همه استاندارد/دست‌نخورده) manifest={manifest_path or '—'}")
        else:
            print("\n✔ هیچ تبدیلی لازم نبود (idempotent).")
        return _exit_code(report)

    # Backup + run
    backup = None
    if not args.no_backup:
        backup = make_backup(db_path)
        if not args.quiet:
            print(f"\nبکاپ    : {backup}")

    sha_before = sha256_of(db_path)
    applied = migrate_rows(db_path, marker, tables)

    manifest = dict(report)
    manifest.update({
        "mode": "applied",
        "db": os.path.abspath(db_path),
        "cutover": marker.isoformat(),
        "backup": backup,
        "updates_per_table": applied,
        "pg_backup": pg_backup,
        "sha256_before": sha_before,
        "sha256_after": sha256_of(db_path),
    })
    manifest["totals"]["updated_cells"] = sum(applied.values())
    manifest_path = _write_manifest(args.manifest, db_path, manifest, verbose=False)

    if args.quiet:
        print(f"[migrate] converted={report['totals']['converted']} "
              f"updated_cells={manifest['totals']['updated_cells']} "
              f"skipped={report['totals']['skipped']} backup={backup or '—'} "
              f"manifest={manifest_path or '—'}")
    else:
        print("\n── نتیجه ────────────────────────────────────────")
        for table, n in applied.items():
            if n:
                print(f"   {table:24} {n} سلول به‌روزرسانی شد")
        print(f"\n✔ مهاجرت انجام شد | converted={report['totals']['converted']} "
              f"skipped={report['totals']['skipped']} "
              f"needs_decision={report['totals']['needs_decision']}")
        if manifest_path:
            print(f"   manifest: {manifest_path}")

    return _exit_code(report)


def _exit_code(report: dict) -> int:
    """
    Exit code from the scan result.

    ``z_cutover_unknown'' (row ``Z'' without reliable sign) ⇒ 2: human decision
    it is necessary Garbage/empty rows are **warning** only (output 0) because no interpretation
    There is no meaning for them and it is safe to remain untouched; All in the manifest
    are listed.
    """
    return EXIT_NEEDS_ACTION if report["totals"]["needs_decision"] else EXIT_OK


def _write_manifest(path: str, db_path: str, manifest: dict, verbose: bool = False) -> str | None:
    """Write JSON manifest (default: next to database itself with timestamp — TD-005)."""
    if path and path.strip():
        target = path.strip()
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = f"{db_path}.e3-manifest-{timestamp}.json"
        if os.path.exists(target):
            counter = 1
            while os.path.exists(f"{db_path}.e3-manifest-{timestamp}_{counter}.json"):
                counter += 1
            target = f"{db_path}.e3-manifest-{timestamp}_{counter}.json"
    try:
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        if verbose:
            print(f"   manifest: {target}")
        return target
    except OSError as exc:
        print(f"⚠ نوشتن manifest ممکن نشد ({exc})", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
