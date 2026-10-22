#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/run_rollup.py — اسکریپت اجرای دوره‌ای خلاصه‌سازی روزانه آمار (فاز I.3/I.4).

قابلیت اجرا به‌صورت دستی یا از طریق Windows Task Scheduler:
    python tools/run_rollup.py [--date YYYY-MM-DD] [--backfill-days 7]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# افزودن مسیر ریشه پروژه به sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import connect
from core.rollup_jobs import backfill_daily_stats, rollup_daily_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_rollup")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="اجرای خلاصه‌سازی روزانه آمار پرینترها و تونرها در جدول daily_stats"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="تاریخ موردنظر برای خلاصه‌سازی (قالب YYYY-MM-DD، پیش‌فرض: دیروز)",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="تعداد روزهای گذشته برای اجرای backfill (مثلاً 7 یا 30)",
    )
    parser.add_argument(
        "--backfill-start",
        type=str,
        default=None,
        help="تاریخ شروع بازتولید آمار (قالب YYYY-MM-DD)",
    )
    parser.add_argument(
        "--backfill-end",
        type=str,
        default=None,
        help="تاریخ پایان بازتولید آمار (قالب YYYY-MM-DD، پیش‌فرض: دیروز)",
    )
    args = parser.parse_args()

    try:
        conn = connect()
    except Exception as exc:
        log.error("خطا در اتصال به پایگاه‌داده: %s", exc)
        return 1

    try:
        if args.backfill_start:
            today = datetime.now(timezone.utc).date()
            start = args.backfill_start
            end = args.backfill_end or (today - timedelta(days=1))
            log.info("شروع جبران آمار گذشته (Backfill) از %s تا %s...", start, end)
            total = backfill_daily_stats(conn, start_date=start, end_date=end)
            log.info("✔ بازتولید آمار با موفقیت انجام شد: %d رکورد پرینتر ثبت شد.", total)
        elif args.backfill_days > 0:
            today = datetime.now(timezone.utc).date()
            start = today - timedelta(days=args.backfill_days)
            end = today - timedelta(days=1)
            log.info("شروع جبران آمار گذشته (Backfill) از %s تا %s...", start, end)
            total = backfill_daily_stats(conn, start_date=start, end_date=end)
            log.info("✔ بازتولید آمار با موفقیت انجام شد: %d رکورد پرینتر ثبت شد.", total)
        else:
            target_str = args.date or "دیروز"
            log.info("شروع خلاصه‌سازی روزانه برای تاریخ: %s", target_str)
            count = rollup_daily_stats(conn, target_date=args.date)
            log.info("✔ خلاصه‌سازی با موفقیت انجام شد: آمار %d پرینتر ثبت/به‌روز شد.", count)

        return 0
    except Exception as exc:
        log.exception("خطا در اجرای خلاصه‌سازی آمار: %s", exc)
        return 1
    finally:
        if hasattr(conn, "close"):
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
