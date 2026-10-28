#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tools/auto_migrate.py
# @brief:        Safe, idempotent automatic migration of data to PostgreSQL at startup.
# @details:      Responsibilities: Resolve the backend, scan seed tables and migrate them once.
#                Main API: resolve_backend(), resolve_dsn(), scan_seed_tables(),
#                get_user_tables(), check_users_count(), main()
#                Side effects: database access, runs external processes
# @dependencies: external: dotenv, psycopg
# @notes:        Reads environment variables: DB_BACKEND, PG_DSN. Can be run directly as a script.
# =============================================================================

"""
tools/auto_migrate.py — Tool to perform automatic, safe and Idempotent migration of data to PostgreSQL at startup (phase F).

Safety logic:
  1. Reading DB_BACKEND from environment or.env file. If it was something other than postgresql (or its equivalents),
     The script exits unchanged with an exit code of 0.
  2. Checking the existence and number of rows of users table in PostgreSQL.
  3. If the number of rows is > 0: the database is already initialized; No need to run the migration again
     And the script exits with exit code 0 (Idempotent property).
  4. If the number of rows == 0:
     A) Security check before any TRUNCATE:
         - Scan all migration files in alembic/versions/ to extract tables with INSERT INTO command (seed tables).
         - In case of no detection or an error in the scan, quite conservatively, only the "sensor_readings" table
           It is considered as an authorized seed table and the rest of the tables will be unauthorized.
         - checking all the tables in PostgreSQL; If a table outside the seed list has a row:
           The operation is immediately stopped with an explicit security error message ("Real data exists in table X; migration aborted.")
           exits (exit code 1).
     b) If only seed tables have rows:
         - Only those seed tables are specifically emptied (TRUNCATE).
     c) Implementation of the main migration tool:
         tools/migrate_sqlite_to_pg.py --verify
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

VERSIONS_DIR = PROJECT_ROOT / "alembic" / "versions"
MIGRATE_SCRIPT = PROJECT_ROOT / "tools" / "migrate_sqlite_to_pg.py"

BACKEND_POSTGRESQL = "postgresql"
BACKEND_ALIASES = {"postgres": BACKEND_POSTGRESQL, "pg": BACKEND_POSTGRESQL}
DEFAULT_SEED_TABLES = {"sensor_readings"}


def _load_env_if_present() -> None:
    """Load variables from.env file if present without overwriting current environment variables."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(dotenv_path=env_file, override=False)
        except ImportError:
            # Simple manual parsing for cases where dotenv is not installed yet
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


def resolve_backend() -> str:
    """Identifying the database backend with priority environment and then.env (default: sqlite)."""
    _load_env_if_present()
    raw = os.getenv("DB_BACKEND", "").strip().lower()
    return BACKEND_ALIASES.get(raw, raw) or "sqlite"


def resolve_dsn(target_arg: Optional[str] = None) -> Optional[str]:
    """Get DSN connection to PostgreSQL."""
    if target_arg and target_arg.strip():
        return target_arg.strip()
    _load_env_if_present()
    dsn = os.getenv("PG_DSN", "").strip()
    return dsn if dsn else None


def scan_seed_tables(versions_dir: Path) -> Set[str]:
    """
    Scan migration files in alembic/versions/ to find tables that have INSERT INTO.
    A conservative fallback of {'sensor_readings'} is returned if there are none or an error occurs.
    """
    pattern = re.compile(r"INSERT\s+INTO\s+[\"`']?([a-zA-Z0-9_]+)[\"`']?", re.IGNORECASE)
    discovered: Set[str] = set()

    try:
        if versions_dir.is_dir():
            for py_file in versions_dir.glob("*.py"):
                try:
                    text = py_file.read_text(encoding="utf-8", errors="replace")
                    for match in pattern.findall(text):
                        tbl = match.strip().lower()
                        if tbl and tbl != "alembic_version":
                            discovered.add(tbl)
                except Exception:
                    pass
    except Exception:
        pass

    if not discovered:
        return set(DEFAULT_SEED_TABLES)
    return discovered


def get_user_tables(conn) -> List[str]:
    """List of tables in the current PostgreSQL schema (without alembic_version)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename 
            FROM pg_tables 
            WHERE schemaname = current_schema()
            ORDER BY tablename;
            """
        )
        return [r[0] for r in cur.fetchall() if r[0] != "alembic_version"]


def check_users_count(conn) -> Optional[int]:
    """
    Checking the existence of the users table and returning the number of its rows.
    Returns None if the table does not exist.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 
            FROM pg_tables 
            WHERE schemaname = current_schema() AND tablename = 'users';
            """
        )
        if not cur.fetchone():
            return None
        cur.execute('SELECT COUNT(*) FROM "users"')
        return int(cur.fetchone()[0])


def main(argv: Optional[List[str]] = None) -> int:
    """Command-line entry point of the automatic migration."""

    parser = argparse.ArgumentParser(
        description="ابزار اجرای خودکار و ایمن مهاجرت به PostgreSQL در زمان استارت پروژه PrintGuard."
    )
    parser.add_argument(
        "--source",
        default="logs.db",
        help="مسیر فایل دیتابیس منبع SQLite (پیش‌فرض: logs.db)",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="DSN اتصال به PostgreSQL (در صورت خالی بودن از PG_DSN محیط/.env خوانده می‌شود)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="فقط وضعیت را گزارش کن و هیچ تغییری اعمال نکن",
    )
    args = parser.parse_args(argv)

    # 1. Backend type check
    backend = resolve_backend()
    if backend != BACKEND_POSTGRESQL:
        print(
            f"[auto_migrate] پیکربندی DB_BACKEND برابر «{backend}» است (نه postgresql)؛ "
            "مهاجرت خودکار لغو شد."
        )
        return 0

    # 2. Checking the connection to PostgreSQL
    target_dsn = resolve_dsn(args.target)
    if not target_dsn:
        print(
            "[auto_migrate] ❌ خطا: DB_BACKEND=postgresql است اما متغیر PG_DSN تنظیم نشده است.",
            file=sys.stderr,
        )
        return 1

    try:
        import psycopg
    except ImportError:
        print(
            "[auto_migrate] ❌ خطا: پکیج psycopg یافت نشد. pip install -r requirements.txt را اجرا کنید.",
            file=sys.stderr,
        )
        return 1

    try:
        conn = psycopg.connect(target_dsn, autocommit=True, connect_timeout=10)
    except Exception as exc:
        print(
            f"[auto_migrate] ❌ خطا در برقراری اتصال به PostgreSQL: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        # 3. Checking the existence of the users table and counting the rows
        users_count = check_users_count(conn)
        if users_count is None:
            print(
                "[auto_migrate] ❌ جدول users در پایگاه‌داده مقصد یافت نشد. "
                "ابتدا اسکیمای Alembic را اجرا کنید: python -m alembic upgrade head",
                file=sys.stderr,
            )
            return 1

        if users_count > 0:
            print(
                f"[auto_migrate] ✔ تعداد ردیف‌های جدول users برابر {users_count} است؛ "
                "داده‌ها از قبل در دیتابیس موجودند. نیازی به مهاجرت نیست (Idempotent)."
            )
            return 0

        print("[auto_migrate] جدول users خالی است (count = 0). بررسی ایمنی پیش از آغاز مهاجرت...")

        # 4. Security check before any TRUNCATE
        seed_tables = scan_seed_tables(VERSIONS_DIR)
        user_tables = get_user_tables(conn)

        table_counts = {}
        for tbl in user_tables:
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
                cnt = int(cur.fetchone()[0])
                if cnt > 0:
                    table_counts[tbl] = cnt

        # Does a table have rows outside the seed list?
        non_seed_occupied = {
            t: c for t, c in table_counts.items() if t not in seed_tables
        }
        if non_seed_occupied:
            first_table = sorted(non_seed_occupied.keys())[0]
            first_count = non_seed_occupied[first_table]
            details = ", ".join(f"«{t}»={c}" for t, c in sorted(non_seed_occupied.items()))
            print(
                f"[auto_migrate] ❌ خطای امنیتی: داده‌ی واقعی در جدول {first_table} وجود دارد "
                f"({first_count} سطر)؛ migration لغو شد.\n"
                f"                جداول دارای داده: {details}",
                file=sys.stderr,
            )
            return 1

        # If we come this far, only the seed tables may have data
        seed_occupied = [t for t in table_counts if t in seed_tables]
        if seed_occupied:
            print(
                f"[auto_migrate] پاک‌سازی جدول‌های مجاز seed قبل از شروع مهاجرت: "
                f"{', '.join(seed_occupied)}"
            )
            with conn.cursor() as cur:
                for st in seed_occupied:
                    cur.execute(f'TRUNCATE TABLE "{st}" RESTART IDENTITY CASCADE')
            print("[auto_migrate] ✔ جدول‌های seed با موفقیت تخلیه شدند.")

        if args.check_only:
            print("[auto_migrate] بررسی ایمنی با موفقیت انجام شد (--check-only فعال بود).")
            return 0

        # 5. Run the main migration tool
        if not MIGRATE_SCRIPT.is_file():
            print(
                f"[auto_migrate] ❌ اسکریپت مهاجرت در مسیر {MIGRATE_SCRIPT} پیدا نشد.",
                file=sys.stderr,
            )
            return 1

        cmd = [sys.executable, str(MIGRATE_SCRIPT), "--verify"]
        if args.source:
            cmd.extend(["--source", str(args.source)])
        if target_dsn:
            cmd.extend(["--target", str(target_dsn)])

        print("[auto_migrate] در حال اجرای فرآیند مهاجرت داده...")
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if proc.returncode == 0:
            print("[auto_migrate] ✔ مهاجرت خودکار با موفقیت کامل انجام شد.")
            return 0
        else:
            print(
                f"[auto_migrate] ❌ مهاجرت با کد خطای {proc.returncode} متوقف شد.",
                file=sys.stderr,
            )
            return proc.returncode

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
