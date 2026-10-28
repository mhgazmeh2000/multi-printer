# =============================================================================
# @file:         core/collectors/sensor.py
# @brief:        Collects readings from the ECS100G environmental sensor over SNMP v1.
# @details:      Responsibilities: Build sensor OID sets and convert sensor values for storage.
#                Main API: sensor_oids(), collect_sensor()
#                Side effects: network I/O
# @dependencies: internal: core, core.database, core.sensor_alert, core.snmp.protocol,
#                core.time_utils
# @notes:        Reads environment variables: SENSOR_HUM_CHANGE_LOG_DELTA,
#                SENSOR_HUM_CRITICAL_HIGH, SENSOR_HUM_MAX, SENSOR_HUM_MIN,
#                SENSOR_HUM_WARNING_HIGH, SENSOR_HUM_WARNING_LOW, SENSOR_PORT_COUNT,
#                SENSOR_TEMP_CHANGE_LOG_DELTA.
# =============================================================================

"""Data collection from ECS100G sensor with SNMP v1.

Features:
- Multi-port temperature/humidity detection and reading
- Temperature/humidity range validation
- Register SENSOR_CHANGE log for every temperature/humidity change of each port
- Save snapshot in sensor_readings table for daily average chart
"""

from __future__ import annotations

import logging
import os
import time

from core.snmp.protocol import snmp_get
from core import store
from core.database import add_event, record_sensor_readings
from core.sensor_alert import check_and_send_sensor_alert
from core.time_utils import utc_now_iso

log = logging.getLogger("PrinterMonitor")

# Real ECS100G OIDs (extracted from snmpwalk)
SENSOR_BASE_OIDS = {
    "model":  "1.3.6.1.4.1.47206.1.0",
    "serial": "1.3.6.1.4.1.47206.2.0",
    "uptime": "1.3.6.1.2.1.1.3.0",
}

# Most ECS100Gs have two ports, but it can be configured for future expansion.
SENSOR_PORT_COUNT = max(1, min(8, int(os.getenv("SENSOR_PORT_COUNT", "2") or "2")))

# logical limits; Outside these ranges, it is displayed as invalid.
TEMP_MIN_C = float(os.getenv("SENSOR_TEMP_MIN_C", "-40"))
TEMP_MAX_C = float(os.getenv("SENSOR_TEMP_MAX_C", "125"))
HUM_MIN = float(os.getenv("SENSOR_HUM_MIN", "0"))
HUM_MAX = float(os.getenv("SENSOR_HUM_MAX", "100"))

# Operational alert threshold; It only makes active alert in UI, individual changes are logged with SENSOR_CHANGE.
TEMP_WARNING_C = float(os.getenv("SENSOR_TEMP_WARNING_C", "30"))
TEMP_CRITICAL_C = float(os.getenv("SENSOR_TEMP_CRITICAL_C", "35"))
HUM_WARNING_HIGH = float(os.getenv("SENSOR_HUM_WARNING_HIGH", "70"))
HUM_CRITICAL_HIGH = float(os.getenv("SENSOR_HUM_CRITICAL_HIGH", "80"))
HUM_WARNING_LOW = float(os.getenv("SENSOR_HUM_WARNING_LOW", "20"))

# Recording the sensor change log only when the change is significant.
# Operational request: temperature at least 1 degree, humidity at least 5%.
SENSOR_TEMP_CHANGE_LOG_DELTA = float(os.getenv("SENSOR_TEMP_CHANGE_LOG_DELTA", "1"))
SENSOR_HUM_CHANGE_LOG_DELTA = float(os.getenv("SENSOR_HUM_CHANGE_LOG_DELTA", "5"))


def _port_oids(port: int) -> dict:
    """Return the SNMP OIDs of the temperature and humidity readings for one sensor port."""
    return {
        f"temp{port}": f"1.3.6.1.4.1.47206.110.{port}.2.0",
        f"temp{port}_status": f"1.3.6.1.4.1.47206.110.{port}.1.0",
        f"hum{port}": f"1.3.6.1.4.1.47206.111.{port}.2.0",
        f"hum{port}_status": f"1.3.6.1.4.1.47206.111.{port}.1.0",
    }


def sensor_oids() -> dict:
    """Return the OID map used to read the ECS100G sensor."""

    oids = dict(SENSOR_BASE_OIDS)
    for port in range(1, SENSOR_PORT_COUNT + 1):
        oids.update(_port_oids(port))
    return oids


SENSOR_OIDS = sensor_oids()


def _clean_text(value, default: str) -> str:
    """Return the stripped text, or the default when it is empty or a placeholder such as 'N/A'."""
    text = str(value or "").strip()
    if not text or text.lower() in ("none", "n/a", "null"):
        return default
    return text


def _to_int(value):
    """Convert a sensor value to int, returning None on failure."""
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_divide_10(value, *, kind: str):
    """Secure SNMP value conversion to real value and scope validation."""
    raw = _to_int(value)
    if raw is None:
        return None, "no_data"
    val = round(raw / 10.0, 1)
    if kind == "temperature":
        if not (TEMP_MIN_C <= val <= TEMP_MAX_C):
            return None, "invalid"
    elif kind == "humidity":
        if not (HUM_MIN <= val <= HUM_MAX):
            return None, "invalid"
    return val, "ok"


def _status(raw_status, value, validation_status: str) -> str:
    """Compute the reading status from the raw status, the value and the validation status."""
    if validation_status == "invalid":
        return "invalid"
    status_int = _to_int(raw_status)
    if status_int == 1:
        return "active"
    if status_int == 0 and value is None:
        return "inactive"
    # Some devices do not correct the status but send a valid value.
    if value is not None:
        return "active"
    return "inactive"


def _severity_for_reading(kind: str, value):
    """Return the alert severity for a sensor reading, or None when no alert applies."""
    if value is None:
        return None
    if kind == "temperature":
        if value >= TEMP_CRITICAL_C:
            return "critical"
        if value >= TEMP_WARNING_C:
            return "warning"
    if kind == "humidity":
        if value >= HUM_CRITICAL_HIGH:
            return "critical"
        if value >= HUM_WARNING_HIGH or value <= HUM_WARNING_LOW:
            return "warning"
    return None


def _register_sensor_changes(ip: str, previous: dict, readings: list):
    """Compare the sensor readings with the previous poll and record any changes."""
    prev_counters = (previous or {}).get("counters") or {}
    for r in readings:
        key = f"{'temp' if r['kind'] == 'temperature' else 'hum'}{r['port']}"
        old = prev_counters.get(key)
        new = r.get("value")
        if old is None or new is None:
            continue
        try:
            old_f = round(float(old), 1)
            new_f = round(float(new), 1)
        except (TypeError, ValueError):
            continue
        delta = round(abs(new_f - old_f), 1)
        threshold = SENSOR_TEMP_CHANGE_LOG_DELTA if r["kind"] == "temperature" else SENSOR_HUM_CHANGE_LOG_DELTA
        if delta < threshold:
            continue
        label = "دما" if r["kind"] == "temperature" else "رطوبت"
        unit = r.get("unit") or ("°C" if r["kind"] == "temperature" else "%")
        add_event(ip, "SENSOR_CHANGE", {
            "message": f"تغییر {label} سنسور پورت {r['port']}: {old_f}{unit} → {new_f}{unit} (Δ={delta}{unit})",
            "severity": "info",
            "code": f"sensor:{r['kind']}:port{r['port']}",
            "sensor_kind": r["kind"],
            "sensor_port": r["port"],
            "prev_value": old_f,
            "new_value": new_f,
            "delta": delta,
            "threshold": threshold,
            "unit": unit,
        })


def collect_sensor(ip: str, name: str, community: str, start: float) -> dict:
    """Collect readings from an ECS100G environmental sensor over SNMP v1."""

    try:
        def g(oid_key):
            """Read OID with SNMPv1 and general timeout."""
            if time.time() - start > 10.0:
                return None
            oid = SENSOR_OIDS.get(oid_key)
            if not oid:
                return None
            return snmp_get(ip, oid, community, timeout=2.0, version=1)

        values = {key: g(key) for key in SENSOR_OIDS}

        # Sensor may not give sysDescr; If there is a model or any sensor value, it is online.
        has_any_sensor_value = any(
            values.get(f"temp{port}") is not None or values.get(f"hum{port}") is not None
            for port in range(1, SENSOR_PORT_COUNT + 1)
        )
        if values.get("model") is None and not has_any_sensor_value:
            elapsed = int((time.time() - start) * 1000)
            return {
                "ip": ip, "name": name, "brand": "sensor", "device_type": "sensor",
                "online": False,
                "last_poll": utc_now_iso(),
                "poll_ms": elapsed,
                "error": "No SNMP response",
                "device": {"model": "ECS100G", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
                "counters": {}, "paper_sizes": {}, "trays": [], "toners": {}, "alerts": [],
            }

        model = _clean_text(values.get("model"), "ECS100G")
        serial = _clean_text(values.get("serial"), "N/A")

        ut = _to_int(values.get("uptime")) or 0
        us = ut // 100
        uptime_str = f"{us//86400}d {(us%86400)//3600:02d}:{(us%3600)//60:02d}" if ut else "N/A"

        counters = {}
        readings = []
        temp_ports = []
        hum_ports = []
        alerts = []

        for port in range(1, SENSOR_PORT_COUNT + 1):
            temp, temp_validation = _safe_divide_10(values.get(f"temp{port}"), kind="temperature")
            hum, hum_validation = _safe_divide_10(values.get(f"hum{port}"), kind="humidity")
            temp_status = _status(values.get(f"temp{port}_status"), temp, temp_validation)
            hum_status = _status(values.get(f"hum{port}_status"), hum, hum_validation)

            counters[f"temp{port}"] = temp
            counters[f"temp{port}_status"] = temp_status
            counters[f"hum{port}"] = hum
            counters[f"hum{port}_status"] = hum_status

            if temp is not None or values.get(f"temp{port}") is not None:
                temp_ports.append({"port": port, "value": temp, "unit": "°C", "status": temp_status})
            if hum is not None or values.get(f"hum{port}") is not None:
                hum_ports.append({"port": port, "value": hum, "unit": "%", "status": hum_status})

            if temp is not None:
                readings.append({"port": port, "kind": "temperature", "value": temp, "unit": "°C", "status": temp_status})
            if hum is not None:
                readings.append({"port": port, "kind": "humidity", "value": hum, "unit": "%", "status": hum_status})

            if temp_validation == "invalid":
                alerts.append({"message": f"دمای پورت {port} خارج از محدوده معتبر است", "code": f"sensor:temperature:port{port}:invalid"})
            if hum_validation == "invalid":
                alerts.append({"message": f"رطوبت پورت {port} خارج از محدوده معتبر است", "code": f"sensor:humidity:port{port}:invalid"})

            temp_sev = _severity_for_reading("temperature", temp)
            if temp_sev:
                alerts.append({"message": f"دمای پورت {port}: {temp}°C", "code": f"sensor:temperature:port{port}:{temp_sev}"})
            hum_sev = _severity_for_reading("humidity", hum)
            if hum_sev:
                alerts.append({"message": f"رطوبت پورت {port}: {hum}%", "code": f"sensor:humidity:port{port}:{hum_sev}"})

        counters["temp_ports"] = temp_ports
        counters["hum_ports"] = hum_ports

        timestamp = utc_now_iso()
        with store.data_lock:
            previous = store.printer_data.get(ip, {}) or {}
        _register_sensor_changes(ip, previous, readings)
        record_sensor_readings(ip, readings, timestamp=timestamp)

        # Send email alerts to users
        try:
            check_and_send_sensor_alert(ip, name, readings)
        except Exception as e:
            log.exception(f"Sensor alert email error for {ip}: {e}")

        elapsed = int((time.time() - start) * 1000)
        log.info("  ✓ %s [sensor] readings=%s %sms", name, readings, elapsed)

        return {
            "ip": ip, "name": name, "brand": "sensor", "device_type": "sensor",
            "online": True,
            "last_poll": timestamp,
            "poll_ms": elapsed,
            "device": {
                "model": model,
                "serial": serial,
                "firmware": model,
                "uptime_str": uptime_str,
            },
            "counters": counters,
            "paper_sizes": {},
            "trays": [],
            "toners": {},
            "alerts": alerts,
            "sensor_readings": readings,
        }
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        log.exception("  ✗ %s [sensor] error: %s", name, e)
        return {
            "ip": ip, "name": name, "brand": "sensor", "device_type": "sensor",
            "online": False,
            "last_poll": utc_now_iso(),
            "poll_ms": elapsed,
            "device": {"model": "ECS100G", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
            "counters": {},
            "paper_sizes": {}, "trays": [], "toners": {},
            "alerts": [{"message": f"Collection error: {e}", "code": 9999}],
        }
