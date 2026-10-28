# =============================================================================
# @file:         web/routes/stats.py
# @brief:        API routes for daily statistics, including sensor statistics.
# @details:      Responsibilities: Aggregate daily counters for the dashboard charts.
#                Main API: api_daily_stats(), api_sensor_daily_stats()
#                Side effects: database access, defines HTTP routes
# @dependencies: internal: core, core.db, core.time_utils | external: flask
# @notes:        None.
# =============================================================================

"""Statistics API routes."""

import logging
from datetime import datetime, time, timedelta

from flask import Blueprint, jsonify, request

from core.db import DatabaseError, UndefinedColumn, UndefinedTable, get_connection
from core import store
from core.time_utils import local_tz, to_local_day, to_utc_z

bp = Blueprint("stats", __name__)
log = logging.getLogger("PrinterMonitor")


# ─── Phase E.3 Auxiliaries
# Until E.3, the stored data was local naive and daily grouping with
# `substr(timestamp, 1, 10)` was done in SQL. Now the data is UTC/Z ⇒ "day" should be
# It must be a local day (Asia/Tehran), otherwise every row after 20:30 Tehran time
# The basket drops the next day. Grouping is done in Python rather than SQL on both backends
# (SQLite today, PostgreSQL in E.4) remain the same.

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
        # Phase E.4.6: The raw cursor was removed ⇒ ``conn.execute'' (on PG translation of? ⇒ %s too
        # It comes from the same path and two-style SQL is not required).
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