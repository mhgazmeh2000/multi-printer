"""
PrintGuard Alert Engine Service (Phase J.11)
Unified alert aggregator and analyzer for dashboard alerts,
printer diagnostic notifications, sensor thresholds, and security anomalies.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from core.db.connection import connect

log = logging.getLogger("printguard.alerting")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_iso(dt: datetime) -> str:
    return dt.isoformat()


def _resolve_suggested_action(category: str, code: Optional[str], message: str) -> str:
    """Provide contextual Persian resolution guidance based on alert characteristics."""
    msg_lower = (message or "").lower()
    code_str = str(code or "").lower()

    if "toner" in category or "toner" in msg_lower or "کارتریج" in msg_lower or "تونر" in msg_lower:
        return "کارتریج تونر رو به اتمام است. کارتریج یدک را آماده کرده و در صورت نیاز تعویض فرمایید."
    if "offline" in msg_lower or "آفلاین" in msg_lower or "قطع" in msg_lower:
        return "کابل شبکه، تغذیه برق پرینتر و آدرس IP دستگاه را در شبکه بررسی فرمایید."
    if "door" in msg_lower or "cover" in msg_lower or "درب" in msg_lower:
        return "درب یا پوشش محافظ پرینتر باز است. لطفاً پوشش دستگاه را محکم ببندید."
    if "jam" in msg_lower or "گیر" in msg_lower:
        return "کاغذ در سینی یا مسیر خروجی گیر کرده است. مسیر کاغذ را تخلیه و غلتک‌ها را چک کنید."
    if "sensor" in category or "دما" in msg_lower or "رطوبت" in msg_lower:
        return "دمای محیط یا رطوبت فراتر از آستانه امن است. سیستم تهویه و سرمایش اتاق سرور را بررسی کنید."
    if "security" in category or "امنیت" in msg_lower or "رمز" in msg_lower or "login" in msg_lower:
        return "فعالیت نامتعارف امنیتی ثبت شد. لاگ ورود و آدرس IP کاربر را در بخش امنیت بررسی فرمایید."
    return "دستگاه نیاز به بازبینی اپراتور دارد. وضعیت فیزیکی و لاگ‌های خطا را بررسی کنید."


def get_active_alerts(
    severity: Optional[str] = None,
    printer_ip: Optional[str] = None,
    limit: int = 50,
    since_hours: int = 72,
) -> List[Dict[str, Any]]:
    """
    Fetch and aggregate active and recent alerts from logs, printer counters,
    toner snapshots, sensor thresholds, and security events.
    """
    alerts: List[Dict[str, Any]] = []
    cutoff = _utc_now() - timedelta(hours=max(1, since_hours))
    cutoff_iso = cutoff.isoformat()

    try:
        with connect() as conn:
            # 1. Fetch from `logs` table (errors, warnings, status alerts)
            sql_logs = """
                SELECT id, printer_ip, printer_name, timestamp, type, message, severity, code, details
                FROM logs
                WHERE (severity IN ('warning', 'error', 'critical') OR type IN ('ALERT', 'STATUS'))
                  AND timestamp >= ?
            """
            params_logs: List[Any] = [cutoff_iso]
            if printer_ip:
                sql_logs += " AND printer_ip = ?"
                params_logs.append(printer_ip)
            sql_logs += " ORDER BY timestamp DESC LIMIT ?"
            params_logs.append(limit)

            try:
                for row in conn.execute(sql_logs, params_logs).fetchall():
                    sev_raw = (row[6] or "warning").lower()
                    sev = "critical" if sev_raw in ("error", "critical") else "warning" if sev_raw == "warning" else "info"
                    cat = "printer"
                    if row[4] == "STATUS":
                        cat = "connectivity"
                    elif row[4] == "ALERT":
                        cat = "hardware"

                    alerts.append({
                        "id": f"log-{row[0]}",
                        "title": row[5] or "رویداد دستگاه",
                        "severity": sev,
                        "category": cat,
                        "printer_ip": row[1],
                        "printer_name": row[2] or row[1],
                        "code": row[7] or "SYS_EVENT",
                        "message": row[5] or "",
                        "details": row[8] or {},
                        "timestamp": row[3],
                        "suggested_action": _resolve_suggested_action(cat, row[7], row[5]),
                    })
            except Exception as e:
                log.debug("Logs alert fetch warning: %s", e)

            # 2. Fetch from `security_events` table
            try:
                sql_sec = """
                    SELECT id, event_type, ip_address, user_identifier, severity, details, created_at
                    FROM security_events
                    WHERE created_at >= ?
                """
                params_sec: List[Any] = [cutoff_iso]
                if printer_ip:
                    sql_sec += " AND ip_address = ?"
                    params_sec.append(printer_ip)
                if severity and severity != "all":
                    sql_sec += " AND severity = ?"
                    params_sec.append(severity)
                sql_sec += " ORDER BY created_at DESC LIMIT ?"
                params_sec.append(limit)

                for row in conn.execute(sql_sec, params_sec).fetchall():
                    sev = (row[4] or "warning").lower()
                    alerts.append({
                        "id": f"sec-{row[0]}",
                        "title": f"رویداد امنیتی: {row[1]}",
                        "severity": sev if sev in ("critical", "warning", "info") else "warning",
                        "category": "security",
                        "printer_ip": row[2] or "127.0.0.1",
                        "printer_name": f"Security Watch ({row[3] or 'Guest'})",
                        "code": row[1],
                        "message": f"رویداد امنیتی {row[1]} از مبدا {row[2]}",
                        "details": row[5],
                        "timestamp": row[6],
                        "suggested_action": _resolve_suggested_action("security", row[1], row[1]),
                    })
            except Exception as e:
                log.debug("Security events alert fetch warning: %s", e)

            # 3. Check for low toner warnings from `toner_snapshots_v2` or `printer_counters`
            try:
                sql_toner = """
                    SELECT printer_ip, color, level_percent, recorded_at
                    FROM toner_snapshots_v2
                    WHERE level_percent <= 15
                      AND recorded_at >= ?
                """
                params_toner: List[Any] = [cutoff_iso]
                if printer_ip:
                    sql_toner += " AND printer_ip = ?"
                    params_toner.append(printer_ip)
                sql_toner += " ORDER BY recorded_at DESC LIMIT ?"
                params_toner.append(limit)

                for row in conn.execute(sql_toner, params_toner).fetchall():
                    pct = row[2]
                    sev = "critical" if pct <= 5 else "warning"
                    alerts.append({
                        "id": f"toner-{row[0]}-{row[1]}",
                        "title": f"کاهش سطح تونر {row[1]} ({pct}%)",
                        "severity": sev,
                        "category": "toner",
                        "printer_ip": row[0],
                        "printer_name": row[0],
                        "code": f"TONER_LOW_{str(row[1]).upper()}",
                        "message": f"میزان کارتریج رنگ {row[1]} به {pct}% رسیده است.",
                        "details": {"color": row[1], "level_percent": pct},
                        "timestamp": row[3],
                        "suggested_action": _resolve_suggested_action("toner", "TONER_LOW", str(row[1])),
                    })
            except Exception as e:
                log.debug("Toner snapshots alert fetch warning: %s", e)

            # 4. Check for sensor threshold violations
            try:
                sql_sensor = """
                    SELECT ip, name, temperature, humidity, timestamp
                    FROM sensor_readings
                    WHERE (temperature > 32 OR temperature < 15 OR humidity > 70 OR humidity < 25)
                      AND timestamp >= ?
                """
                params_sensor: List[Any] = [cutoff_iso]
                if printer_ip:
                    sql_sensor += " AND ip = ?"
                    params_sensor.append(printer_ip)
                sql_sensor += " ORDER BY timestamp DESC LIMIT ?"
                params_sensor.append(limit)

                for row in conn.execute(sql_sensor, params_sensor).fetchall():
                    temp = row[2]
                    hum = row[3]
                    sev = "critical" if (temp and temp > 36) or (hum and hum > 80) else "warning"
                    alerts.append({
                        "id": f"sensor-{row[0]}",
                        "title": f"هشدار سنسور محیطی {row[1] or row[0]}",
                        "severity": sev,
                        "category": "sensor",
                        "printer_ip": row[0],
                        "printer_name": row[1] or row[0],
                        "code": "SENSOR_THRESHOLD_EXCEEDED",
                        "message": f"دما: {temp}°C، رطوبت: {hum}% خارج از محدوده مطلوب",
                        "details": {"temperature": temp, "humidity": hum},
                        "timestamp": row[4],
                        "suggested_action": _resolve_suggested_action("sensor", "ENV_HIGH", "دما و رطوبت"),
                    })
            except Exception as e:
                log.debug("Sensor alerts fetch warning: %s", e)

    except Exception as exc:
        log.warning("Database alert aggregation error: %s", exc)

    # Sort descending by timestamp
    alerts.sort(key=lambda a: a.get("timestamp") or "", reverse=True)

    # Apply severity filter if requested
    if severity and severity != "all":
        alerts = [a for a in alerts if a.get("severity") == severity]

    return alerts[:limit]


def get_alert_summary(since_hours: int = 72) -> Dict[str, Any]:
    """Return counts of active alerts grouped by severity and category."""
    alerts = get_active_alerts(limit=500, since_hours=since_hours)
    critical_cnt = sum(1 for a in alerts if a.get("severity") == "critical")
    warning_cnt = sum(1 for a in alerts if a.get("severity") == "warning")
    info_cnt = sum(1 for a in alerts if a.get("severity") == "info")

    by_cat: Dict[str, int] = {}
    for a in alerts:
        cat = a.get("category", "other")
        by_cat[cat] = by_cat.get(cat, 0) + 1

    return {
        "total": len(alerts),
        "critical": critical_cnt,
        "warning": warning_cnt,
        "info": info_cnt,
        "by_category": by_cat,
        "last_checked": _format_iso(_utc_now()),
    }


def get_alert_by_id(alert_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve full details for a specific alert item."""
    alerts = get_active_alerts(limit=500, since_hours=168)  # 7 days lookup
    for a in alerts:
        if a.get("id") == alert_id:
            return a
    return None
