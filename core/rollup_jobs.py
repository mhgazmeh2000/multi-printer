# -*- coding: utf-8 -*-
"""
core/rollup_jobs.py — daily summarization and aggregation of print statistics and consumables (phase I.3).

Goals:
  • Automatically generate `daily_stats` records for every printer, every day (based on UTC).
  • Aggregate total counters, the color/mono split, the A3/A4 paper-size split, and toner averages.
  • Support idempotent regeneration with `ON CONFLICT (date, printer_ip) DO UPDATE`.
  • Support regeneration and backfill of past data, reading historical data from `logs` when `print_events` is empty.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger(__name__)


def _parse_date(d: Union[date, str, None]) -> date:
    if d is None:
        # Default: yesterday, based on the UTC calendar
        return (datetime.now(timezone.utc) - timedelta(days=1)).date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d).strip()[:10], "%Y-%m-%d").date()


def rollup_daily_stats(
    conn: Any,
    target_date: Union[date, str, None] = None,
) -> int:
    """
    Computes and stores the daily statistics of all printers in the `daily_stats` table for a specific date.
    Output: number of printers whose statistics were recorded/updated.
    """
    dt_obj = _parse_date(target_date)
    date_str = dt_obj.strftime("%Y-%m-%d")

    # Time range of the day, in the project's 27-character format
    start_ts = f"{date_str}T00:00:00.000000Z"
    next_day = dt_obj + timedelta(days=1)
    end_ts = f"{next_day.strftime('%Y-%m-%d')}T00:00:00.000000Z"

    processed_count = 0

    # 1. Get the list of active printers on this date
    # from print_events, logs and toner_snapshots_v2
    printer_ips = set()

    # a) from print_events
    try:
        rows = conn.execute(
            "SELECT DISTINCT printer_ip FROM print_events WHERE timestamp >= ? AND timestamp < ?",
            (start_ts, end_ts),
        ).fetchall()
        for row in rows:
            if row[0]:
                printer_ips.add(row[0])
    except Exception:
        pass

    # b) from logs
    try:
        rows = conn.execute(
            "SELECT DISTINCT printer_ip FROM logs WHERE timestamp >= ? AND timestamp < ?",
            (start_ts, end_ts),
        ).fetchall()
        for row in rows:
            if row[0]:
                printer_ips.add(row[0])
    except Exception:
        pass

    # c) from toner_snapshots_v2
    try:
        rows = conn.execute(
            "SELECT DISTINCT printer_ip FROM toner_snapshots_v2 WHERE timestamp >= ? AND timestamp < ?",
            (start_ts, end_ts),
        ).fetchall()
        for row in rows:
            if row[0]:
                printer_ips.add(row[0])
    except Exception:
        pass

    if not printer_ips:
        log.info("خلاصه‌سازی روزانه (%s): هیچ رویدادی برای این تاریخ یافت نشد.", date_str)
        return 0

    for ip in sorted(printer_ips):
        stats = _compute_printer_day_stats(conn, ip, start_ts, end_ts)
        _upsert_daily_stats(conn, date_str, ip, stats)
        processed_count += 1

    if hasattr(conn, "commit"):
        conn.commit()

    log.info(
        "خلاصه‌سازی روزانه (%s): آمار %d پرینتر با موفقیت ثبت/به‌روز شد.",
        date_str,
        processed_count,
    )
    return processed_count


def _compute_printer_day_stats(
    conn: Any,
    ip: str,
    start_ts: str,
    end_ts: str,
) -> Dict[str, Any]:
    """Computes the statistics of one printer in the requested time range."""
    printer_name = None
    total_pages = 0
    color_pages = 0
    mono_pages = 0
    a3_pages = 0
    a4_pages = 0
    print_events_count = 0

    # Check print_events
    has_print_events = False
    try:
        row = conn.execute(
            """
            SELECT
                MAX(printer_name),
                COUNT(*),
                COALESCE(SUM(pages), 0),
                COALESCE(SUM(CASE WHEN color_mode = 'color' THEN pages ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN color_mode = 'mono' THEN pages ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN paper_size IN ('A3', 'Large (A3/B4)') THEN pages ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN paper_size IN ('A4', 'Small (A4/A5)') THEN pages ELSE 0 END), 0)
            FROM print_events
            WHERE printer_ip = ? AND timestamp >= ? AND timestamp < ?
            """,
            (ip, start_ts, end_ts),
        ).fetchone()
        if row and row[1] and row[1] > 0:
            has_print_events = True
            printer_name = row[0]
            print_events_count = row[1] or 0
            total_pages = row[2] or 0
            color_pages = row[3] or 0
            mono_pages = row[4] or 0
            a3_pages = row[5] or 0
            a4_pages = row[6] or 0
    except Exception:
        has_print_events = False

    # If there is no data in print_events, extract the fallback from the logs table
    if not has_print_events:
        try:
            rows = conn.execute(
                """
                SELECT details, message
                FROM logs
                WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ? AND timestamp < ?
                """,
                (ip, start_ts, end_ts),
            ).fetchall()
            for r in rows:
                print_events_count += 1
                details_raw = r[0]
                d = {}
                if isinstance(details_raw, dict):
                    d = details_raw
                elif isinstance(details_raw, str) and details_raw.strip():
                    try:
                        d = json.loads(details_raw)
                    except Exception:
                        d = {}

                pg = d.get("pages") or d.get("printed_pages") or 1
                try:
                    pg = int(pg)
                except Exception:
                    pg = 1

                total_pages += pg
                cmode = d.get("color_mode") or d.get("mode") or ""
                if cmode == "color":
                    color_pages += pg
                elif cmode == "mono":
                    mono_pages += pg

                psize = d.get("paper_size") or ""
                if "A3" in psize:
                    a3_pages += pg
                elif "A4" in psize:
                    a4_pages += pg
        except Exception as exc:
            log.debug("خطا در خواندن logs fallback برای %s: %s", ip, exc)

    # Fallback device name from the last log status
    if not printer_name:
        try:
            r = conn.execute(
                "SELECT message FROM logs WHERE printer_ip = ? AND message IS NOT NULL ORDER BY id DESC LIMIT 1",
                (ip,),
            ).fetchone()
            if r and r[0]:
                printer_name = str(r[0])[:50]
        except Exception:
            pass

    # 2. Compute toner averages from toner_snapshots_v2
    toner_stats = _compute_toner_averages(conn, ip, start_ts, end_ts)

    return {
        "printer_name": printer_name,
        "total_pages": total_pages,
        "color_pages": color_pages,
        "mono_pages": mono_pages,
        "a3_pages": a3_pages,
        "a4_pages": a4_pages,
        "print_events_count": print_events_count,
        **toner_stats,
    }


def _compute_toner_averages(
    conn: Any,
    ip: str,
    start_ts: str,
    end_ts: str,
) -> Dict[str, Optional[float]]:
    """Computes the average and lowest toner levels for the printer over the day range."""
    result = {
        "avg_toner_black": None,
        "avg_toner_cyan": None,
        "avg_toner_magenta": None,
        "avg_toner_yellow": None,
        "min_toner_black": None,
    }
    try:
        rows = conn.execute(
            """
            SELECT color, AVG(toner_level), MIN(toner_level)
            FROM toner_snapshots_v2
            WHERE printer_ip = ? AND timestamp >= ? AND timestamp < ? AND toner_level >= 0
            GROUP BY color
            """,
            (ip, start_ts, end_ts),
        ).fetchall()
        for row in rows:
            c = str(row[0]).lower() if row[0] else ""
            avg_val = round(float(row[1]), 2) if row[1] is not None else None
            min_val = round(float(row[2]), 2) if row[2] is not None else None
            if c in ("black", "k"):
                result["avg_toner_black"] = avg_val
                result["min_toner_black"] = min_val
            elif c in ("cyan", "c"):
                result["avg_toner_cyan"] = avg_val
            elif c in ("magenta", "m"):
                result["avg_toner_magenta"] = avg_val
            elif c in ("yellow", "y"):
                result["avg_toner_yellow"] = avg_val
    except Exception as exc:
        log.debug("عدم محاسبه تونرها از toner_snapshots_v2 برای %s: %s", ip, exc)

    return result


def _upsert_daily_stats(
    conn: Any,
    date_str: str,
    ip: str,
    stats: Dict[str, Any],
) -> None:
    """Safely inserts or updates a row in the daily_stats table."""
    # Detect the parameter style (%s for PG or ? for SQLite)
    is_pg_raw = False
    if hasattr(conn, "info") and hasattr(conn.info, "vendor") and conn.info.vendor == "PostgreSQL":
        is_pg_raw = True
    elif type(conn).__module__.startswith("psycopg"):
        is_pg_raw = True

    ph = "%s" if is_pg_raw else "?"

    sql = f"""
        INSERT INTO daily_stats (
            date, printer_ip, printer_name,
            total_pages, color_pages, mono_pages,
            a3_pages, a4_pages,
            avg_toner_black, avg_toner_cyan,
            avg_toner_magenta, avg_toner_yellow,
            min_toner_black,
            print_events_count
        ) VALUES (
            {ph}, {ph}, {ph},
            {ph}, {ph}, {ph},
            {ph}, {ph},
            {ph}, {ph},
            {ph}, {ph},
            {ph},
            {ph}
        )
        ON CONFLICT (date, printer_ip) DO UPDATE SET
            printer_name = COALESCE(EXCLUDED.printer_name, daily_stats.printer_name),
            total_pages = EXCLUDED.total_pages,
            color_pages = EXCLUDED.color_pages,
            mono_pages = EXCLUDED.mono_pages,
            a3_pages = EXCLUDED.a3_pages,
            a4_pages = EXCLUDED.a4_pages,
            avg_toner_black = COALESCE(EXCLUDED.avg_toner_black, daily_stats.avg_toner_black),
            avg_toner_cyan = COALESCE(EXCLUDED.avg_toner_cyan, daily_stats.avg_toner_cyan),
            avg_toner_magenta = COALESCE(EXCLUDED.avg_toner_magenta, daily_stats.avg_toner_magenta),
            avg_toner_yellow = COALESCE(EXCLUDED.avg_toner_yellow, daily_stats.avg_toner_yellow),
            min_toner_black = COALESCE(EXCLUDED.min_toner_black, daily_stats.min_toner_black),
            print_events_count = EXCLUDED.print_events_count
    """
    params = (
        date_str,
        ip,
        stats["printer_name"],
        stats["total_pages"],
        stats["color_pages"],
        stats["mono_pages"],
        stats["a3_pages"],
        stats["a4_pages"],
        stats["avg_toner_black"],
        stats["avg_toner_cyan"],
        stats["avg_toner_magenta"],
        stats["avg_toner_yellow"],
        stats["min_toner_black"],
        stats["print_events_count"],
    )
    conn.execute(sql, params)


def backfill_daily_stats(
    conn: Any,
    start_date: Union[date, str],
    end_date: Union[date, str, None] = None,
) -> int:
    """
    Runs the summarization over a range of days to backfill and regenerate past statistics (Backfill).
    Output: total number of records generated.
    """
    s = _parse_date(start_date)
    e = _parse_date(end_date) if end_date else datetime.now(timezone.utc).date()

    if s > e:
        s, e = e, s

    total_records = 0
    curr = s
    while curr <= e:
        count = rollup_daily_stats(conn, target_date=curr)
        total_records += count
        curr += timedelta(days=1)

    log.info("تکمیل بازتولید آمار (Backfill) از %s تا %s: مجموعاً %d رکورد.", s, e, total_records)
    return total_records
