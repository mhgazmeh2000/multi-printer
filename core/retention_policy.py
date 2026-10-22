# -*- coding: utf-8 -*-
"""
core/retention_policy.py — سیاست نگهداری و پاک‌سازی داده‌های سری‌زمانی (فاز I.2).

سیاست نگهداری (Retention Policy):
  • logs: ۹۰ روز
  • sensor_readings: ۹۰ روز
  • toner_snapshots_v2: ۹۰ روز
  • security_events: ۲ سال (۷۳۰ روز)

جداول دائمی (Permanent - بدون حذف):
  • print_events
  • cartridge_lifecycle
  • daily_stats
  • yield_samples
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

RETENTION_DAYS: Dict[str, int] = {
    "logs": 90,
    "sensor_readings": 90,
    "toner_snapshots_v2": 90,
    "security_events": 730,  # 2 years
}

PERMANENT_TABLES: list[str] = [
    "print_events",
    "cartridge_lifecycle",
    "daily_stats",
    "yield_samples",
]

ALLOWED_PURGE_TABLES = set(RETENTION_DAYS.keys())


def purge_table(
    conn: Any,
    table: str,
    days: int,
    batch_size: int = 10000,
    now_dt: Optional[datetime] = None,
    dry_run: bool = False,
) -> int:
    """
    حذف ردیف‌های قدیمی‌تر از `days` روز به‌صورت دسته‌ای (batch) جهت جلوگیری از قفل طولانی جدول.

    پارامترها:
      conn: اتصال پایگاه‌داده (ConnectionAdapter یا sqlite3/psycopg connection)
      table: نام جدول (باید در ALLOWED_PURGE_TABLES باشد)
      days: تعداد روزهای نگهداری
      batch_size: اندازه هر بسته حذف (پیش‌فرض: ۱۰,۰۰۰)
      now_dt: زمان مرجع (اختیاری، جهت تست)
      dry_run: شبیه‌سازی و شمارش ردیف‌های مشمول حذف بدون حذف فیزیکی
    """
    if table in PERMANENT_TABLES:
        raise ValueError(
            f"جدول '{table}' در فهرست جداول دائمی (PERMANENT_TABLES) است و حذف آن مجاز نیست."
        )

    if table not in ALLOWED_PURGE_TABLES:
        raise ValueError(
            f"جدول '{table}' در فهرست جداول مجاز برای سیاست نگهداری نیست."
        )

    if days < 0:
        raise ValueError("تعداد روزهای نگهداری نمی‌تواند منفی باشد.")

    if batch_size <= 0:
        batch_size = 10000

    ref_dt = now_dt or datetime.now(timezone.utc)
    if ref_dt.tzinfo is None:
        ref_dt = ref_dt.replace(tzinfo=timezone.utc)
    else:
        ref_dt = ref_dt.astimezone(timezone.utc)

    cutoff_dt = ref_dt - timedelta(days=days)
    # فرمت ایزوی استاندارد ۲۷ کاراکتری پروژه
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # تشخیص اینکه آیا شیء اتصال ConnectionAdapter است یا اتصال خام psycopg
    is_pg_raw = False
    if hasattr(conn, "info") and hasattr(conn.info, "vendor") and conn.info.vendor == "PostgreSQL":
        is_pg_raw = True
    elif type(conn).__module__.startswith("psycopg"):
        is_pg_raw = True

    if dry_run:
        q_count = f"SELECT count(*) FROM {table} WHERE timestamp < %s" if is_pg_raw else f"SELECT count(*) FROM {table} WHERE timestamp < ?"
        try:
            r = conn.execute(q_count, (cutoff_iso,)).fetchone()
            count = r[0] if r else 0
        except Exception:
            count = 0
        log.info(
            "سیاست نگهداری (dry-run): %d ردیف در جدول '%s' قدیمی‌تر از %d روز است (بدون حذف).",
            count,
            table,
            days,
        )
        return count

    total_deleted = 0
    query_sqlite = f"""
        DELETE FROM {table}
        WHERE id IN (
            SELECT id FROM {table}
            WHERE timestamp < ?
            LIMIT ?
        )
    """

    query = query_sqlite
    if is_pg_raw:
        query = f"""
            DELETE FROM {table}
            WHERE id IN (
                SELECT id FROM {table}
                WHERE timestamp < %s
                LIMIT %s
            )
        """

    while True:
        cur = conn.execute(query, (cutoff_iso, batch_size))
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        if hasattr(conn, "commit"):
            conn.commit()
        total_deleted += deleted
        if deleted < batch_size:
            break

    if total_deleted > 0:
        log.info(
            "پاک‌سازی سیاست نگهداری: %d ردیف از جدول '%s' (قدیمی‌تر از %d روز) حذف شد.",
            total_deleted,
            table,
            days,
        )

    return total_deleted


def run_retention(
    conn: Any,
    retention_config: Optional[Dict[str, int]] = None,
    batch_size: int = 10000,
    now_dt: Optional[datetime] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    اجرای سیاست نگهداری برای تمامی جداول پیکربندی‌شده.
    خروجی: دیکشنری شامل تعداد ردیف‌های حذف‌شده به ازای هر جدول.
    """
    config = retention_config or RETENTION_DAYS
    results: Dict[str, int] = {}

    for table, days in config.items():
        try:
            deleted = purge_table(
                conn=conn,
                table=table,
                days=days,
                batch_size=batch_size,
                now_dt=now_dt,
                dry_run=dry_run,
            )
            results[table] = deleted
        except Exception as exc:
            log.exception("خطا در اجرای سیاست نگهداری جدول '%s': %s", table, exc)
            results[table] = -1

    return results
