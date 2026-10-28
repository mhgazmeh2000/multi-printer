#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/generate_cutover_fixture.py
# @brief:        Generates a synthetic SQLite database for cutover practice (phase F).
# @details:      Responsibilities: Create synthetic data according to the command-line options.
#                Main API: generate_synthetic_db(), parse_args(), main()
#                Side effects: database access, file I/O
# @dependencies: stdlib only
# @notes:        Can be run directly as a script.
# =============================================================================

"""
tools/generate_cutover_fixture.py — Script to generate synthetic SQLite database for Cutover practice (phase F.4).

Features and capabilities:
-------------------
1. Creating SQLite database file with complete structure and schema taken from logs.db
2. Generating target data volume (default 30 MB, within the allowed range of 25 to 35 MB)
3. Distribution of the volume of lines based on statistical percentages of production:
   - logs: 50% (~15 MB)
   - sensor_readings: 17% (~5 MB)
   - toner_snapshots_v2: 13% (~4 MB)
   - toner_history: 10% (~3 MB)
   - yield_samples: 7% (~2 MB)
   - Other regulatory and configuration tables: 3%
4. Production of realistic synthetic data:
   - Naive ISO time strings without Z extension (similar to real production database)
   - 30 real IP addresses of printers
   - Toner levels 0 to 100, states, messages, and details as valid JSON strings.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import sys

DEFAULT_TARGET_SIZE_MB = 30.0
DEFAULT_OUTPUT_PATH = "tests/fixtures/cutover_synthetic.db"
DEFAULT_SCHEMA_SOURCE = "logs.db"

SAMPLE_IPS = [".".join(["192", "168", "10", str(i)]) for i in range(1, 31)]
SAMPLE_MODELS = [
    "HP LaserJet Pro M404n",
    "HP Color LaserJet M577",
    "Canon imageRUNNER 2520",
    "Toshiba e-STUDIO 2505AC",
    "Brother HL-L6200DW",
]
SAMPLE_COLORS = ["black", "cyan", "magenta", "yellow"]
SAMPLE_TYPES = ["PRINT", "ALERT", "STATUS", "REFILL"]


def generate_synthetic_db(
    output_path: str = DEFAULT_OUTPUT_PATH,
    size_mb: float = DEFAULT_TARGET_SIZE_MB,
    schema_source: str = DEFAULT_SCHEMA_SOURCE,
    quiet: bool = False,
) -> int:
    """
    Generating an artificial SQLite database with an approximate volume of size_mb based on the schema_source schema.

    Returns:
        Size of generated file in bytes.
    """
    out_file = pathlib.Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if out_file.exists():
        out_file.unlink()

    # Copy the initial schema from the reference database
    shutil.copyfile(schema_source, out_file)

    conn = sqlite3.connect(str(out_file))
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")

    # Extracting program tables and unloading them
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' AND name != 'alembic_version'"
        ).fetchall()
    ]
    for tbl in tables:
        conn.execute(f'DELETE FROM "{tbl}"')

    # The meta table is not in production SQLite (10 tables), but the destination schema has 11 tables.
    # Exactly the same DDL migration 0001.
    if "meta" not in tables:
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    # Compression after dump: DELETE does not shrink the file, and if ``schema_source''
    # If it is a real database (several hundred megabytes), the final file will be in the range of 25-35MB.
    conn.commit()
    conn.execute("VACUUM")

    # Schema alignment of logs table with actual production (18 columns).
    # ``table_xinfo'' (not ``table_info''): virtual *generated* columns like ``raw_delta''
    # They are not seen in ``table_info'' and ``ADD COLUMN'' with ``duplicate column name''
    # Fails — Actual error on production schema.
    log_xinfo = conn.execute('PRAGMA table_xinfo("logs")').fetchall()
    cur_cols = {r[1] for r in log_xinfo}
    # hidden == 0 ⇒ normal and insertable column; 2/3 ⇒ generated and not insertable.
    insertable_cols = [r[1] for r in log_xinfo if not r[6]]
    legacy_columns = [
        ("sheets", "INTEGER"),
        ("raw_counters", "TEXT"),
        ("raw_delta", "INTEGER"),
        ("event_type", "TEXT"),
        ("duplex_mode", "TEXT"),
    ]
    for col_name, col_type in legacy_columns:
        if col_name not in cur_cols:
            conn.execute(f'ALTER TABLE "logs" ADD COLUMN "{col_name}" {col_type}')

    conn.commit()

    # Calculation of the number of lines based on the volume factor
    scale_factor = size_mb / 30.0

    num_users = 15
    num_counters = len(SAMPLE_IPS)
    num_cartridge_states = len(SAMPLE_IPS) * len(SAMPLE_COLORS)
    num_yield_profiles = len(SAMPLE_MODELS) * len(SAMPLE_COLORS)
    num_security_events = max(100, int(500 * scale_factor))

    num_yield_samples = max(1000, int(10000 * scale_factor))
    num_toner_history = max(2000, int(24000 * scale_factor))
    num_snapshots = max(2000, int(20000 * scale_factor))
    num_sensors = max(3000, int(41000 * scale_factor))
    num_logs = max(5000, int(61500 * scale_factor))

    # 1. users table
    u_rows = [
        (
            i,
            f"user_{i}",
            f"user_{i}@printguard.local",
            "pbkdf2:sha256:fakehashvalue",
            None,
            None,
            None,
            "admin" if i == 1 else "operator",
            1,
            1,
            1,
            "2026-08-25T10:00:00.000000",
            "2026-08-25T10:00:00.000000",
            "2026-08-25T11:00:00.000000",
            "[]",
            "[]",
            0,
            25.0,
            35.0,
            70.0,
            85.0,
            20.0,
            30,
            8,
            17,
        )
        for i in range(1, num_users + 1)
    ]
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", u_rows)

    # 2. printer_counters table
    pc_rows = [
        (
            ip,
            "hp",
            10000,
            2000,
            8000,
            85,
            0,
            None,
            0,
            0,
            0,
            2500,
            0,
            0,
            "[]",
            500,
            9500,
            "[]",
            "{}",
            "2026-08-25T11:00:00.000000",
            "{}",
        )
        for ip in SAMPLE_IPS
    ]
    conn.executemany("INSERT INTO printer_counters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pc_rows)

    # 3. cartridge_state table
    cs_rows = [
        (
            ip,
            c,
            "HP Color LaserJet M577",
            f"Toner {c.capitalize()}",
            f"hp_{c}",
            80,
            80,
            10000,
            9000,
            90,
            "2026-08-25T10:00:00.000000",
            2500,
            "learned",
            "high",
            5,
            4.5,
            "2026-08-25T10:00:00.000000",
            None,
            0,
            0,
            0,
            None,
            9000,
            90,
            "2026-08-25T10:00:00.000000",
            None,
            None,
            0,
            "normal",
            0,
            "2026-08-25T11:00:00.000000",
        )
        for ip in SAMPLE_IPS
        for c in SAMPLE_COLORS
    ]
    conn.executemany("INSERT INTO cartridge_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", cs_rows)

    # 4. cartridge_yield_profiles table
    cyp_rows = [
        (
            f"cyp_{idx}_{c}",
            m,
            f"Toner {c.capitalize()}",
            c,
            2500,
            "high",
            10,
            8.5,
            SAMPLE_IPS[0],
            "2026-08-25T11:00:00.000000",
        )
        for idx, m in enumerate(SAMPLE_MODELS)
        for c in SAMPLE_COLORS
    ]
    conn.executemany("INSERT INTO cartridge_yield_profiles VALUES (?,?,?,?,?,?,?,?,?,?)", cyp_rows)

    # 5. Meta table
    conn.execute(
        "INSERT INTO meta VALUES (?,?,?)",
        ("timezone_cutover_at", "2026-10-03T06:35:11+00:00", "2026-10-03T06:35:11+00:00"),
    )
    conn.execute(
        "INSERT INTO meta VALUES (?,?,?)",
        ("synthetic_seed", "42", "2026-10-07T12:00:00.000000"),
    )

    # 6. security_events table
    se_rows = [
        (
            i,
            f"2026-08-25T11:{i % 60:02d}:{(i * 3) % 60:02d}.500000",
            "LOGIN_SUCCESS",
            "INFO",
            f"user_{(i % num_users) + 1}",
            (i % num_users) + 1,
            SAMPLE_IPS[i % len(SAMPLE_IPS)],
            "Mozilla/5.0",
            "/api/login",
            1,
            '{"auth": true}',
            f"2026-08-25T11:{i % 60:02d}:{(i * 3) % 60:02d}.500000",
        )
        for i in range(1, num_security_events + 1)
    ]
    conn.executemany("INSERT INTO security_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", se_rows)

    # 7. yield_samples table
    ys_rows = [
        (
            i,
            SAMPLE_IPS[i % len(SAMPLE_IPS)],
            SAMPLE_COLORS[i % 4],
            SAMPLE_MODELS[i % len(SAMPLE_MODELS)],
            f"Toner {SAMPLE_COLORS[i % 4]}",
            f"key_{SAMPLE_COLORS[i % 4]}",
            5000,
            7500,
            100,
            20,
            2500,
            80,
            3125,
            "engine",
            0.95,
            1,
            None,
            "poller",
            f"2026-08-25T11:{i % 60:02d}:{(i * 7) % 60:02d}.100000",
        )
        for i in range(1, num_yield_samples + 1)
    ]
    conn.executemany("INSERT INTO yield_samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ys_rows)

    # 8. toner_history table
    th_rows = [
        (
            i,
            SAMPLE_IPS[i % len(SAMPLE_IPS)],
            f"2026-08-25T11:{i % 60:02d}:{(i * 11) % 60:02d}.200000",
            10000 + i,
            (i * 7) % 100,
            2500,
            "poller",
        )
        for i in range(1, num_toner_history + 1)
    ]
    conn.executemany("INSERT INTO toner_history VALUES (?,?,?,?,?,?,?)", th_rows)

    # 9. table toner_snapshots_v2
    ts_rows = [
        (
            i,
            SAMPLE_IPS[i % len(SAMPLE_IPS)],
            SAMPLE_COLORS[i % 4],
            SAMPLE_MODELS[i % len(SAMPLE_MODELS)],
            f"Toner {SAMPLE_COLORS[i % 4]}",
            f"key_{SAMPLE_COLORS[i % 4]}",
            f"2026-08-25T11:{i % 60:02d}:{(i * 13) % 60:02d}.300000",
            10000 + i,
            2000 + i,
            8000 + i,
            10000 + i,
            (i * 3) % 100,
            (i * 3) % 100,
            "poller",
            1,
            None,
        )
        for i in range(1, num_snapshots + 1)
    ]
    conn.executemany("INSERT INTO toner_snapshots_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ts_rows)

    # 10. sensor_readings table
    sr_rows = [
        (
            i,
            SAMPLE_IPS[i % len(SAMPLE_IPS)],
            f"2026-08-25T11:{i % 60:02d}:{(i * 17) % 60:02d}.400000",
            1,
            "temperature" if i % 2 == 0 else "humidity",
            24.5 if i % 2 == 0 else 55.0,
            "C" if i % 2 == 0 else "%",
            "OK",
        )
        for i in range(1, num_sensors + 1)
    ]
    conn.executemany("INSERT INTO sensor_readings VALUES (?,?,?,?,?,?,?,?)", sr_rows)

    # 11. logs table (including 18 columns in line with production)
    def _log_row(i: int) -> tuple:
        """Build one synthetic logs row for the cutover fixture."""
        return (
            i,
            SAMPLE_IPS[i % len(SAMPLE_IPS)],
            SAMPLE_MODELS[i % len(SAMPLE_MODELS)],
            f"2026-08-25T11:{i % 60:02d}:{(i * 19) % 60:02d}.600000",
            SAMPLE_TYPES[i % len(SAMPLE_TYPES)],
            f"Printer event #{i} dispatched cleanly",
            (i % 20) + 1,
            SAMPLE_COLORS[i % 4],
            "OK",
            "INFO",
            "A4",
            f"user_{(i % num_users) + 1}",
            json.dumps({"pages": (i % 20) + 1, "color": SAMPLE_COLORS[i % 4], "job_id": i}),
            None,  # sheets (legacy, 100% NULL)
            None,  # raw_counters (legacy, 100% NULL)
            None,  # raw_delta (legacy, 100% NULL)
            "UNKNOWN",  # event_type (legacy, 100% 'UNKNOWN')
            "unknown",  # duplex_mode (legacy, 100% 'unknown')
        )

    # Insert column-oriented, not ``VALUES'' without column index: on the production schema that
    # ``raw_delta'' is *generated* in that column, direct insertion in that column gives error.
    log_column_order = [
        "id", "printer_ip", "printer_name", "timestamp", "type", "message", "pages",
        "color", "code", "severity", "paper_size", "username", "details",
        "sheets", "raw_counters", "raw_delta", "event_type", "duplex_mode",
    ]
    insertable = set(insertable_cols)
    keep = [i for i, name in enumerate(log_column_order) if name in insertable]
    cols_sql = ", ".join(f'"{log_column_order[i]}"' for i in keep)
    placeholders = ",".join("?" for _ in keep)

    def _insert_logs(rows: list) -> None:
        """Insert the synthetic log rows into the fixture database."""
        conn.executemany(
            f"INSERT INTO logs ({cols_sql}) VALUES ({placeholders})",
            [tuple(row[i] for i in keep) for row in rows],
        )

    _insert_logs([_log_row(i) for i in range(1, num_logs + 1)])

    # Set the final size to "Target size": line count is based on estimates and not on the schema
    # Actual production (columns wider than the reference database) outside the allowed range of 25-35MB
    # hits; So the two sides are corrected to maintain the contract of the tool itself.
    target_bytes = int(size_mb * 1024 * 1024)
    band_bytes = max(1, int(size_mb * 0.10 * 1024 * 1024))     # ±10% ⇒ 27–33MB for 30
    for _ in range(8):
        conn.commit()
        conn.execute("VACUUM")
        current = os.path.getsize(str(out_file))
        n_logs = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        bytes_per_row = max(1, current // max(1, n_logs))
        if current > target_bytes + band_bytes:
            cut = max(1, min(n_logs - 1, (current - target_bytes - band_bytes) // bytes_per_row))
            conn.execute(
                "DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (cut,),
            )
            continue
        if current < target_bytes - band_bytes:
            highest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]
            want = max(500, min(20000, (target_bytes - band_bytes - current) // bytes_per_row))
            _insert_logs([_log_row(i) for i in range(highest + 1, highest + 1 + want)])
            continue
        break

    conn.commit()
    conn.close()

    final_size_bytes = os.path.getsize(str(out_file))
    final_size_mb = final_size_bytes / (1024 * 1024)

    if not quiet:
        total_rows = (
            num_users
            + num_counters
            + num_cartridge_states
            + num_yield_profiles
            + 2  # meta
            + num_security_events
            + num_yield_samples
            + num_toner_history
            + num_snapshots
            + num_sensors
            + num_logs
        )
        print("==============================================================================")
        print(" دیتابیس مصنوعی برای تمرین Cutover با موفقیت ساخته شد")
        print("==============================================================================")
        print(f"مسیر فایل خروجی : {out_file}")
        print(f"حجم نهایی فایل  : {final_size_mb:.2f} مگابایت ({final_size_bytes:,} بایت)")
        print(f"مجموع رکوردهای درج شده: {total_rows:,} سطر در ۱۱ جدول")
        print("==============================================================================")

    return final_size_bytes


def parse_args(argv=None):
    """Parse the command-line arguments of the fixture generator."""

    parser = argparse.ArgumentParser(
        prog="generate_cutover_fixture.py",
        description="تولید دیتابیس مصنوعی SQLite برای تمرین Cutover (فاز F.4).",
    )
    parser.add_argument(
        "--size-mb",
        type=float,
        default=DEFAULT_TARGET_SIZE_MB,
        help=f"حجم هدف دیتابیس به مگابایت (پیش‌فرض: {DEFAULT_TARGET_SIZE_MB})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        help=f"مسیر ذخیره دیتابیس خروجی (پیش‌فرض: {DEFAULT_OUTPUT_PATH})",
    )
    parser.add_argument(
        "--schema-source",
        default=DEFAULT_SCHEMA_SOURCE,
        help=f"دیتابیس مرجع برای کپی اسکیمای ساختار (پیش‌فرض: {DEFAULT_SCHEMA_SOURCE})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="عدم چاپ گزارش خلاصه در خروجی",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="تولید مجدد fixture حتی در صورت وجود فایل قبلی",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """Generate the synthetic cutover database."""

    args = parse_args(argv)
    if not (20.0 <= args.size_mb <= 35.0):
        print(f"✗ حجم دیتابیس درخواستی ({args.size_mb} MB) باید در بازه ۲۰ تا ۳۵ مگابایت باشد.", file=sys.stderr)
        return 1
    generate_synthetic_db(
        output_path=args.output,
        size_mb=args.size_mb,
        schema_source=args.schema_source,
        quiet=args.quiet,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
