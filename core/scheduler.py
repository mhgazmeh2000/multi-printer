# -*- coding: utf-8 -*-
"""
core/scheduler.py — automatic periodic scheduling of the retention policy and summarization (phase I.4).

Scheduled tasks:
  • rollup_daily_stats: every day at 01:00 UTC
  • run_retention: every day at 03:00 UTC

Support:
  • If `APScheduler` is installed: uses `BackgroundScheduler`.
  • If it is not installed: logs a warning and points to Windows Task Scheduler via `tools/run_*.py`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.retention_policy import run_retention
from core.rollup_jobs import rollup_daily_stats

log = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore


_scheduler_instance = None


def _get_active_conn(conn_or_factory: Any) -> Any:
    if conn_or_factory is not None and hasattr(conn_or_factory, "cursor"):
        return conn_or_factory
    if callable(conn_or_factory):
        return conn_or_factory()
    if conn_or_factory is not None:
        return conn_or_factory
    from core.db import get_connection
    return get_connection()


def _job_rollup(conn_or_factory: Any) -> None:
    conn = None
    try:
        conn = _get_active_conn(conn_or_factory)
        rollup_daily_stats(conn)
    except Exception as exc:
        log.exception("خطا در اجرای زمان‌بندی‌شده خلاصه‌سازی آمار روزانه: %s", exc)
    finally:
        if (conn_or_factory is None or callable(conn_or_factory)) and conn is not None:
            if hasattr(conn, "close"):
                try:
                    conn.close()
                except Exception:
                    pass


def _job_retention(conn_or_factory: Any) -> None:
    conn = None
    try:
        conn = _get_active_conn(conn_or_factory)
        run_retention(conn)
    except Exception as exc:
        log.exception("خطا در اجرای زمان‌بندی‌شده سیاست نگهداری داده‌ها: %s", exc)
    finally:
        if (conn_or_factory is None or callable(conn_or_factory)) and conn is not None:
            if hasattr(conn, "close"):
                try:
                    conn.close()
                except Exception:
                    pass


def start_scheduler(conn: Optional[Any] = None) -> Optional[Any]:
    """
    Starts the background scheduler for the retention and summarization tasks.
    """
    global _scheduler_instance

    if not HAS_APSCHEDULER:
        log.info(
            "پکیج APScheduler نصب نیست. برای زمان‌بندی خودکار از Windows Task Scheduler "
            "با فراخوانی اسکریپت‌های tools/run_retention.py و tools/run_rollup.py استفاده می‌شود."
        )
        return None

    try:
        scheduler = BackgroundScheduler(timezone="UTC")

        # Daily summarization at 01:00 UTC
        scheduler.add_job(
            _job_rollup,
            CronTrigger(hour=1, minute=0),
            args=[conn],
            id="rollup_daily",
            name="Daily stats aggregation rollup",
            replace_existing=True,
        )

        # Retention cleanup at 03:00 UTC
        scheduler.add_job(
            _job_retention,
            CronTrigger(hour=3, minute=0),
            args=[conn],
            id="retention_daily",
            name="Daily timeseries data retention purge",
            replace_existing=True,
        )

        scheduler.start()
        _scheduler_instance = scheduler
        log.info("✔ زمان‌بند APScheduler با موفقیت در پس‌زمینه فعال شد.")
        return scheduler
    except Exception as exc:
        log.exception("خطا در راه‌اندازی زمان‌بند APScheduler: %s", exc)
        return None


def stop_scheduler() -> None:
    """Stops the scheduler if it is running."""
    global _scheduler_instance
    if _scheduler_instance is not None:
        try:
            _scheduler_instance.shutdown(wait=False)
            log.info("زمان‌بند با موفقیت متوقف شد.")
        except Exception as exc:
            log.error("خطا در توقف زمان‌بند: %s", exc)
        finally:
            _scheduler_instance = None
