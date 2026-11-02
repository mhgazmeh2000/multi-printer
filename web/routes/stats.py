# =============================================================================
# @file:         web/routes/stats.py
# @brief:        API routes for daily statistics, including sensor statistics.
# @details:      Responsibilities: Aggregate daily counters for the dashboard charts.
#                Main API: api_daily_stats(), api_sensor_daily_stats(), api_sensor_series()
#                Side effects: database access, defines HTTP routes
# @dependencies: internal: core, core.db, core.time_utils | external: flask
# @notes:        None.
# =============================================================================

"""Statistics API routes."""

import logging
import re
from datetime import datetime, time, timedelta, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user

from core.db import DatabaseError, UndefinedColumn, UndefinedTable, get_connection
from core import store
from core.time_utils import local_tz, parse_utc, to_filter_bound, to_local_day, to_utc_z
from web.auth import user_allowed_offices, user_can_access_office, allowed_printer_ips

bp = Blueprint("stats", __name__)
log = logging.getLogger("PrinterMonitor")


# ─── Phase E.3 helpers
# Until E.3 the stored data was naive local time, and daily grouping with
# `substr(timestamp, 1, 10)` was done in SQL. The data is now UTC/Z, so a "day"
# must be a local day (Asia/Tehran). Otherwise every row after 20:30 Tehran time
# falls into the next day's bucket. Grouping is done in Python rather than SQL so that
# the results stay the same on both backends (SQLite today, PostgreSQL in E.4).

def _local_day_start(days_ago: int) -> str:
    """Local midnight of "today minus days_ago" in standard Z format (SQL filter boundary)."""
    day = datetime.now(local_tz()).date() - timedelta(days=days_ago)
    return to_utc_z(datetime.combine(day, time.min, tzinfo=local_tz()))


def _sql_numeric(raw) -> float:
    """Implicit SQLite conversion in SUM/AVG: non-numeric text ⇒ 0 (no exceptions)."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _json_number(value: float):
    """The integer must return an integer like SQLite (3 not 3.0) so that the API response does not change."""
    return int(value) if float(value).is_integer() else value


@bp.route('/api/stats/daily')
def api_daily_stats():
    """Return daily statistics, for one printer or for all printers."""

    ip = request.args.get('ip')
    days = min(request.args.get('days', default=30, type=int), 365)
    start_utc = _local_day_start(days)

    try:
        # Phase D: connection from the adapter layer (core.db); The same timeout as before.
        conn = get_connection(timeout=30.0)

        # Phase E.3: SELECT rows (without GROUP BY) and local day grouping in Python.
        # Phase E.4.6: the raw cursor was removed ⇒ ``conn.execute`` is used. The adapter
        # translates ? placeholders to %s on PG through the same path, so two SQL styles are not needed.
        if ip:
            rows = conn.execute('''
                SELECT timestamp, pages
                FROM logs
                WHERE printer_ip = ? AND type = 'PRINT' AND timestamp >= ?
                  AND pages IS NOT NULL AND pages > 0
            ''', (ip, start_utc)).fetchall()
        else:
            rows = conn.execute('''
                SELECT timestamp, pages
                FROM logs
                WHERE type = 'PRINT' AND timestamp >= ?
                  AND pages IS NOT NULL AND pages > 0
            ''', (start_utc,)).fetchall()

        conn.close()

        date_dict = {}
        skipped = 0
        for ts, pages in rows:
            try:
                day = to_local_day(ts)
            except ValueError:
                skipped += 1
                continue
            date_dict[day] = date_dict.get(day, 0.0) + _sql_numeric(pages)
        if skipped:
            log.warning(f"stats/daily: {skipped} ردیف با timestamp نامعتبر "
                        f"(مهاجرت‌نشده؟) در گروه‌بندی نادیده گرفته شد")

        start_dt = datetime.now(local_tz()).date() - timedelta(days=days)
        all_dates = []
        all_totals = []
        for i in range(days + 1):
            d = (start_dt + timedelta(days=i)).isoformat()
            all_dates.append(d)
            all_totals.append(_json_number(date_dict.get(d, 0)))

        return jsonify({
            "dates": all_dates,
            "totals": all_totals,
            "printer_ip": ip,
            "days": days
        })
    except Exception as e:
        log.error(f"Stats API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@bp.route('/api/stats/sensor/daily')
def api_sensor_daily_stats():
    """Average daily sensor temperature and humidity from sensor_readings table."""
    ip = request.args.get('ip')
    days = min(request.args.get('days', default=30, type=int), 365)
    start_utc = _local_day_start(days)
    if not ip:
        return jsonify({"error": "ip required"}), 400

    try:
        # Phase D: connection from the adapter layer (core.db); The same timeout as before.
        conn = get_connection(timeout=30.0)
        # Phase E.3: Daily AVG and GROUP BY in Python (local day) — same reason as endpoint above.
        # Phase E.4.6: Removed raw cursor ⇒ ``conn.execute''.
        rows = conn.execute('''
            SELECT timestamp, kind, value
            FROM sensor_readings
            WHERE printer_ip = ? AND timestamp >= ? AND value IS NOT NULL
              AND kind IN ('temperature', 'humidity')
        ''', (ip, start_utc)).fetchall()
        conn.close()

        sums = {}
        counts = {}
        skipped = 0
        for ts, kind, value in rows:
            try:
                day = to_local_day(ts)
            except ValueError:
                skipped += 1
                continue
            key = (day, kind)
            sums[key] = sums.get(key, 0.0) + _sql_numeric(value)
            counts[key] = counts.get(key, 0) + 1
        if skipped:
            log.warning(f"stats/sensor/daily: {skipped} ردیف با timestamp نامعتبر "
                        f"(مهاجرت‌نشده؟) در گروه‌بندی نادیده گرفته شد")

        temp_by_day = {}
        hum_by_day = {}
        for (day, kind), total in sums.items():
            avg_value = total / counts[(day, kind)]
            if kind == 'temperature':
                temp_by_day[day] = round(avg_value, 1)
            elif kind == 'humidity':
                hum_by_day[day] = round(avg_value, 1)

        # If the sensor_readings table still does not have enough data, fallback from the live snapshot of the sensor for today.
        # This makes the chart have something to show immediately after the first poll.
        today = datetime.now(local_tz()).date().isoformat()
        if today not in temp_by_day or today not in hum_by_day:
            with store.data_lock:
                live = dict(store.printer_data.get(ip) or {})
            live_readings = live.get('sensor_readings') or []
            temps = [float(r.get('value')) for r in live_readings if r.get('kind') == 'temperature' and r.get('value') is not None]
            hums = [float(r.get('value')) for r in live_readings if r.get('kind') == 'humidity' and r.get('value') is not None]
            if temps and today not in temp_by_day:
                temp_by_day[today] = round(sum(temps) / len(temps), 1)
            if hums and today not in hum_by_day:
                hum_by_day[today] = round(sum(hums) / len(hums), 1)

        start_dt = datetime.now(local_tz()).date() - timedelta(days=days)
        dates = []
        avg_temp = []
        avg_humidity = []
        for i in range(days + 1):
            d = (start_dt + timedelta(days=i)).isoformat()
            dates.append(d)
            avg_temp.append(temp_by_day.get(d))
            avg_humidity.append(hum_by_day.get(d))

        return jsonify({
            "dates": dates,
            "avg_temperature": avg_temp,
            "avg_humidity": avg_humidity,
            "printer_ip": ip,
            "days": days,
        })
    # Phase E.4.4: Errors come from adapter with common type (not sqlite3.*). Equivalent
    # The exact two previous restrictions: missing table/column in the old DB + general database error.
    except (UndefinedTable, UndefinedColumn) as e:
        # If the old DB is not yet init.
        log.error(f"Sensor stats API DB error: {e}", exc_info=True)
        return jsonify({"dates": [], "avg_temperature": [], "avg_humidity": [], "printer_ip": ip, "days": days})
    except DatabaseError as e:
        # Database structural errors should not stop the entire dashboard graph with an HTTP 500.
        log.error(f"Sensor stats API database error: {e}", exc_info=True)
        return jsonify({"dates": [], "avg_temperature": [], "avg_humidity": [], "printer_ip": ip, "days": days})
    except Exception as e:
        log.error(f"Sensor stats API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ─── Sensor time-series (line chart with a free time-range filter) ───────────
#: Allowed aggregation steps in seconds (auto picks the smallest that keeps the chart light).
_SERIES_STEPS = (60, 300, 900, 1800, 3600, 10800, 21600, 86400)
_SERIES_MAX_POINTS = 800
_SERIES_MAX_DAYS = 366
_SERIES_MAX_ROWS = 600000


def _series_pick_step(span_seconds: float, requested: str) -> int:
    """Return the bucket size in seconds for a span (``auto`` or an allowed explicit step)."""
    if requested and requested != "auto":
        try:
            value = int(requested)
        except ValueError:
            value = 0
        if value in _SERIES_STEPS:
            return value
    for step in _SERIES_STEPS:
        if span_seconds / step <= _SERIES_MAX_POINTS:
            return step
    return _SERIES_STEPS[-1]


def _series_row_epoch(ts):
    """Row timestamp (str or datetime) -> epoch seconds; None when invalid/naive."""
    try:
        dt = ts if isinstance(ts, datetime) else parse_utc(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


@bp.route('/api/stats/sensor/series')
def api_sensor_series():
    """
    Sensor temperature/humidity as a time series for a free time range.

    Query: ip (required) · start / end (ISO; naive values are Tehran local time) ·
    hours (alternative to start/end: last N hours) · port (optional) ·
    bucket (``auto`` or one of 60,300,900,1800,3600,10800,21600,86400 seconds).
    """
    ip = request.args.get('ip')
    if not ip:
        return jsonify({"error": "ip required"}), 400

    now = datetime.now(timezone.utc)
    end_raw = request.args.get('end')
    start_raw = request.args.get('start')
    hours = request.args.get('hours', type=float)
    try:
        end_dt = parse_utc(to_filter_bound(end_raw)) if end_raw else now
        if start_raw:
            start_dt = parse_utc(to_filter_bound(start_raw))
        else:
            start_dt = end_dt - timedelta(hours=hours if hours and hours > 0 else 24)
    except ValueError:
        return jsonify({"error": "invalid start/end"}), 400

    if start_dt >= end_dt:
        return jsonify({"error": "start must be before end"}), 400
    if end_dt - start_dt > timedelta(days=_SERIES_MAX_DAYS):
        start_dt = end_dt - timedelta(days=_SERIES_MAX_DAYS)

    port = request.args.get('port', type=int)
    span = (end_dt - start_dt).total_seconds()
    step = _series_pick_step(span, request.args.get('bucket', 'auto'))
    start_z, end_z = to_utc_z(start_dt), to_utc_z(end_dt)

    base = {
        "printer_ip": ip, "start": start_z, "end": end_z, "bucket_seconds": step,
        "timestamps": [], "temperature": [], "humidity": [], "summary": {},
    }
    try:
        conn = get_connection(timeout=30.0)
        try:
            sql = (
                "SELECT timestamp, kind, value FROM sensor_readings "
                "WHERE printer_ip = ? AND timestamp >= ? AND timestamp <= ? "
                "AND value IS NOT NULL AND kind IN ('temperature', 'humidity')"
            )
            params = [ip, start_z, end_z]
            if port is not None:
                sql += " AND port = ?"
                params.append(port)
            sql += " ORDER BY timestamp LIMIT ?"
            params.append(_SERIES_MAX_ROWS)
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
    except (UndefinedTable, UndefinedColumn, DatabaseError) as e:
        log.error(f"Sensor series API database error: {e}", exc_info=True)
        return jsonify(base)
    except Exception as e:
        log.error(f"Sensor series API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    buckets = {}
    skipped = 0
    for ts, kind, value in rows:
        epoch = _series_row_epoch(ts)
        if epoch is None:
            skipped += 1
            continue
        key = int(epoch // step) * step
        slot = buckets.setdefault(key, {"temperature": [0.0, 0], "humidity": [0.0, 0]})
        acc = slot[kind]
        acc[0] += _sql_numeric(value)
        acc[1] += 1
    if skipped:
        log.warning(f"stats/sensor/series: {skipped} ردیف با timestamp نامعتبر نادیده گرفته شد")

    stamps, temps, hums = [], [], []
    all_t, all_h = [], []
    for key in sorted(buckets):
        slot = buckets[key]
        stamps.append(to_utc_z(datetime.fromtimestamp(key, tz=timezone.utc)))
        t = round(slot["temperature"][0] / slot["temperature"][1], 2) if slot["temperature"][1] else None
        h = round(slot["humidity"][0] / slot["humidity"][1], 2) if slot["humidity"][1] else None
        temps.append(t)
        hums.append(h)
        if t is not None:
            all_t.append(t)
        if h is not None:
            all_h.append(h)

    def _sum(values):
        if not values:
            return None
        return {"min": min(values), "max": max(values), "avg": round(sum(values) / len(values), 2)}

    base.update({
        "timestamps": stamps, "temperature": temps, "humidity": hums,
        "summary": {"temperature": _sum(all_t), "humidity": _sum(all_h)},
    })
    return jsonify(base)


# ─── Print usage series (color / black-white split, free range, auto bucket) ──
_USAGE_MAX_DAYS = 366
_USAGE_MAX_ROWS = 500000
_USAGE_BUCKETS = ("hour", "day", "week")
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# Example message: "12 صفحه رنگی + 30 صفحه سیاه‌سفید = 42 صفحه چاپ شد" (12 color pages + 30 B/W pages = 42 printed).
# This message is the only place the mixed split is stored; the regex below parses it.
_MIXED_RE = re.compile(r"(\d+)\s*صفحه\s*رنگی\s*\+\s*(\d+)\s*صفحه\s*سیاه")


def _usage_split(color, pages, message):
    """
    Split one PRINT event into (color, black_white, unknown) pages.

    ``logs.color`` holds the labels رنگی (color) / سیاه‌سفید (black-and-white) /
    مختلط (mixed) / نامشخص (unknown); these are the literal values matched below.
    For a mixed event the exact split exists only in the message text; when it
    cannot be parsed, the pages are reported as *unknown* instead of being guessed.
    """
    pages = max(0, int(_sql_numeric(pages)))
    if pages == 0:
        return 0, 0, 0
    label = (str(color or "")).replace("\u200c", "").replace(" ", "").lower()
    if "مختلط" in label or "mixed" in label:
        match = _MIXED_RE.search(str(message or "").translate(_PERSIAN_DIGITS))
        if match:
            c, b = int(match.group(1)), int(match.group(2))
            if c + b <= pages:
                return c, b, pages - c - b
        return 0, 0, pages
    if "سیاه" in label or label in ("bw", "b/w", "mono", "black", "blackwhite", "grayscale"):
        return 0, pages, 0
    if "رنگ" in label or "color" in label or "colour" in label:
        return pages, 0, 0
    return 0, 0, pages


def _usage_pick_bucket(span_seconds: float, requested: str) -> str:
    if requested in _USAGE_BUCKETS:
        return requested
    days = span_seconds / 86400.0
    if days <= 3:
        return "hour"
    return "day" if days <= 120 else "week"


def _usage_bucket_start(dt_local, bucket: str):
    """Local datetime -> start of its bucket (hour / local day / Saturday-based week)."""
    if bucket == "hour":
        return dt_local.replace(minute=0, second=0, microsecond=0)
    day = dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":
        day -= timedelta(days=(day.weekday() + 2) % 7)   # Iran: week starts on Saturday
    return day


def _usage_label(dt_local, bucket: str) -> str:
    """Wall-clock label in Tehran time (no offset); the browser formats it as given."""
    return dt_local.strftime("%Y-%m-%dT%H:00") if bucket == "hour" else dt_local.strftime("%Y-%m-%d")


@bp.route('/api/stats/usage/series')
def api_usage_series():
    """
    Printed pages per time bucket, split into color / black-white / unknown.

    Query: ip (optional; all printers when omitted) ·
    range: ``days=N`` (N whole local days incl. today) | ``hours=N`` | ``start``/``end`` (ISO; naive = Tehran) ·
    bucket: auto | hour | day | week.
    """
    ip = (request.args.get('ip') or '').strip() or None
    restricted = (getattr(current_user, "role", None) != "admin"
                  and bool(user_allowed_offices(current_user)))
    if ip and restricted and not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
    allowed_ips = set(allowed_printer_ips(current_user)) if (restricted and not ip) else None

    tz = local_tz()
    now_utc = datetime.now(timezone.utc)
    try:
        if request.args.get('start'):
            start_dt = parse_utc(to_filter_bound(request.args['start']))
            end_dt = parse_utc(to_filter_bound(request.args['end'])) if request.args.get('end') else now_utc
        elif request.args.get('hours', type=float):
            end_dt = now_utc
            start_dt = end_dt - timedelta(hours=min(request.args.get('hours', type=float), _USAGE_MAX_DAYS * 24))
        else:
            days = max(1, min(request.args.get('days', default=30, type=int) or 30, _USAGE_MAX_DAYS))
            end_dt = now_utc
            first_day = datetime.now(tz).date() - timedelta(days=days - 1)
            start_dt = datetime.combine(first_day, time.min, tzinfo=tz)
    except ValueError:
        return jsonify({"error": "invalid start/end"}), 400

    if start_dt >= end_dt:
        return jsonify({"error": "start must be before end"}), 400
    if end_dt - start_dt > timedelta(days=_USAGE_MAX_DAYS):
        start_dt = end_dt - timedelta(days=_USAGE_MAX_DAYS)

    bucket = _usage_pick_bucket((end_dt - start_dt).total_seconds(), request.args.get('bucket', 'auto'))
    start_z, end_z = to_utc_z(start_dt), to_utc_z(end_dt)
    empty = {
        "printer_ip": ip, "start": start_z, "end": end_z, "bucket": bucket,
        "labels": [], "color": [], "bw": [], "unknown": [], "total": [], "summary": {},
    }

    try:
        conn = get_connection(timeout=30.0)
        try:
            sql = ("SELECT timestamp, pages, color, message, printer_ip FROM logs "
                   "WHERE type = 'PRINT' AND timestamp >= ? AND timestamp <= ? "
                   "AND pages IS NOT NULL AND pages > 0")
            params = [start_z, end_z]
            if ip:
                sql += " AND printer_ip = ?"
                params.append(ip)
            sql += " LIMIT ?"
            params.append(_USAGE_MAX_ROWS)
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
    except (UndefinedTable, UndefinedColumn, DatabaseError) as e:
        log.error(f"Usage series API database error: {e}", exc_info=True)
        return jsonify(empty)
    except Exception as e:
        log.error(f"Usage series API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    acc = {}
    skipped = 0
    for ts, pages, color, message, row_ip in rows:
        if allowed_ips is not None and row_ip not in allowed_ips:
            continue
        try:
            dt = ts if isinstance(ts, datetime) else parse_utc(str(ts))
            if dt.tzinfo is None:
                raise ValueError("naive")
            key = _usage_bucket_start(dt.astimezone(tz), bucket)
        except ValueError:
            skipped += 1
            continue
        c, b, u = _usage_split(color, pages, message)
        slot = acc.setdefault(key, [0, 0, 0])
        slot[0] += c
        slot[1] += b
        slot[2] += u
    if skipped:
        log.warning(f"stats/usage/series: {skipped} ردیف با timestamp نامعتبر نادیده گرفته شد")

    # Continuous axis (empty buckets are real zeros, not missing days)
    cursor = _usage_bucket_start(start_dt.astimezone(tz), bucket)
    last = _usage_bucket_start(end_dt.astimezone(tz), bucket)
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=7 if bucket == "week" else 1)
    labels, colors, bws, unknowns, totals = [], [], [], [], []
    while cursor <= last and len(labels) < 2000:
        c, b, u = acc.get(cursor, (0, 0, 0))
        labels.append(_usage_label(cursor, bucket))
        colors.append(c)
        bws.append(b)
        unknowns.append(u)
        totals.append(c + b + u)
        # wall-clock stepping keeps local midnight stable across DST changes
        cursor = (cursor.replace(tzinfo=None) + step).replace(tzinfo=tz)

    grand = sum(totals)
    peak_i = max(range(len(totals)), key=totals.__getitem__) if totals else None
    sum_c, sum_b, sum_u = sum(colors), sum(bws), sum(unknowns)
    base = {
        "printer_ip": ip, "start": start_z, "end": end_z, "bucket": bucket,
        "labels": labels, "color": colors, "bw": bws, "unknown": unknowns, "total": totals,
        "summary": {
            "total": grand, "color": sum_c, "bw": sum_b, "unknown": sum_u,
            "color_pct": round(sum_c * 100.0 / grand, 1) if grand else 0,
            "bw_pct": round(sum_b * 100.0 / grand, 1) if grand else 0,
            "avg_per_bucket": round(grand / len(totals), 1) if totals else 0,
            "peak": {"label": labels[peak_i], "value": totals[peak_i]} if peak_i is not None and totals[peak_i] else None,
        },
    }
    return jsonify(base)
