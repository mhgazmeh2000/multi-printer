#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/migrate_sqlite_to_pg.py — ابزار مستقل خط فرمان برای مهاجرت داده از SQLite به PostgreSQL (فاز F).

چارچوب معماری و ایمنی:
--------------------
۱. منبع SQLite به صورت کاملاً فقط-خواندنی با URI باز می‌شود (mode=ro&immutable=1) تا هیچ قفل یا تغییری رخ ندهد.
۲. استراتژی هیبرید: جداول کلان‌داده (> 10000 سطر) با COPY FROM STDIN و جداول تنظیمی با Batch Insert (1000 سطر).
۳. همگام‌سازی توالی‌ها (Identity Sequence Sync): اجرای اجباری setval برای هر ۷ جدول دارای IDENTITY (رفع ریسک R-1).
۴. بازگشت خودکار (Rollback): در صورت بروز خطای بحرانی، اجرای سریع TRUNCATE ... RESTART IDENTITY CASCADE.
۵. اعتبارسنجی نوع‌ها: تبدیل بولی‌ها (bool)، اعتبارسنجی زمان UTC-Z، و اعتبارسنجی فیلدهای JSON.
۶. جدول alembic_version در SQLite هرگز به PostgreSQL منتقل نمی‌شود.

کدهای خروج (Exit Codes):
------------------------
0 = success (مهاجرت با موفقیت کامل انجام شد)
1 = partial failure (برخی سطرها به دلیل فرمت نامعتبر رد شدند — skip)
2 = fatal (خطای بحرانی رخ داد و Rollback کامل انجام شد)
3 = --dry-run only (فقط شبیه‌سازی و شمارش بدون هیچ تغییری)
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]

log = logging.getLogger("migrate_sqlite_to_pg")

# ── کدهای خروج ──────────────────────────────────────────────────
EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2
EXIT_DRY_RUN = 3

# ── آستانه سوئیچ به استراتژی COPY ──────────────────────────────
COPY_THRESHOLD = 10000
BATCH_SIZE = 1000

# ── جداول دارای ستون IDENTITY (برگرفته از migration 0003) ───────
IDENTITY_TABLES: Tuple[str, ...] = (
    "logs",
    "toner_history",
    "sensor_readings",
    "users",
    "security_events",
    "yield_samples",
    "toner_snapshots_v2",
)

# ── ستون‌های بولی (برگرفته از migration 0002) ───────────────────
BOOLEAN_COLUMNS: Set[Tuple[str, str]] = {
    ("users", "is_verified"),
    ("users", "email_verified"),
    ("users", "is_active"),
    ("yield_samples", "accepted"),
    ("security_events", "success"),
}

# ── ستون‌های JSONB در PostgreSQL (برگرفته از migration 0004) ────
JSONB_COLUMNS: Set[Tuple[str, str]] = {
    ("logs", "details"),
}

# ── ترتیب مهاجرت جداول با رعایت وابستگی‌ها (FK-safe) ─────────────
TABLE_MIGRATION_ORDER: Tuple[str, ...] = (
    "users",
    "meta",
    "printer_counters",
    "cartridge_state",
    "cartridge_yield_profiles",
    "logs",
    "toner_history",
    "yield_samples",
    "toner_snapshots_v2",
    "sensor_readings",
    "security_events",
)

# ── جداول معاف از مهاجرت ───────────────────────────────────────
EXCLUDED_TABLES: Set[str] = {
    "alembic_version",
    "sqlite_sequence",
    "sqlite_stat1",
}

# ── شمارنده‌ی سراسری timestampهای بدون Z ──────────────────────
_naive_ts_counter: int = 0

# ── الگوهای اعتبارسنجی زمان در جدول logs ───────────────────────
LOG_TS_WITH_Z_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
LOG_TS_NAIVE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")


@dataclasses.dataclass
class TablePlan:
    name: str
    sqlite_rows: int
    pg_rows: int
    columns: List[str]
    strategy: str  # "COPY" or "BATCH"
    has_identity: bool = False


@dataclasses.dataclass
class MigrationStats:
    table: str
    read_count: int = 0
    inserted_count: int = 0
    skipped_count: int = 0
    strategy: str = ""
    duration_seconds: float = 0.0


def resolve_pg_dsn(cli_target: Optional[str]) -> str:
    """استخراج DSN اتصال PostgreSQL از آرگومان، متغیر محیطی یا فایل پیکربندی."""
    if cli_target and cli_target.strip():
        return cli_target.strip()
    env_dsn = os.environ.get("PG_DSN", "").strip()
    if env_dsn:
        return env_dsn
    alembic_url = os.environ.get("ALEMBIC_DB_URL", "").strip()
    if alembic_url:
        return alembic_url
    return "postgresql://printguard:pg-sandbox-only@127.0.0.1:5432/printguard"


def connect_sqlite_ro(source_path: str) -> sqlite3.Connection:
    """اتصال قطعی فقط-خواندنی به پایگاه‌داده SQLite."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"فایل دیتابیس SQLite منبع پیدا نشد: {source_path}")
    abs_path = os.path.abspath(source_path)
    uri = f"file:{abs_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def connect_pg(dsn: str) -> psycopg.Connection:
    """برقراری اتصال به PostgreSQL با کتابخانه psycopg 3."""
    if psycopg is None:
        raise RuntimeError("کتابخانه psycopg نصب نیست. لطفاً وابستگی‌ها را نصب کنید.")
    return psycopg.connect(dsn, autocommit=False)


def get_sqlite_tables(conn: sqlite3.Connection) -> List[str]:
    """فهرست جدول‌های موجود در SQLite بدون جداول سیستمی."""
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [r[0] for r in cur.fetchall()]


def get_sqlite_table_ddl(conn: sqlite3.Connection, table: str) -> str:
    """دریافت عبارت CREATE TABLE از sqlite_master جهت بررسی تعاریف خاص (مانند GENERATED)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    )
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else ""


def detect_sqlite_generated_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """شناسایی ستون‌های محاسباتی GENERATED در جدول بر اساس DDL و table_xinfo."""
    gen_cols: List[str] = []
    cur = conn.cursor()
    try:
        cur.execute(f"PRAGMA table_xinfo({table})")
        rows = cur.fetchall()
        for r in rows:
            if len(r) > 6 and r[6] in (2, 3):
                gen_cols.append(r[1])
    except Exception:
        pass

    # بررسی تکمیلی DDL در sqlite_master.sql برای کلمه GENERATED
    ddl = get_sqlite_table_ddl(conn, table)
    if "GENERATED" in ddl.upper() and not gen_cols:
        import re

        matches = re.findall(
            r'["`]?([a-zA-Z0-9_]+)["`]?\s+[^,]*\bGENERATED\b', ddl, re.IGNORECASE
        )
        for m in matches:
            if m and m not in gen_cols:
                gen_cols.append(m)
    return gen_cols


def get_pg_tables(conn: psycopg.Connection) -> List[str]:
    """فهرست جدول‌های موجود در اسکیمای فعلی PostgreSQL."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name 
            FROM information_schema.columns 
            WHERE table_schema = current_schema()
            GROUP BY table_name
            """
        )
        return [r[0] for r in cur.fetchall()]


def get_table_columns_sqlite(conn: sqlite3.Connection, table: str) -> List[str]:
    """فهرست نام ستون‌های جدول در SQLite (شامل ستون‌های عادی و محاسباتی GENERATED/VIRTUAL)."""
    cur = conn.cursor()
    cols: List[str] = []
    try:
        cur.execute(f"PRAGMA table_xinfo({table})")
        rows = cur.fetchall()
        cols = [r[1] for r in rows if len(r) <= 6 or r[6] in (0, 2, 3)]
    except Exception:
        cols = []

    if not cols:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]

    gen_cols = detect_sqlite_generated_columns(conn, table)
    for gc in gen_cols:
        if gc not in cols:
            cols.append(gc)

    return cols


def get_table_columns_pg(conn: psycopg.Connection, table: str) -> List[str]:
    """فهرست نام ستون‌های جدول در PostgreSQL بر اساس ترتیب تعریف."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_schema = current_schema() AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def get_table_row_count_sqlite(conn: sqlite3.Connection, table: str) -> int:
    """شمارش تعداد سطرهای یک جدول در SQLite."""
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def get_table_row_count_pg(conn: psycopg.Connection, table: str) -> int:
    """شمارش تعداد سطرهای یک جدول در PostgreSQL."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def build_migration_plan(
    sqlite_conn: sqlite3.Connection, pg_conn: psycopg.Connection
) -> List[TablePlan]:
    """بررسی ساختار، هماهنگی ستون‌ها و تعیین استراتژی به ازای هر جدول."""
    sqlite_tables = set(get_sqlite_tables(sqlite_conn))
    pg_tables = set(get_pg_tables(pg_conn))

    plan: List[TablePlan] = []
    for table in TABLE_MIGRATION_ORDER:
        if table in EXCLUDED_TABLES:
            continue
        if table not in sqlite_tables:
            log.warning("جدول %s در دیتابیس منبع SQLite وجود ندارد؛ نادیده گرفته شد.", table)
            continue
        if table not in pg_tables:
            raise RuntimeError(
                f"جدول {table} در اسکیمای مقصد PostgreSQL وجود ندارد! "
                "ابتدا مایگریشن‌های Alembic را اعمال کنید."
            )

        # اسکن sqlite_master.sql برای بررسی ستون‌های محاسباتی GENERATED
        ddl = get_sqlite_table_ddl(sqlite_conn, table)
        gen_cols = detect_sqlite_generated_columns(sqlite_conn, table)
        if "GENERATED" in ddl.upper() or gen_cols:
            log.info(
                "جدول %s شامل ستون‌های GENERATED است (%s)؛ "
                "این ستون‌ها به عنوان ستون معمولی به PostgreSQL مهاجرت می‌شوند.",
                table,
                ", ".join(gen_cols) if gen_cols else "مشاهده در DDL",
            )

        s_cols = get_table_columns_sqlite(sqlite_conn, table)
        p_cols = get_table_columns_pg(pg_conn, table)

        # اعتبارسنجی تطابق ستون‌ها
        if set(s_cols) != set(p_cols):
            diff = set(s_cols) ^ set(p_cols)
            raise RuntimeError(
                "ناهمخوانی ستون‌ها در جدول " + table + ": تفاوت = " + str(diff)
            )

        s_count = get_table_row_count_sqlite(sqlite_conn, table)
        p_count = get_table_row_count_pg(pg_conn, table)

        strategy = "COPY" if s_count > COPY_THRESHOLD else "BATCH"
        has_identity = table in IDENTITY_TABLES

        plan.append(
            TablePlan(
                name=table,
                sqlite_rows=s_count,
                pg_rows=p_count,
                columns=p_cols,
                strategy=strategy,
                has_identity=has_identity,
            )
        )
    return plan


def _preflight_data_quality(
    sqlite_conn: sqlite3.Connection,
    plan: List[TablePlan],
    max_allowed_ratio: float = 0.05,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    اعتبارسنجی کیفیت داده‌های SQLite پیش از آغاز مهاجرت به PostgreSQL.

    جداول منبع را برای شناسایی ردیف‌های با نوع داده‌ی ناهمخوان (مثلاً ستون‌های INTEGER
    که مقدار غیرعددی یا متنی دارند — ناشی از خرابی دیتابیس یا recovery ناقص) اسکن می‌کند.
    اگر نرخ خطا در یک جدول بیش از ۵٪ (یا max_allowed_ratio) باشد، RuntimeError صادر کرده
    و مانع از اجرای مهاجرت ناقص می‌شود؛ در غیر این صورت صرفاً گزارش و لاگ ثبت می‌کند.
    """
    findings: Dict[str, List[Dict[str, Any]]] = {}
    cur = sqlite_conn.cursor()

    for item in plan:
        table = item.name
        total_rows = item.sqlite_rows
        if total_rows == 0:
            continue

        try:
            cur.execute(f'PRAGMA table_xinfo("{table}")')
            xinfo = cur.fetchall()
        except Exception:
            cur.execute(f'PRAGMA table_info("{table}")')
            xinfo = cur.fetchall()

        int_cols: List[str] = []
        pk_col = xinfo[0][1] if xinfo else "rowid"
        for col_meta in xinfo:
            c_name = col_meta[1]
            c_type = (col_meta[2] or "").upper()
            is_pk = bool(col_meta[5]) if len(col_meta) > 5 else False
            if is_pk:
                pk_col = c_name
            if any(t in c_type for t in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT")):
                int_cols.append(c_name)

        table_issues: List[Dict[str, Any]] = []

        for col in int_cols:
            query = f'''
                SELECT "{pk_col}", "{col}", typeof("{col}")
                FROM "{table}"
                WHERE "{col}" IS NOT NULL AND typeof("{col}") IN ('text', 'blob')
            '''
            try:
                cur.execute(query)
                raw_invalid = cur.fetchall()
            except Exception as exc:
                log.warning("[%s] خطا در بررسی کیفیت داده‌های ستون %s: %s", table, col, exc)
                continue

            invalid_rows = []
            for r in raw_invalid:
                val = r[1]
                try:
                    int(val)
                except (ValueError, TypeError):
                    invalid_rows.append(r)

            if invalid_rows:
                ratio = len(invalid_rows) / total_rows
                issue = {
                    "column": col,
                    "expected_type": "integer",
                    "actual_type": "text",
                    "corrupt_count": len(invalid_rows),
                    "total_rows": total_rows,
                    "ratio": ratio,
                    "sample_rows": [
                        {"pk": str(r[0]), "value": str(r[1])} for r in invalid_rows[:5]
                    ],
                }
                table_issues.append(issue)
                log.warning(
                    "[%s] ستون %s دارای %d مقدار غیرعددی است (نرخ خطا: %.2f%%). نمونه‌ها: %s",
                    table,
                    col,
                    len(invalid_rows),
                    ratio * 100,
                    [f"{r[0]}: {r[1]!r}" for r in invalid_rows[:3]],
                )

                if ratio > max_allowed_ratio:
                    raise RuntimeError(
                        f"توقف مهاجرت به دلیل خرابی داده: در جدول {table} ستون {col}، "
                        f"{len(invalid_rows)} سطر از مجموع {total_rows} سطر ({ratio:.1%}) "
                        f"دارای نوع داده نامعتبر هستند (بیش از ۵٪ مجاز، حد مجاز: {max_allowed_ratio:.1%})."
                    )

        if table_issues:
            findings[table] = table_issues

    return findings


def truncate_all_pg_tables(pg_conn: psycopg.Connection) -> None:
    """تخلیه کامل تمام جداول مقصد و ریست کردن توالی‌های IDENTITY به صورت یکپارچه."""
    tables_to_truncate = [
        "logs",
        "toner_history",
        "sensor_readings",
        "users",
        "security_events",
        "cartridge_state",
        "yield_samples",
        "toner_snapshots_v2",
        "cartridge_yield_profiles",
        "printer_counters",
        "meta",
    ]
    query = (
        f"TRUNCATE TABLE {', '.join(tables_to_truncate)} RESTART IDENTITY CASCADE;"
    )
    with pg_conn.cursor() as cur:
        cur.execute(query)
    pg_conn.commit()
    log.warning("تخلیه کامل تمام جدول‌های PostgreSQL و بازنشانی توالی‌ها انجام شد.")


def sync_identity_sequences(pg_conn: psycopg.Connection) -> Dict[str, int]:
    """
    همگام‌سازی توالی‌های IDENTITY (setval) برای رفع ریسک بحرانی R-1.

    پس از درج شناسه‌های تاریخی با مقادیر صریح، مقدار توالی داخلی با حداکثر شناسه
    تراز می‌شود تا اولین INSERT بعدی منجر به خطای UniqueViolation نشود.
    """
    synced: Dict[str, int] = {}
    with pg_conn.cursor() as cur:
        for table in IDENTITY_TABLES:
            cur.execute(
                f"""
                SELECT CASE 
                    WHEN MAX(id) IS NOT NULL THEN setval(pg_get_serial_sequence('{table}', 'id'), MAX(id), true)
                    ELSE setval(pg_get_serial_sequence('{table}', 'id'), 1, false)
                END
                FROM {table};
                """
            )
            val = cur.fetchone()[0]
            synced[table] = int(val)
    pg_conn.commit()
    return synced


def transform_row(
    table: str,
    row: Sequence[Any],
    columns: Sequence[str],
    bool_indices: Set[int],
    ts_idx: int,
    details_idx: int,
    convert_naive: bool = False,
) -> Optional[Tuple[Any, ...]]:
    """تبدیل انواع داده از SQLite به PostgreSQL همراه با اعتبارسنجی الزامی."""
    vals = list(row)

    # ۱. اعتبارسنجی رشته زمان در جدول logs
    if ts_idx >= 0:
        raw_ts = vals[ts_idx]

        # None یا غیر-string → skip
        if raw_ts is None or not isinstance(raw_ts, str):
            log.warning("[%s] timestamp خالی/غیر-string رد شد: %r", table, raw_ts)
            return None

        # حالت ۱: با Z → قبول
        if LOG_TS_WITH_Z_REGEX.match(raw_ts):
            pass  # OK

        # حالت ۲: ISO معتبر بدون Z
        elif LOG_TS_NAIVE_REGEX.match(raw_ts):
            global _naive_ts_counter
            _naive_ts_counter += 1

            if not convert_naive:
                # پیش‌فرض: skip
                if _naive_ts_counter <= 5:
                    log.warning("[%s] timestamp بدون Z رد شد: %r", table, raw_ts)
                elif _naive_ts_counter == 6:
                    log.error(
                        "بیش از ۵ timestamp بدون Z! راه‌حل: "
                        "اول E.3 را اجرا کن (tools/migrate_timestamps_to_utc.py --apply) "
                        "یا از --convert-naive-timestamps استفاده کن (با احتیاط)."
                    )
                return None
            else:
                # حالت ۲ با flag: تبدیل به Z
                if _naive_ts_counter <= 5:
                    log.warning(
                        "[%s] timestamp بدون Z تبدیل شد (با flag): %r → %rZ",
                        table, raw_ts, raw_ts
                    )
                vals[ts_idx] = raw_ts + "Z"

        # حالت ۳: فرمت کاملاً نامعتبر → skip
        else:
            log.warning("[%s] timestamp نامعتبر رد شد: %r", table, raw_ts)
            return None

    # ۲. تبدیل مقادیر پرچمی به boolean واقعی
    for bi in bool_indices:
        v = vals[bi]
        vals[bi] = bool(v) if v is not None else None

    # ۳. اعتبارسنجی فیلد JSON در logs.details
    if details_idx >= 0:
        raw_det = vals[details_idx]
        if raw_det is not None:
            if isinstance(raw_det, str) and raw_det.strip():
                try:
                    obj = json.loads(raw_det)
                    vals[details_idx] = json.dumps(obj, ensure_ascii=False)
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning(
                        "[%s] فیلد details نامعتبر به '{}' تبدیل شد (%s): %r",
                        table,
                        exc,
                        raw_det,
                    )
                    vals[details_idx] = "{}"
            elif isinstance(raw_det, (dict, list)):
                vals[details_idx] = json.dumps(raw_det, ensure_ascii=False)
            else:
                vals[details_idx] = "{}"
        else:
            vals[details_idx] = None

    return tuple(vals)


def migrate_table_batch(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    plan: TablePlan,
    convert_naive: bool = False,
) -> MigrationStats:
    """مهاجرت داده‌های جدول با استراتژی Batch Insert (دسته‌های ۱۰۰۰ تایی)."""
    t0 = time.time()
    table = plan.name
    cols = plan.columns
    cols_quoted = [f'"{c}"' for c in cols]
    placeholders = ["%s"] * len(cols)
    insert_sql = (
        f'INSERT INTO "{table}" ({", ".join(cols_quoted)}) VALUES ({", ".join(placeholders)})'
    )

    bool_indices = {i for i, c in enumerate(cols) if (table, c) in BOOLEAN_COLUMNS}
    ts_idx = cols.index("timestamp") if "timestamp" in cols and table == "logs" else -1
    details_idx = cols.index("details") if "details" in cols and table == "logs" else -1

    s_cur = sqlite_conn.cursor()
    s_cur.execute(f'SELECT {", ".join(cols_quoted)} FROM "{table}"')

    stats = MigrationStats(table=table, strategy="BATCH")
    batch: List[Tuple[Any, ...]] = []

    with pg_conn.cursor() as p_cur:
        for row in s_cur:
            stats.read_count += 1
            t_row = transform_row(table, row, cols, bool_indices, ts_idx, details_idx, convert_naive=convert_naive)
            if t_row is None:
                stats.skipped_count += 1
                continue
            batch.append(t_row)
            if len(batch) >= BATCH_SIZE:
                p_cur.executemany(insert_sql, batch)
                stats.inserted_count += len(batch)
                batch = []
        if batch:
            p_cur.executemany(insert_sql, batch)
            stats.inserted_count += len(batch)

    pg_conn.commit()
    stats.duration_seconds = time.time() - t0
    return stats


def migrate_table_copy(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    plan: TablePlan,
    convert_naive: bool = False,
) -> MigrationStats:
    """مهاجرت داده‌های کلان‌جدول با استراتژی پرسرعت COPY FROM STDIN."""
    t0 = time.time()
    table = plan.name
    cols = plan.columns
    cols_quoted = [f'"{c}"' for c in cols]
    copy_sql = f'COPY "{table}" ({", ".join(cols_quoted)}) FROM STDIN'

    bool_indices = {i for i, c in enumerate(cols) if (table, c) in BOOLEAN_COLUMNS}
    ts_idx = cols.index("timestamp") if "timestamp" in cols and table == "logs" else -1
    details_idx = cols.index("details") if "details" in cols and table == "logs" else -1

    s_cur = sqlite_conn.cursor()
    s_cur.execute(f'SELECT {", ".join(cols_quoted)} FROM "{table}"')

    stats = MigrationStats(table=table, strategy="COPY")

    with pg_conn.cursor() as p_cur:
        with p_cur.copy(copy_sql) as copy:
            for row in s_cur:
                stats.read_count += 1
                t_row = transform_row(table, row, cols, bool_indices, ts_idx, details_idx, convert_naive=convert_naive)
                if t_row is None:
                    stats.skipped_count += 1
                    continue
                copy.write_row(t_row)
                stats.inserted_count += 1

    pg_conn.commit()
    stats.duration_seconds = time.time() - t0
    return stats


def execute_migration(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    plan: List[TablePlan],
    force: bool = False,
    quiet: bool = False,
    convert_naive: bool = False,
) -> Tuple[List[MigrationStats], bool]:
    """اجرای ترتیبی و امن کل فرآیند مهاجرت با مدیریت خطا و Rollback خودکار."""
    global _naive_ts_counter
    _naive_ts_counter = 0

    # بررسی خالی بودن مقصد در صورت عدم وجود --force
    if not force:
        non_empty = [p.name for p in plan if p.pg_rows > 0]
        if non_empty:
            raise RuntimeError(
                f"پایگاه‌داده مقصد خالی نیست (جداول: {', '.join(non_empty)}). "
                "برای تخلیه اولیه از پرچم --force استفاده کنید."
            )
    else:
        truncate_all_pg_tables(pg_conn)

    all_stats: List[MigrationStats] = []
    has_skips = False

    try:
        for p in plan:
            if not quiet:
                log.info(
                    "آغاز مهاجرت جدول %-24s [تعداد: %-6d | روش: %s]",
                    p.name,
                    p.sqlite_rows,
                    p.strategy,
                )

            if p.strategy == "COPY":
                stats = migrate_table_copy(sqlite_conn, pg_conn, p, convert_naive=convert_naive)
            else:
                stats = migrate_table_batch(sqlite_conn, pg_conn, p, convert_naive=convert_naive)

            all_stats.append(stats)
            if stats.skipped_count > 0:
                has_skips = True

            if not quiet:
                log.info(
                    "پایان مهاجرت جدول %-24s [درج: %-6d | رد: %-3d | زمان: %.2fs]",
                    p.name,
                    stats.inserted_count,
                    stats.skipped_count,
                    stats.duration_seconds,
                )

        # همگام‌سازی توالی‌ها
        synced = sync_identity_sequences(pg_conn)
        if not quiet:
            log.info("همگام‌سازی توالی‌های IDENTITY با موفقیت انجام شد: %s", synced)

    except Exception as exc:
        log.error("خطای بحرانی در جریان مهاجرت: %s — اجرای خودکار Rollback (TRUNCATE)", exc)
        try:
            truncate_all_pg_tables(pg_conn)
        except Exception as rb_exc:
            log.critical("خطا در اجرای دستور Rollback TRUNCATE: %s", rb_exc)
        raise

    return all_stats, has_skips


def verify_migration(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    plan: List[TablePlan],
    quiet: bool = False,
) -> bool:
    """اعتبارسنجی دقیق و مقایسه Checksum میان SQLite و PostgreSQL پس از مهاجرت."""
    print("\n" + "=" * 78)
    print(" اعتبارسنجی و تطابق یکپارچگی داده‌ها (--verify)")
    print("=" * 78)
    print(f"{'جدول':<24} | {'SQLite':<8} | {'PG':<8} | {'MAX(id)':<14} | {'نتیجه'}")
    print("-" * 78)

    all_ok = True
    for p in plan:
        table = p.name
        s_count = get_table_row_count_sqlite(sqlite_conn, table)
        p_count = get_table_row_count_pg(pg_conn, table)

        # مقایسه تعداد سطرها
        count_match = s_count == p_count
        if not count_match:
            all_ok = False

        # مقایسه max(id) برای جداول دارای شناسه
        id_label = "—"
        if p.has_identity:
            s_cur = sqlite_conn.cursor()
            s_cur.execute(f"SELECT MAX(id) FROM {table}")
            s_max_id = s_cur.fetchone()[0]

            with pg_conn.cursor() as p_cur:
                p_cur.execute(f"SELECT MAX(id) FROM {table}")
                p_max_id = p_cur.fetchone()[0]

            id_match = s_max_id == p_max_id
            if not id_match:
                all_ok = False
            id_label = f"{s_max_id}/{p_max_id}" if id_match else f"DIFF({s_max_id}!={p_max_id})"

        if count_match:
            verdict = "✔" if not p.has_identity or s_max_id == p_max_id else "✘ (id_mismatch)"
        else:
            diff = p_count - s_count
            if diff < 0:
                verdict = f"✘ (skip={abs(diff)})"
            else:
                verdict = f"✘ (extra={diff})"
        print(f"{table:<24} | {s_count:<8} | {p_count:<8} | {id_label:<14} | {verdict}")

    # اعتبارسنجی عمیق برای جدول کلیدی logs
    print("-" * 78)
    print(" اعتبارسنجی تکمیلی جدول logs (Checksum & Recent Events)")
    print("-" * 78)
    s_cur = sqlite_conn.cursor()
    s_cur.execute("SELECT COALESCE(SUM(id), 0) FROM logs")
    s_sum = s_cur.fetchone()[0]

    with pg_conn.cursor() as p_cur:
        p_cur.execute("SELECT COALESCE(SUM(id), 0) FROM logs")
        p_sum = p_cur.fetchone()[0]

    sum_match = s_sum == p_sum
    if not sum_match:
        all_ok = False
    print(f"SUM(id) لاگ‌ها           : SQLite={s_sum} | PG={p_sum} | {'✔ تطابق' if sum_match else '✘ مغایرت'}")

    # اضافه کردن MAX(timestamp) logs
    s_cur.execute("SELECT MAX(timestamp) FROM logs")
    s_max_ts = s_cur.fetchone()[0]
    with pg_conn.cursor() as p_cur:
        p_cur.execute("SELECT MAX(timestamp) FROM logs")
        p_max_ts = p_cur.fetchone()[0]
    ts_match = (s_max_ts == p_max_ts) or (
        isinstance(s_max_ts, str)
        and isinstance(p_max_ts, str)
        and p_max_ts.endswith("Z")
        and p_max_ts[:-1] == s_max_ts
    )
    if not ts_match:
        all_ok = False
    print(f"MAX(timestamp) logs     : SQLite={s_max_ts} | PG={p_max_ts} | {'✔' if ts_match else '✘'}")

    cutoff_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
    s_cur.execute("SELECT COUNT(*) FROM logs WHERE timestamp >= ?", (cutoff_ts,))
    s_recent = s_cur.fetchone()[0]

    with pg_conn.cursor() as p_cur:
        p_cur.execute("SELECT COUNT(*) FROM logs WHERE timestamp >= %s", (cutoff_ts,))
        p_recent = p_cur.fetchone()[0]

    recent_match = s_recent == p_recent
    if not recent_match:
        all_ok = False
    print(f"لاگ‌های ۳۰ روز اخیر       : SQLite={s_recent} | PG={p_recent} | {'✔ تطابق' if recent_match else '✘ مغایرت'}")
    print("=" * 78)

    if all_ok:
        print("✔ تمام اعتبارسنجی‌ها و چک‌سام‌های داده‌ها با موفقیت تأیید شدند.\n")
    else:
        print("✘ در اعتبارسنجی داده‌ها مغایرت مشاهده شد!\n")

    return all_ok


def print_dry_run_report(
    plan: List[TablePlan], source_path: str, target_dsn: str, quiet: bool = False
) -> None:
    """نمایش گزارش کامل و خوانا از وضعیت شبیه‌سازی مهاجرت (Dry-Run)."""
    if quiet:
        total_s = sum(p.sqlite_rows for p in plan)
        print(f"[dry-run] tables={len(plan)} total_source_rows={total_s} (zero rows written)")
        return

    safe_target = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", target_dsn)

    print("\n" + "=" * 78)
    print(" گزارش شبیه‌سازی مهاجرت داده از SQLite به PostgreSQL (Dry-Run)")
    print("=" * 78)
    print(f"منبع SQLite      : {os.path.abspath(source_path)}")
    print(f"مقصد PostgreSQL   : {safe_target}")
    print("حالت اجرا        : DRY-RUN (صفر بایت نوشته می‌شود)")
    print("-" * 78)
    print(f"{'جدول':<26} | {'SQLite':<9} | {'PG':<8} | {'استراتژی':<12} | {'IDENTITY'}")
    print("-" * 78)

    total_s = 0
    total_p = 0
    for p in plan:
        ident_label = "بله (setval)" if p.has_identity else "خیر"
        strat_label = f"{p.strategy} ({BATCH_SIZE if p.strategy == 'BATCH' else 'STREAM'})"
        print(
            f"{p.name:<26} | {p.sqlite_rows:<9} | {p.pg_rows:<8} | {strat_label:<12} | {ident_label}"
        )
        total_s += p.sqlite_rows
        total_p += p.pg_rows

    print("-" * 78)
    print(f"{'مجموع':<26} | {total_s:<9} | {total_p:<8} | {'11 جدول':<12} | {len(IDENTITY_TABLES)} توالی")
    print("=" * 78)
    print("✔ بررسی ساختار و ارتباط با هر دو دیتابیس با موفقیت انجام شد.")
    print("✔ هیچ داده‌ای نوشته نشد (کد خروج: ۳ — DRY-RUN ONLY).\n")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """تجزیه و تحلیل آرگومان‌های خط فرمان."""
    parser = argparse.ArgumentParser(
        description="ابزار مستقل مهاجرت داده از SQLite به PostgreSQL (PrintGuard Phase F)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        default="logs.db",
        help="مسیر فایل دیتابیس منبع SQLite (پیش‌فرض: logs.db)",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="DSN اتصال به PostgreSQL مقصد (پیش‌فرض: از متغیر PG_DSN یا پیش‌فرض سندباکس)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="فقط گزارش و شمارش داده‌ها بدون هیچ‌گونه نوشتن روی PostgreSQL (کد خروج ۳)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="انجام اعتبارسنجی و checksum عمیق پس از پایان مهاجرت",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="اجرای خاموش با کمترین خروجی در ترمینال",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="تخلیه کامل (TRUNCATE) تمام جدول‌های مقصد قبل از آغاز مهاجرت",
    )
    parser.add_argument(
        "--convert-naive-timestamps",
        action="store_true",
        help="تبدیل timestamp های ISO بدون Z به Z (پیش‌فرض: رد کردن + خطا). "
             "استفاده با احتیاط — توصیه می‌شود اول E.3 اجرا شود.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """نقطه ورود اصلی اسکریپت مهاجرت."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    target_dsn = resolve_pg_dsn(args.target)

    # اتصال به منبع و مقصد
    try:
        sqlite_conn = connect_sqlite_ro(args.source)
    except Exception as exc:
        log.error("خطا در باز کردن پایگاه‌داده SQLite: %s", exc)
        return EXIT_FATAL

    try:
        pg_conn = connect_pg(target_dsn)
    except Exception as exc:
        sqlite_conn.close()
        log.error("خطا در اتصال به پایگاه‌داده PostgreSQL: %s", exc)
        return EXIT_FATAL

    try:
        # تهیه نقشه مهاجرت
        plan = build_migration_plan(sqlite_conn, pg_conn)

        # پیش‌اعتبارسنجی کیفیت داده‌ها (Preflight Data Quality Validation)
        _preflight_data_quality(sqlite_conn, plan)

        # حالت Dry-Run
        if args.dry_run:
            print_dry_run_report(plan, args.source, target_dsn, quiet=args.quiet)
            return EXIT_DRY_RUN

        # اجرای واقعی مهاجرت
        stats, has_skips = execute_migration(
            sqlite_conn,
            pg_conn,
            plan,
            force=args.force,
            quiet=args.quiet,
            convert_naive=args.convert_naive_timestamps,
        )

        # اعتبارسنجی در صورت درخواست
        if args.verify:
            verify_ok = verify_migration(sqlite_conn, pg_conn, plan, quiet=args.quiet)
            if not verify_ok:
                return EXIT_PARTIAL

        # بررسی و گزارش شمارنده timestampهای بدون Z
        if _naive_ts_counter > 0:
            if not args.convert_naive_timestamps:
                log.error(
                    f"مجموع {_naive_ts_counter} timestamp بدون Z رد شد. "
                    f"E.3 باید اول اجرا شود. "
                    f"یا از --convert-naive-timestamps استفاده کن (با احتیاط)."
                )
                if _naive_ts_counter >= 100:
                    return EXIT_FATAL
                return EXIT_PARTIAL
            else:
                log.warning(
                    f"مجموع {_naive_ts_counter} timestamp بدون Z تبدیل شد (flag فعال). "
                    f"**در manifest ثبت شود** که تبدیل انجام شده."
                )

        if has_skips:
            log.warning("مهاجرت انجام شد ولی برخی سطرها رد شدند (کد خروج ۱).")
            return EXIT_PARTIAL

        log.info("مهاجرت با موفقیت کامل به پایان رسید (کد خروج ۰).")
        return EXIT_SUCCESS

    except Exception as exc:
        log.error("خطای بحرانی در جریان مهاجرت: %s", exc)
        return EXIT_FATAL
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    sys.exit(main())
