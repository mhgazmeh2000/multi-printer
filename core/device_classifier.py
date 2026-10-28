# =============================================================================
# @file:         core/device_classifier.py
# @brief:        Classifies a printer as colour or mono from its supplies table.
# @details:      Responsibilities: Walk prtMarkerSupplies, decide the device type and log
#                validation errors.
#                Main API: classify_from_scan()
#                Side effects: network I/O, file I/O
# @dependencies: internal: config.settings, core.snmp.protocol
# @notes:        None.
# =============================================================================

"""
Determining the device type by walking on the consumables table (prtMarkerSuppliesTable)
By logging the errors in oid_validation_errors.txt
"""

import logging
from datetime import datetime

from config.settings import VALIDATION_LOG_FILE
from core.snmp.protocol import snmp_get_with_fallback

log = logging.getLogger("PrinterMonitor")


def _log_error(ip: str, error_type: str, details: str):
    """Record errors in the validation log file"""
    try:
        timestamp = datetime.now().isoformat()
        with open(VALIDATION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] IP: {ip} | Type: device_classifier_{error_type}\n")
            f.write(f"  Details: {details}\n\n")
    except Exception as e:
        log.exception("خطا در نوشتن device_classifier log: %s", e)


def _walk_supplies(ip, community, max_index=10, timeout=2.0):
    """
    Reading the table prtMarkerSuppliesTable
    Returns: a list of dictionaries {"name":..., "type":..., "max":..., "remaining":..., "percent":...}
    """
    supplies = []
    for idx in range(1, max_index + 1):
        try:
            name = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}", community, timeout=timeout)
            if name is None:
                # Two consecutive None = end of table
                if idx > 1:
                    prev_name = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.6.1.{idx-1}", community, timeout=timeout)
                    if prev_name is None:
                        break
                continue

            stype = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.5.1.{idx}", community, timeout=timeout)
            max_val = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}", community, timeout=timeout)
            remaining = snmp_get_with_fallback(ip, f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}", community, timeout=timeout)

            name_str = str(name).strip() if name else f"Supply {idx}"

            # Safe conversion to int
            try:
                stype_int = int(stype) if stype is not None else 0
            except (ValueError, TypeError):
                stype_int = 0
                _log_error(ip, "type_conversion", f"Supply {idx}: type={stype} قابل تبدیل به int نیست")

            try:
                max_int = int(max_val) if max_val is not None and str(max_val).lstrip('-').isdigit() else -2
            except (ValueError, TypeError):
                max_int = -2
                _log_error(ip, "max_conversion", f"Supply {idx}: max={max_val} قابل تبدیل به int نیست")

            try:
                rem_int = int(remaining) if remaining is not None and str(remaining).lstrip('-').isdigit() else -2
            except (ValueError, TypeError):
                rem_int = -2
                _log_error(ip, "remaining_conversion", f"Supply {idx}: remaining={remaining} قابل تبدیل به int نیست")

            # Calculate the percentage
            percent = None
            if max_int > 0 and rem_int >= 0:
                percent = round(rem_int / max_int * 100)

            supplies.append({
                "index": idx,
                "name": name_str,
                "type": stype_int,
                "max": max_int,
                "remaining": rem_int,
                "percent": percent
            })

        except Exception as e:
            _log_error(ip, "walk_exception", f"Supply {idx}: {e}")
            continue

    return supplies


# ─── Main function called in scanner.py ───
def classify_from_scan(active_oids: dict, current_vals: dict, brand: str) -> str:
    """
    Device type detection using scanned OIDs (fast method, no additional SNMP)
    """
    # 1. Sensor
    if brand == "sensor":
        return "sensor"

    sys_desc = current_vals.get("sys_descr")
    if sys_desc and isinstance(sys_desc, str) and "ECS100G" in sys_desc.upper():
        return "sensor"
    if any(key in active_oids for key in ("sensor_model", "sensor_temp1", "sensor_hum1")):
        return "sensor"

    # 2. Toshiba - check the value of toner status OIDs
    if brand == "toshiba":
        cyan_st = current_vals.get("tsh_cyan_st")
        mag_st  = current_vals.get("tsh_magenta_st")
        yel_st  = current_vals.get("tsh_yellow_st")

        for val in (cyan_st, mag_st, yel_st):
            if val is not None:
                try:
                    if int(val) > 0:
                        return "color"
                except (ValueError, TypeError):
                    pass

        if "tsh_color" in active_oids:
            try:
                color_val = int(current_vals.get("tsh_color", 0))
                if color_val > 0:
                    return "color"
            except (ValueError, TypeError):
                pass

        return "mono"

    # 3. Canon
    if brand == "canon":
        if "toner_name_2" in active_oids:
            t2_name = str(current_vals.get("toner_name_2", "")).lower()
            if any(c in t2_name for c in ("cyan", "magenta", "yellow")):
                return "color"
            else:
                return "mono"
        if "canon_lbp_color" in active_oids:
            return "color"
        return "mono"

    # 4. HP
    if brand == "hp":
        if "toner_name_2" in active_oids:
            t2_name = str(current_vals.get("toner_name_2", "")).lower()
            if any(c in t2_name for c in ("cyan", "magenta", "yellow")):
                return "color"
            if any(c in t2_name for c in ("drum", "feeder", "kit", "fuser")):
                if "toner_name_3" in active_oids:
                    t3_name = str(current_vals.get("toner_name_3", "")).lower()
                    if any(c in t3_name for c in ("cyan", "magenta", "yellow")):
                        return "color"
                return "mono"
        if any(k in active_oids for k in ("hp_print_color", "hp_copy_color")):
            return "color"
        return "mono"

    # 5. Brother
    if brand == "brother":
        if "toner_name_2" in active_oids:
            t2_name = str(current_vals.get("toner_name_2", "")).lower()
            if any(c in t2_name for c in ("cyan", "magenta", "yellow")):
                return "color"
            else:
                return "mono"
        if "bro_color" in active_oids:
            try:
                color_val = int(current_vals.get("bro_color", 0))
                if color_val > 0:
                    return "color"
            except (ValueError, TypeError):
                pass
        return "mono"

    return "mono"