import logging
from datetime import datetime, time, timedelta
from flask import Blueprint, jsonify, request
from core.db import DatabaseError, UndefinedColumn, UndefinedTable, get_connection
from core import store
from core.time_utils import local_tz, to_local_day, to_utc_z

bp = Blueprint("stats", __name__)
log = logging.getLogger("PrinterMonitor")


# ─── کمکی‌های فاز E.3 ────────────────────────────────────────────────────────
# تا پیش از E.3 داده‌ی ذخیره‌شده naive محلی بود و گروه‌بندی روزانه با
# `substr(timestamp, 1, 10)` در SQL انجام می‌شد. حالا داده UTC/Z است ⇒ «روز» باید
# صریحاً روزِ محلی (Asia/Tehran) باشد، وگرنه هر ردیف بعد از ۲۰:۳۰ به وقت تهران به
# سبد روز بعد می‌افتد. گروه‌بندی در پایتون انجام می‌شود تا SQL در هر دو backend
# (SQLite امروز، PostgreSQL در E.4) یکسان بماند.

def _local_day_start(days_ago: int) -> str:
    """نیمه‌شبِ محلیِ «امروز منهای days_ago» به فرمت استاندارد Z (مرز فیلتر SQL)."""
    day = datetime.now(local_tz()).date() - timedelta(days=days_ago)
    return to_utc_z(datetime.combine(day, time.min, tzinfo=local_tz()))


def _sql_numeric(raw) -> float:
    """تبدیل ضمنی SQLite در SUM/AVG: متن غیرعددی ⇒ ۰ (بدون استثنا)."""
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _json_number(value: float):
    """جمعِ صحیح باید مثل SQLite عدد صحیح برگردد (۳ نه ۳.۰) تا پاسخ API تغییر نکند."""
    return int(value) if float(value).is_integer() else value


@bp.route('/api/stats/daily')
def api_daily_stats():
    ip = request.args.get('ip')
    days = min(request.args.get('days', default=30, type=int), 365)
    start_utc = _local_day_start(days)

    try:
        # فاز D: اتصال از لایه‌ی adapter (core.db)؛ همان timeout قبلی.
        conn = get_connection(timeout=30.0)

        # فاز E.3: SELECT ردیف‌ها (بدون GROUP BY) و گروه‌بندی روزِ محلی در پایتون.
        # فاز E.4.6: cursor خام حذف شد ⇒ ``conn.execute`` (روی PG ترجمه‌ی ? ⇒ %s هم
        # از همین مسیر می‌آید و SQL دو-سبکی لازم نیست).
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
    """میانگین روزانه دما و رطوبت سنسور از جدول sensor_readings."""
    ip = request.args.get('ip')
    days = min(request.args.get('days', default=30, type=int), 365)
    start_utc = _local_day_start(days)
    if not ip:
        return jsonify({"error": "ip required"}), 400

    try:
        # فاز D: اتصال از لایه‌ی adapter (core.db)؛ همان timeout قبلی.
        conn = get_connection(timeout=30.0)
        # فاز E.3: AVG و GROUP BY روزانه در پایتون (روزِ محلی) — همان دلیل endpoint بالا.
        # فاز E.4.6: cursor خام حذف شد ⇒ ``conn.execute``.
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

        # اگر هنوز جدول sensor_readings داده کافی ندارد، از snapshot زنده سنسور برای امروز fallback بگیر.
        # این باعث می‌شود نمودار بلافاصله بعد از اولین poll هم چیزی برای نمایش داشته باشد.
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
    # فاز E.4.4: خطاها از adapter با نوع مشترک می‌آیند (نه sqlite3.*). معادل
    # دقیق دو مهار قبلی: نبود جدول/ستون در DB قدیمی + خطای عمومی دیتابیس.
    except (UndefinedTable, UndefinedColumn) as e:
        # اگر DB قدیمی هنوز init نشده باشد.
        log.error(f"Sensor stats API DB error: {e}", exc_info=True)
        return jsonify({"dates": [], "avg_temperature": [], "avg_humidity": [], "printer_ip": ip, "days": days})
    except DatabaseError as e:
        # خطاهای ساختاری دیتابیس نباید نمودار کل داشبورد را با HTTP 500 متوقف کنند.
        log.error(f"Sensor stats API database error: {e}", exc_info=True)
        return jsonify({"dates": [], "avg_temperature": [], "avg_humidity": [], "printer_ip": ip, "days": days})
    except Exception as e:
        log.error(f"Sensor stats API error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500