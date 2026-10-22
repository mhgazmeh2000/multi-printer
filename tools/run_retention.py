#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/run_retention.py — اسکریپت اجرای دوره‌ای سیاست نگهداری داده‌ها (فاز I.2/I.4).

قابلیت اجرا به‌صورت دستی یا از طریق Windows Task Scheduler:
    python tools/run_retention.py [--days-logs 90] [--batch-size 10000]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# افزودن مسیر ریشه پروژه به sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import connect
from core.retention_policy import RETENTION_DAYS, run_retention

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_retention")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="اجرای سیاست نگهداری و پاک‌سازی داده‌های قدیمی پرینت‌گارد"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10000,
        help="اندازه هر بسته حذف برای جلوگیری از قفل طولانی (پیش‌فرض: ۱۰۰۰۰)",
    )
    parser.add_argument(
        "--days-logs",
        type=int,
        default=RETENTION_DAYS.get("logs", 90),
        help="تعداد روز نگهداری جدول logs",
    )
    parser.add_argument(
        "--days-sensors",
        type=int,
        default=RETENTION_DAYS.get("sensor_readings", 90),
        help="تعداد روز نگهداری جدول sensor_readings",
    )
    parser.add_argument(
        "--days-toner",
        type=int,
        default=RETENTION_DAYS.get("toner_snapshots_v2", 90),
        help="تعداد روز نگهداری جدول toner_snapshots_v2",
    )
    parser.add_argument(
        "--days-security",
        type=int,
        default=RETENTION_DAYS.get("security_events", 730),
        help="تعداد روز نگهداری جدول security_events",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="شبیه‌سازی اجرای سیاست و شمارش ردیف‌های منقضی بدون حذف فیزیکی",
    )
    args = parser.parse_args()

    custom_config = {
        "logs": args.days_logs,
        "sensor_readings": args.days_sensors,
        "toner_snapshots_v2": args.days_toner,
        "security_events": args.days_security,
    }

    if args.dry_run:
        log.info("شروع شبیه‌سازی سیاست نگهداری داده‌ها (DRY-RUN) با تنظیمات: %s", custom_config)
    else:
        log.info("شروع اجرای سیاست نگهداری داده‌ها با تنظیمات: %s", custom_config)

    try:
        conn = connect()
    except Exception as exc:
        log.error("خطا در اتصال به پایگاه‌داده: %s", exc)
        return 1

    try:
        results = run_retention(
            conn=conn,
            retention_config=custom_config,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        has_error = False
        for tbl, count in results.items():
            if count < 0:
                has_error = True
                log.error("❌ جدول '%s': با خطا متوقف شد.", tbl)
            else:
                log.info("✔ جدول '%s': %d سطر قدیمی پاک‌سازی شد.", tbl, count)

        if has_error:
            return 1

        log.info("پایان موفقیت‌آمیز عملیات سیاست نگهداری داده‌ها.")
        return 0
    finally:
        if hasattr(conn, "close"):
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
