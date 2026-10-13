#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/generate_cutover_fixture.py — اسکریپت تولید دیتابیس مصنوعی SQLite برای تمرین Cutover (فاز F.4).

ویژگی‌ها و قابلیت‌ها:
-------------------
۱. ایجاد فایل دیتابیس SQLite با ساختار و اسکیمای کامل برگرفته از logs.db
۲. تولید حجم داده‌ی هدف (پیش‌فرض ۳۰ مگابایت، در بازه مجاز ۲۵ الی ۳۵ مگابایت)
۳. توزیع حجم سطور بر مبنای درصدهای آماری پروداکشن:
   - logs: ۵۰٪ (~۱۵ مگابایت)
   - sensor_readings: ۱۷٪ (~۵ مگابایت)
   - toner_snapshots_v2: ۱۳٪ (~۴ مگابایت)
   - toner_history: ۱۰٪ (~۳ مگابایت)
   - yield_samples: ۷٪ (~۲ مگابایت)
   - سایر جداول تنظیمی و پیکربندی: ۳٪
۴. تولید داده‌های واقع‌گرایانه (Realistic Synthetic Data):
   - رشته‌های زمان ایزوی Naive بدون پسوند Z (مشابه دیتابیس واقعی پروداکشن)
   - ۳۰ نشانی IP واقعی پرینترها
   - سطوح تونر ۰ تا ۱۰۰، وضعیت‌ها، پیام‌ها، و details به صورت رشته معتبر JSON.
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

TABLE_WEIGHTS = {
    "logs": 0.50,               # ~15MB
    "sensor_readings": 0.17,    # ~5MB
    "toner_snapshots_v2": 0.13, # ~4MB
    "toner_history": 0.10,      # ~3MB
    "yield_samples": 0.07,      # ~2MB
    "printer_counters": 0.01,
    "cartridge_state": 0.01,
    "cartridge_yield_profiles": 0.005,
    "users": 0.005,
    "security_events": 0.005,
}

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
    تولید دیتابیس مصنوعی SQLite با حجم تقریبی size_mb بر مبنای اسکیمای schema_source.

    Returns:
        حجم فایل تولید شده به بایت.
    """
    out_file = pathlib.Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if out_file.exists():
        out_file.unlink()

    # کپی اسکیمای اولیه از دیتابیس مرجع
    shutil.copyfile(schema_source, out_file)

    conn = sqlite3.connect(str(out_file))
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")

    # استخراج جداول برنامه و تخلیه آن‌ها
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' AND name != 'alembic_version'"
        ).fetchall()
    ]
    for tbl in tables:
        conn.execute(f'DELETE FROM "{tbl}"')

    # هم‌راستایی اسکیمای جدول logs با پروداکشن واقعی (۱۸ ستون)
    cur_cols = {r[1] for r in conn.execute('PRAGMA table_info("logs")').fetchall()}
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

    # محاسبه تعداد سطور بر مبنای ضریب حجم
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

    # ۱. جدول users
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

    # ۲. جدول printer_counters
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

    # ۳. جدول cartridge_state
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

    # ۴. جدول cartridge_yield_profiles
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

    # ۵. جدول meta
    conn.execute(
        "INSERT INTO meta VALUES (?,?,?)",
        ("timezone_cutover_at", "2026-10-03T06:35:11+00:00", "2026-10-03T06:35:11+00:00"),
    )
    conn.execute(
        "INSERT INTO meta VALUES (?,?,?)",
        ("synthetic_seed", "42", "2026-10-07T12:00:00.000000"),
    )

    # ۶. جدول security_events
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

    # ۷. جدول yield_samples
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

    # ۸. جدول toner_history
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

    # ۹. جدول toner_snapshots_v2
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

    # ۱۰. جدول sensor_readings
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

    # ۱۱. جدول logs (شامل ۱۸ ستون هم‌راستا با production)
    log_rows = [
        (
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
        for i in range(1, num_logs + 1)
    ]
    conn.executemany(
        "INSERT INTO logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        log_rows,
    )

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
