# =============================================================================
# @file:         core/enhanced_collector.py
# @brief:        Advanced collector that walks supply and tray tables with alternative OIDs.
# @details:      Responsibilities: Detect SNMP version and printer type, and walk supply and
#                input-tray tables.
#                Main API: detect_snmp_version(), try_alternative_oids(), walk_supplies_table(),
#                walk_input_trays(), detect_printer_type_from_supplies(), collect_enhanced()
#                Side effects: network I/O, file I/O
# @dependencies: internal: config.settings, core, core.collectors.base, core.collectors.brother,
#                core.collectors.canon, core.collectors.cartridge_discovery,
#                core.collectors.cartridge_id, core.collectors.hp, core.collectors.sensor,
#                core.collectors.toshiba, core.database, core.oid.scanner, core.snmp.oid_map,
#                core.snmp.protocol, core.time_utils, core.yield_engine
# @notes:        None.
# =============================================================================

# core/enhanced_collector.py

"""
Advanced data collection using test_toner.py methods:
- Walk full table prtMarkerSuppliesTable
- Walk table prtInputTable (trays)
- Alternate OIDs for HP, Canon, Brother
- Automatic detection of SNMP version
- Storing detailed toner information in the database
- Record log in toner_report.txt
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config.settings import VALIDATION_LOG_FILE, TONER_ALERT_THRESHOLDS
from core.snmp.protocol import snmp_get_with_fallback, _detect_snmp_version
from core.snmp.oid_map import OIDS
from core import store
from core.collectors.base import _counters_event, apply_toner_override, _bootstrap_yield_from_history, get_pages_since_last_reset, attribute_paper_size
from core.database import add_event
from core.time_utils import to_utc_z, utc_now_iso

log = logging.getLogger("PrinterMonitor")


# Settings
ENHANCED_TIMEOUT = 3.0   # timeout for each OID
ENHANCED_MAX_SUPPLIES = 15  # The maximum number of consumables for walk
DEFAULT_CARTRIDGE_YIELD = 2000  # Approximate pages for toner estimation when sensor is not available

# Alternate OIDs for HP
# Note: the name of the cartridge on the device is exactly these strings (for example, "Black Cartridge CF287X");
# Being only CF287A on the map meant that the X/Y models — and the entire E52645 (CF289*) — would never match.
_HP_NPCL_PCT_OID = "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1"
HP_ALTERNATE_OIDS = {
    "CE505A": [_HP_NPCL_PCT_OID],
    "CF283A": [_HP_NPCL_PCT_OID],
    "CC388A": [_HP_NPCL_PCT_OID],
    # M506 / M527 (87A/87X/87Y)
    "CF287A": [_HP_NPCL_PCT_OID],
    "CF287X": [_HP_NPCL_PCT_OID],
    "CF287Y": [_HP_NPCL_PCT_OID],
    # E52645 / Managed E52645 (89A/89X/89Y)
    "CF289A": [_HP_NPCL_PCT_OID],
    "CF289X": [_HP_NPCL_PCT_OID],
    "CF289Y": [_HP_NPCL_PCT_OID],
    # M604/M605/M606 (81A/81X)
    "CF281A": [_HP_NPCL_PCT_OID],
    "CF281X": [_HP_NPCL_PCT_OID],
    "W9008MC": [_HP_NPCL_PCT_OID],
}

# Alternative OIDs for Canon
CANON_ALTERNATE_OIDS = [
    "1.3.6.1.4.1.1602.1.2.1.1.1.1.1",
    "1.3.6.1.4.1.1602.1.2.1.1.1.2.1",
]

# Brother toner OID
BROTHER_TONER_OID = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.1"
BROTHER_DRUM_OID = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.2"


# Toner color detection patterns (more accurate)
import re as _re
_TONER_COLOR_PATTERNS = {
    "black": _re.compile(r'\b(black|bk)\b', _re.IGNORECASE),
    "cyan": _re.compile(r'\b(cyan)\b', _re.IGNORECASE),
    "magenta": _re.compile(r'\b(magenta|mgt)\b', _re.IGNORECASE),
    "yellow": _re.compile(r'\b(yellow)\b', _re.IGNORECASE),
}

def _detect_toner_color(name: str) -> Optional[str]:
    """Bug #8: accurate toner color detection with regex (preventing wrong match)"""
    if not name:
        return None
    for color, pattern in _TONER_COLOR_PATTERNS.items():
        if pattern.search(name):
            return color
    return None


# ─── Self-healing brand of real device identity (sysDescr/sysObjectID) ──────────────
# Actual case: "canon" brand was left on 172.16.25.43 in printers.json, but after
# DHCP relocation of a Brother MFC-8510DN sat on the same IP; Result: Canon toner logic
# (discrete code + Remote UI script) ran for nothing on Brother and no toner displayed
# was not given With this layer, if the SNMP identity of the device is inconsistent with the stored brand,
# The brand is automatically modified and persisted in printers.json (+ event log).
_SYS_VENDOR_HINTS = (
    ("toshiba", "toshiba"),
    ("brother", "brother"),
    ("canon", "canon"),
    ("hewlett", "hp"),
    ("jetdirect", "hp"),
    ("hp ", "hp"),
    ("laserjet", "hp"),
    ("officejet", "hp"),
    ("pagewide", "hp"),
    ("kyocera", "kyocera"),
    ("ricoh", "ricoh"),
    ("xerox", "xerox"),
    ("lexmark", "lexmark"),
    ("samsung", "samsung"),
    ("epson", "epson"),
)

_SYS_OBJECTID_HINTS = (
    ("1.3.6.1.4.1.1129", "toshiba"),
    ("1.3.6.1.4.1.1602", "canon"),
    ("1.3.6.1.4.1.2435", "brother"),
    ("1.3.6.1.4.1.11", "hp"),
)


def _vendor_hint_from_identity(sys_desc: str = "", sys_object_id: str = "") -> Optional[str]:
    """From sysDescr (and sysObjectID if empty) guesses the actual brand of the device."""
    ls = (sys_desc or "").lower()
    for token, hint in _SYS_VENDOR_HINTS:
        if token in ls:
            return hint
    oid = sys_object_id or ""
    for prefix, hint in _SYS_OBJECTID_HINTS:
        if prefix in oid:
            return hint
    return None


def _toner_level_missing(toners: Dict) -> bool:
    """Do we have numbers for any actual "toners"? (Drum/OPC are not counted)

    Note: In Brother rum, the drum percentage may be healthy, but the toner is unknown;
    The simple condition "no raw_level" in that case falsely prevents web fallback.
    """
    toner_keys = [k for k in toners.keys() if k not in ("drum", "opc")]
    if not toner_keys:
        return True
    return not any(toners[k].get("raw_level") is not None for k in toner_keys)


def _merge_scraped_toners(toners: Dict, scraped: Dict, source: str) -> int:
    """Integration of web script result in toners dictionary (same as Canon/TopAccess template).
    "_" keys are metadata (like "_used" in HP) and are ignored.
    Output: The number of toners whose value has been updated."""
    merged = 0
    for color_key, pct in scraped.items():
        if color_key.startswith("_") or not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        pct = int(pct)
        t = toners.get(color_key)
        if not t:
            toners[color_key] = {
                "level": pct, "raw_level": pct, "status": "unknown",
                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                "source": source,
            }
            merged += 1
            continue
        if t.get("raw_level") is None:
            t["raw_level"] = pct
        if t.get("level") is None:
            t["level"] = pct
            t["source"] = source
            if pct == 0:
                t["status"] = "empty"
            elif pct <= 10:
                t["status"] = "critical"
            elif pct <= 25:
                t["status"] = "low"
            else:
                t["status"] = "ok"
            merged += 1
    return merged

# Auxiliary functions
def _log_to_toner_report(content: str):
    """Add line to toner_report.txt file"""
    try:
        with open("toner_report.txt", "a", encoding="utf-8") as f:
            f.write(content + "\n")
    except Exception as e:
        log.error(f"خطا در نوشتن toner_report: {e}")


def _log_validation_error(ip: str, error_type: str, details: str):
    """Record errors in the validation log file"""
    try:
        timestamp = datetime.now().isoformat()
        with open(VALIDATION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] IP: {ip} | Type: enhanced_{error_type}\n")
            f.write(f"  Details: {details}\n\n")
    except Exception as e:
        log.error(f"خطا در نوشتن validation log: {e}")


def detect_snmp_version(ip: str, community: str = "public", timeout: float = 2.0) -> Optional[int]:
    """
    Detection of SNMP version relying on cache and negative-cache protocol module.
    Returns: 1, 2, or None
    """
    return _detect_snmp_version(ip, community, probe_timeout=timeout)


def try_alternative_oids(ip: str, community: str, brand: str, cartridge_model: str = "",
                         snmp_version: int = None, timeout: float = 3.0) -> Optional[int]:
    """Try alternative OIDs to get the toner level"""

    if brand == "hp":
        # First, OIDs for the cartridge model (case-insensitive match)
        cart_upper = (cartridge_model or "").upper()
        for model_key, oid_list in HP_ALTERNATE_OIDS.items():
            if model_key in cart_upper:
                for oid in oid_list:
                    val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
                    if val is not None:
                        try:
                            int_val = int(val)
                            if 0 <= int_val <= 100:
                                return int_val
                        except Exception:
                            pass

        # Generic HP OIDs
        general_oids = [
            "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.1.5.5.1.1",
            "1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.4.1.2.1.5.5.1.1",
            "1.3.6.1.4.1.11.2.3.9.1.1.7.0",
        ]
        for oid in general_oids:
            val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
            if val is not None:
                try:
                    int_val = int(val)
                    if 0 <= int_val <= 100:
                        return int_val
                except Exception:
                    pass

    elif brand == "canon":
        for oid in CANON_ALTERNATE_OIDS:
            val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
            if val is not None:
                try:
                    int_val = int(val)
                    if 0 <= int_val <= 100:
                        return int_val
                except Exception:
                    pass

    return None


def walk_supplies_table(ip: str, community: str, brand: str = "unknown",
                        snmp_version: int = None, timeout: float = 2.0, current_total: int = None) -> List[Dict]:
    """
    Complete walk on the prtMarkerSuppliesTable table
    Returns: A list of dictionaries containing cartridge information
    """
    supplies = []

    # For Brother, the proprietary method is tried first.
    # If the toner level is not obtained from the dedicated OID (like some NC-8300h models),
    # We fallback to the standard prtMarkerSuppliesTable method.
    if brand == "brother":
        toner_level = snmp_get_with_fallback(ip, BROTHER_TONER_OID, community,
                                              version=snmp_version, timeout=timeout)
        if toner_level is not None:
            try:
                level = int(toner_level)
                if 0 <= level <= 100:
                    supplies.append({
                        "index": 1,
                        "name": "Black Toner",
                        "model": "Toner Cartridge",
                        "type": 3,
                        "type_name": "toner",
                        "unit": "percent",
                        "max": 100,
                        "remaining": level,
                        "percent": level,
                        "status": "critical" if level <= 10 else "low" if level <= 25 else "ok",
                    })
            except Exception:
                pass

        # Let's play drama too
        drum_level = snmp_get_with_fallback(ip, BROTHER_DRUM_OID, community,
                                             version=snmp_version, timeout=timeout)
        if drum_level is not None:
            try:
                level = int(drum_level)
                if 0 <= level <= 100:
                    supplies.append({
                        "index": 2,
                        "name": "Drum Unit",
                        "model": "Drum Unit",
                        "type": 7,  # OPC type
                        "type_name": "opc",
                        "unit": "percent",
                        "max": 100,
                        "remaining": level,
                        "percent": level,
                        "status": "critical" if level <= 10 else "low" if level <= 25 else "ok",
                    })
            except Exception:
                pass

        # If the proprietary toner is read successfully, return the same.
        # Otherwise, we proceed to the prtMarkerSuppliesTable public method.
        if any(s.get("type_name") == "toner" for s in supplies):
            return supplies

    # Standard practice for other brands
    for idx in range(1, ENHANCED_MAX_SUPPLIES + 1):
        try:
            name_oid = f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"
            name = snmp_get_with_fallback(ip, name_oid, community,
                                          version=snmp_version, timeout=timeout)

            if name is None:
                if idx >= 5 and brand in ["canon", "hp", "brother"]:
                    break
                continue

            name_str = str(name).strip()

            type_oid = f"1.3.6.1.2.1.43.11.1.1.5.1.{idx}"
            stype = snmp_get_with_fallback(ip, type_oid, community,
                                           version=snmp_version, timeout=timeout)

            unit_oid = f"1.3.6.1.2.1.43.11.1.1.7.1.{idx}"
            unit_val = snmp_get_with_fallback(ip, unit_oid, community,
                                              version=snmp_version, timeout=timeout)

            max_oid = f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"
            max_val = snmp_get_with_fallback(ip, max_oid, community,
                                             version=snmp_version, timeout=timeout)

            rem_oid = f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"
            rem_val = snmp_get_with_fallback(ip, rem_oid, community,
                                             version=snmp_version, timeout=timeout)

            stype_int = 0
            if stype is not None and str(stype).lstrip('-').isdigit():
                stype_int = int(stype)

            type_names = {
                1: "other", 2: "unknown", 3: "toner", 4: "wasteToner",
                5: "ink", 6: "wasteInk", 7: "OPC", 8: "developer",
                9: "fuser", 10: "cleaner", 11: "transfer", 12: "staples",
                21: "cartridge"
            }
            type_name = type_names.get(stype_int, f"type_{stype_int}")
            # Brother reports some consumables with inaccurate types.
            # To display the drum and toner correctly, we also use the user name.
            if brand == "brother":
                lowered_name = name_str.lower()
                if "drum" in lowered_name:
                    type_name = "drum"
                elif "toner" in lowered_name:
                    type_name = "toner"

            percent = None
            max_int = -2
            rem_int = -2
            unit_int = None
            percent_source = None
            unit_names = {
                1: "other", 2: "unknown", 3: "tenThousandthsOfInches",
                4: "micrometers", 7: "impressions", 8: "sheets",
                16: "feet", 17: "meters", 18: "items", 19: "percent",
            }

            try:
                if unit_val is not None and str(unit_val).lstrip('-').isdigit():
                    unit_int = int(unit_val)
                if max_val is not None and str(max_val).lstrip('-').isdigit():
                    max_int = int(max_val)
                if rem_val is not None and str(rem_val).lstrip('-').isdigit():
                    rem_int = int(rem_val)

                # ✅ Bug #20: Calculate percentage only when max_int is valid
                # Remove elif condition that assumed rem_int between 0-100 = percent
                if max_int > 0 and rem_int >= 0:
                    percent = round(rem_int / max_int * 100)
                elif unit_int == 19 and 0 <= rem_int <= 100:
                    # ✅ Fix HP FutureSmart (M527 / E52645): with "percent (19)" unit
                    # Level itself is the same percentage, even if MaxCapacity is not read or
                    # The device returned it as -3 (unspecified capacity). Without this law, this
                    # Two models were experiencing the exact same "toner not received" user.
                    percent = rem_int
                    percent_source = "unit_percent"
            except (ValueError, TypeError) as e:
                log.warning(f"Supply conversion error for {ip} idx={idx}: max={max_val}, rem={rem_val}: {e}")

            # If we don't have a percentage, try alternative OIDs
            _canon_discrete = (brand == "canon" and max_int <= 0 and rem_int in (0, 5, 7))
            if percent is None and brand in ["hp", "canon"] and not _canon_discrete:
                alt_percent = try_alternative_oids(ip, community, brand, name_str, snmp_version, timeout)
                if alt_percent is not None:
                    percent = alt_percent
                    percent_source = "alternative_oid"
                    rem_int = alt_percent
                    max_int = 100

            # status
            status = "N/A"
            supply_present = False
            if percent is not None and percent_source != "canon_status_code":
                if percent == 0:
                    status = "empty"
                elif percent <= 10:
                    status = "critical"
                elif percent <= 25:
                    status = "low"
                else:
                    status = "ok"
            elif brand == "canon" and percent is None and max_int <= 0 and rem_int in (0, 5, 7):
                # Canon i-SENSYS fix (like LBP233dw): this family instead of percentage
                # True, returns a discrete status code: 0=Empty, 5=Low, 7=OK.
                # A representative number is only a "demographic estimate" and should never be used as an estimate
                # raw_level to be used in yield learning (percent_source is specified).
                status = {0: "empty", 5: "low", 7: "ok"}[rem_int]
                percent = {0: 0, 5: 15, 7: 70}[rem_int]
                percent_source = "canon_status_code"
                supply_present = rem_int != 0
            elif rem_int == -3 or max_int == -3:
                # ✅ RFC 3805:-3 means "the device knows there is some consumption/space" but
                # It does not have a number. The status is unknown+present, not "not supported".
                status = "unknown"
                supply_present = True
            elif rem_int == -2:
                # Bug #15: Never assume rem as a percentage when we don't have a valid max.
                # This mode usually means that the reliable sensor/value is not available.
                status = "no_sensor" if name_str and name_str != "Unknown" else "not_supported"
                log.info(f"  [{ip}] Supply {idx} ({name_str}): no sensor data available")

            # Duplicate unknowns filter
            if name_str.startswith("Unknown"):
                unknown_count = sum(1 for s in supplies if s["name"].startswith("Unknown"))
                if unknown_count > 2:
                    continue

            unit_name = unit_names.get(unit_int, "unknown")
            # Fix phantom lines (real discovery: Canon LBP2330K on 172.16.0.43):
            # This device gives the correct first line (Cartridge 324 II, level=10) but
            # The next three lines have "empty name + no data". Before this fix, those lines
            # They rejected the same table and because their names were empty, when making toners, all of them
            # They hit the "black" key and overwrite the actual value of 10% with unknown
            # They did — result: "toner not read" even though SNMP was fine!
            if not name_str and percent is None:
                log.info(f"  [{ip}] Supply {idx}: رد سطر فانتوم (نام خالی و بدون داده قابل‌استفاده)")
                continue
            # If max is greater than 100 and unit is not percentage, it is usually declared capacity
            # It is by the device itself (page/Impressions/Items). This amount to the Yield Engine
            # It is given so that default=2000 is only the last fallback.
            device_capacity_pages = None
            if max_int > 100 and unit_int != 19 and type_name in ("toner", "cartridge", "drum", "OPC", "opc"):
                device_capacity_pages = max_int

            supplies.append({
                "index": idx,
                "name": name_str,
                "model": name_str,
                "type": stype_int,
                "type_name": type_name,
                "unit": unit_name,
                "unit_code": unit_int,
                "max": max_int if max_int != -2 else (100 if percent is not None else "N/A"),
                "remaining": rem_int if rem_int >= 0 else ("N/A" if rem_int == -2 else "unsupported"),
                "percent": percent,
                "percent_source": percent_source,
                "supply_present": supply_present,
                "status": status,
                "device_capacity_pages": device_capacity_pages,
            })

        except Exception as e:
            if idx <= 3:
                _log_validation_error(ip, "walk_supplies_exception", f"idx={idx}: {e}")

    return supplies


def walk_input_trays(ip: str, community: str, snmp_version: int = None, timeout: float = 2.0) -> List[Dict]:
    """Walk on prtInputTable table for trays"""
    trays = []

    for idx in range(1, 8):
        try:
            name_oid = f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"
            name = snmp_get_with_fallback(ip, name_oid, community,
                                          version=snmp_version, timeout=timeout)

            cap_oid = f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"
            cap_val = snmp_get_with_fallback(ip, cap_oid, community,
                                             version=snmp_version, timeout=timeout)

            level_oid = f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"
            level_val = snmp_get_with_fallback(ip, level_oid, community,
                                               version=snmp_version, timeout=timeout)

            if name is None and cap_val is None and level_val is None:
                continue

            name_str = str(name).strip() if name else f"Tray {idx}"

            try:
                cap_int = int(cap_val) if cap_val is not None and str(cap_val).lstrip('-').isdigit() else 0
            except Exception:
                cap_int = 0

            try:
                if level_val is not None and str(level_val).lstrip('-').isdigit():
                    level_int = int(level_val)
                else:
                    level_int = -2
            except Exception:
                level_int = -2

            fill_percent = None
            status = "unknown"

            if level_int == -2:
                status = "no_sensor"
            elif level_int == -3:
                status = "not_supported"
            elif cap_int > 0 and level_int >= 0:
                fill_percent = round(level_int / cap_int * 100)
                if level_int == 0:
                    status = "empty"
                elif fill_percent <= 25:
                    status = "low"
                elif fill_percent <= 75:
                    status = "medium"
                else:
                    status = "ok"
            elif level_int >= 0 and level_int <= 100 and cap_int == 0:
                fill_percent = level_int
                status = "ok" if level_int > 25 else "low" if level_int > 10 else "critical"

            trays.append({
                "index": idx,
                "name": name_str,
                "capacity": cap_int,
                "level": level_int if level_int >= 0 else ("N/A" if level_int == -2 else "unsupported"),
                "fill_percent": fill_percent,
                "status": status,
            })

        except Exception:
            continue

    return trays


def _save_oid_profile(ip: str, community: str, snmp_version: int, model: str,
                      device_type: str, total: int, color: Optional[int], bw: int,
                      supplies: List[Dict], trays: List[Dict], scan_ms: int):
    """Create an OID profile for an IP and store it in oid_profiles.json."""
    try:
        candidates = {
            "sys_descr": "1.3.6.1.2.1.1.1.0",
            "sys_uptime": "1.3.6.1.2.1.1.3.0",
            "sys_hostname": "1.3.6.1.2.1.1.5.0",
        }
        oids = {}
        active = 0
        rejected = {}
        for key, oid in candidates.items():
            try:
                val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
                if val is not None:
                    oids[key] = {
                        "oid": oid,
                        "type": "int" if isinstance(val, int) else "str",
                        "category": "sys",
                        "description": key,
                        "unit": "str",
                        "active": True,
                        "last_value": str(val)
                    }
                    active += 1
                else:
                    oids[key] = {"oid": oid, "active": False}
                    rejected[key] = oid
            except Exception as e:
                rejected[key] = str(e)

        # counters and supplies/trays probing (indexes)
        counter_oids = {
            "print_total": "1.3.6.1.2.1.43.10.2.1.4.1.1",
            "print_color": "1.3.6.1.2.1.43.10.2.1.4.1.2",
            "print_mono": "1.3.6.1.2.1.43.10.2.1.4.1.3",
            "prt_marker_total": "1.3.6.1.4.1.1602.1.11.2.1.1.3.1",
        }
        for k, oid in counter_oids.items():
            try:
                val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
                if val is not None:
                    oids[k] = {"oid": oid, "type": "int", "category": "counter", "active": True, "last_value": str(val)}
                    active += 1
                else:
                    oids[k] = {"oid": oid, "active": False}
                    rejected[k] = oid
            except Exception as e:
                rejected[k] = str(e)

        for idx in range(1, ENHANCED_MAX_SUPPLIES + 1):
            name_oid = f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"
            rem_oid = f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"
            max_oid = f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"
            try:
                name = snmp_get_with_fallback(ip, name_oid, community, version=snmp_version, timeout=1.5)
                rem = snmp_get_with_fallback(ip, rem_oid, community, version=snmp_version, timeout=1.5)
                mx = snmp_get_with_fallback(ip, max_oid, community, version=snmp_version, timeout=1.5)
                if name is None and rem is None and mx is None:
                    continue
                key_name = f"toner_name_{idx}"
                oids[key_name] = {"oid": name_oid, "type": "str", "category": "identity", "active": bool(name), "last_value": str(name) if name is not None else None}
                oids[f"toner_remain_{idx}"] = {"oid": rem_oid, "type": "int", "category": "supply", "active": bool(rem is not None), "last_value": str(rem) if rem is not None else None}
                oids[f"toner_max_{idx}"] = {"oid": max_oid, "type": "int", "category": "supply", "active": bool(mx is not None), "last_value": str(mx) if mx is not None else None}
                active += 1
            except Exception as e:
                rejected[f"supply_{idx}"] = str(e)

        for idx in range(1, 9):
            t_name = f"1.3.6.1.2.1.43.8.2.1.13.1.{idx}"
            t_cap = f"1.3.6.1.2.1.43.8.2.1.9.1.{idx}"
            t_lvl = f"1.3.6.1.2.1.43.8.2.1.10.1.{idx}"
            try:
                nm = snmp_get_with_fallback(ip, t_name, community, version=snmp_version, timeout=1.5)
                cap = snmp_get_with_fallback(ip, t_cap, community, version=snmp_version, timeout=1.5)
                lvl = snmp_get_with_fallback(ip, t_lvl, community, version=snmp_version, timeout=1.5)
                if nm is None and cap is None and lvl is None:
                    continue
                key = f"tray{idx}_name"
                oids[key] = {"oid": t_name, "type": "str", "category": "identity", "active": bool(nm), "last_value": str(nm) if nm is not None else None}
                oids[f"tray{idx}_cap"] = {"oid": t_cap, "type": "int", "category": "tray", "active": bool(cap is not None), "last_value": str(cap) if cap is not None else None}
                oids[f"tray{idx}_level"] = {"oid": t_lvl, "type": "int", "category": "tray", "active": bool(lvl is not None), "last_value": str(lvl) if lvl is not None else None}
                active += 1
            except Exception as e:
                rejected[f"tray_{idx}"] = str(e)

        profile = {
            "ip": ip,
            "brand": (model or "unknown").split()[0].lower() if model else "unknown",
            "device_type": device_type or "unknown",
            "scanned_at": utc_now_iso(),
            "scan_ms": scan_ms,
            "oid_total": sum(1 for _ in oids),
            "oid_active": active,
            "oid_inactive": sum(1 for v in oids.values() if not v.get("active")),
            "oid_rejected": len(rejected),
            "oids": oids,
            "current_vals": {k: v.get("last_value") for k, v in oids.items() if v.get("last_value") is not None},
            "rejected_oids": rejected,
            "summary": {
                "model": model or "Unknown",
                "serial": "N/A",
                "brand": (model or "unknown").split()[0].lower() if model else "unknown",
                "total_pages": total,
                "toner_pct": None,
                "device_type": device_type or "mono",
            }
        }

        serial_oids = [
            "1.3.6.1.2.1.43.5.1.1.17.1",
            "1.3.6.1.4.1.1602.1.2.1.4.0",
        ]
        for so in serial_oids:
            try:
                s = snmp_get_with_fallback(ip, so, community, version=snmp_version, timeout=1.5)
                if s:
                    profile["summary"]["serial"] = str(s)
                    break
            except Exception:
                continue

        try:
            from core.oid.scanner import _profiles_lock, _load_oid_profiles, _save_oid_profiles
            with _profiles_lock:
                data = _load_oid_profiles()
                data[ip] = profile
                _save_oid_profiles(data)
            log.info(f"OID profile saved for {ip}")
        except Exception as e:
            log.error(f"Error saving oid_profiles for {ip}: {e}")
    except Exception as e:
        log.debug(f"_save_oid_profile failed for {ip}: {e}")


def detect_printer_type_from_supplies(supplies: List[Dict]) -> str:
    """Identifying the type of printer from consumables"""
    toners = [s for s in supplies if s.get("type") == 3 or s.get("type_name") == "toner"]
    if not toners:
        toners = supplies

    color_keywords = ["cyan", "magenta", "yellow", "سیان", "مژنتا", "color", "colour"]
    for t in toners:
        name_lower = t.get("name", "").lower()
        for c in color_keywords:
            if c in name_lower:
                return "color"

    return "mono"


def _canon_display_percent(model: str, supply_name: str, percent: Optional[int]) -> Optional[int]:
    """Canon panel values are rounded more coarsely than raw PRT-MIB supply values."""
    if percent is None:
        return None
    model_upper = (model or "").upper()
    name_upper = (supply_name or "").upper()
    if "CANON MF" in model_upper and "CARTRIDGE 137" in name_upper and 10 < percent < 20:
        return 20
    return percent


# Regional names equivalent to Canon cartridges
# CRG-137 (America) / CRG-337 (Asia) / CRG-737 (Europe) / CRG-937 (Pacific)
# They are all the same physical cartridge with the same capacity and their number is only regional.
# The project catalog and the Iran shopping market "Cartridge 737" are considered as a reference, but
# A device with another region's firmware reports the equivalent name (actual fleet case:
# SNMP returned a name of "Cartridge 137" while the user installed Cartridge 737).
# The display/learning key is aligned with the canonical name and the raw device name in
# raw_name is preserved. Heuristics that operate on raw names (eg
# _canon_display_percent) still see the original string.
_CANON_REGIONAL_CARTRIDGE_EQUIVALENTS = {
    "cartridge 137": "Cartridge 737",
    "cartridge 337": "Cartridge 737",
    "cartridge 937": "Cartridge 737",
}


def _canonical_cartridge_display_name(name):
    """Canonical cartridge display name: maps canon regional equivalents to "737"."""
    if not name:
        return name
    key = " ".join(str(name).strip().lower().split())
    return _CANON_REGIONAL_CARTRIDGE_EQUIVALENTS.get(key, name)



def _read_toshiba_value(ip: str, community: str, key: str, snmp_version: int, default=None, timeout: float = 2.0):
    """Read one Toshiba value by key over SNMP; returns the default when it is missing or fails."""
    oid = OIDS.get(key)
    if not oid:
        return default
    value = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=timeout)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default



def _collect_toshiba_job_data(ip: str, community: str, snmp_version: int, total: int, color: Optional[int]):
    """Read Toshiba's proprietary counters to prevent overcount and UI/log completion.

    For older models (such as e-STUDIO3540) if the original total OID is not available,
    From the sum of paper-size or sub-counters, a suggested total is made.
    """
    a3_total = _read_toshiba_value(ip, community, "a3_total", snmp_version, default=None)
    a4_total = _read_toshiba_value(ip, community, "a4_total", snmp_version, default=None)
    prev = store._prev.get(ip) or {}

    # ✅ Fix bug "wrong paper size in log": label is placed only when
    # The delta of the paper counters should be in line with the delta of the total counter (proven identity).
    # a3_total + a4_total == print_total). Otherwise, the snapshot is outdated
    # Previous label was "sure but wrong" — now records unknown size.
    paper_size, paper_split, paper_size_reliable = attribute_paper_size(
        total=total,
        prev_total=prev.get("print_total"),
        a3_total=a3_total,
        prev_a3=prev.get("a3_total"),
        a4_total=a4_total,
        prev_a4=prev.get("a4_total"),
        ip=ip,
        a3_lagged=bool(prev.get("a3_lagged")),
        a4_lagged=bool(prev.get("a4_lagged")),
    )

    copy_fc = _read_toshiba_value(ip, community, "print_copy_fc", snmp_version, default=0)
    copy_bw = _read_toshiba_value(ip, community, "print_copy_bw", snmp_version, default=0)
    printer_fc = _read_toshiba_value(ip, community, "print_printer_fc", snmp_version, default=0)
    printer_bw = _read_toshiba_value(ip, community, "print_printer_bw", snmp_version, default=0)
    print_bw_raw = _read_toshiba_value(ip, community, "print_bw", snmp_version, default=None)
    twin = _read_toshiba_value(ip, community, "print_twin", snmp_version, default=0)
    fax = _read_toshiba_value(ip, community, "print_fax", snmp_version, default=None)
    list_count = _read_toshiba_value(ip, community, "print_list", snmp_version, default=None)

    # To register the event, we rely on the raw total and do not include the twin in the pages calculation
    # Because in some Toshiba models, the two-phase update causes duplicate PRINT.
    bw_for_event = print_bw_raw if isinstance(print_bw_raw, int) and print_bw_raw > 0 else (max(0, total - (color or 0)) if total is not None else None)
    scan_fc = _read_toshiba_value(ip, community, "scan_fc", snmp_version, default=None)
    scan_bw = _read_toshiba_value(ip, community, "scan_bw", snmp_version, default=None)
    scan_net_fc = _read_toshiba_value(ip, community, "scan_net_fc", snmp_version, default=None)
    scan_net_bw = _read_toshiba_value(ip, community, "scan_net_bw", snmp_version, default=None)

    paper_sizes = {}
    for key in ["a4", "a3", "a4r", "a5", "b4"]:
        total_key = _read_toshiba_value(ip, community, f"{key}_total", snmp_version, default=None)
        fc_key = _read_toshiba_value(ip, community, f"{key}_fc", snmp_version, default=None)
        bw_key = _read_toshiba_value(ip, community, f"{key}_bw", snmp_version, default=None)
        if total_key is not None or fc_key is not None or bw_key is not None:
            paper_sizes[key.upper()] = {
                "total": total_key or 0,
                "fc": fc_key or 0,
                "bw": bw_key or 0,
            }

    paper_total = None
    if a3_total is not None or a4_total is not None:
        paper_total = (a3_total or 0) + (a4_total or 0)

    sub_counter_total = None
    components = [
        (copy_fc or 0) + (copy_bw or 0),
        (printer_fc or 0) + (printer_bw or 0),
        fax or 0,
        list_count or 0,
    ]
    comp_sum = sum(v for v in components if isinstance(v, int) and v >= 0)
    if comp_sum > 0:
        sub_counter_total = comp_sum

    suggested_total = None
    total_candidates = [v for v in [paper_total, sub_counter_total, print_bw_raw] if isinstance(v, int) and v > 0]
    if total_candidates:
        suggested_total = max(total_candidates)

    return {
        "paper_size": paper_size,
        "paper_split": paper_split,
        "paper_size_reliable": paper_size_reliable,
        "a3_total": a3_total,
        "a4_total": a4_total,
        "black_white_for_event": bw_for_event,
        "suggested_total": suggested_total,
        "suggested_bw": print_bw_raw if isinstance(print_bw_raw, int) and print_bw_raw >= 0 else None,
        "counters": {
            "printer": (printer_fc + printer_bw) if (printer_fc is not None and printer_bw is not None) else total,
            "printer_fc": printer_fc,
            "printer_bw": printer_bw,
            "copy": (copy_fc + copy_bw) if (copy_fc is not None and copy_bw is not None) else None,
            "copy_fc": copy_fc,
            "copy_bw": copy_bw,
            "fax": fax,
            "list": list_count,
            "twin": twin,
            "scan_fc": scan_fc,
            "scan_bw": scan_bw,
            "scan_net_fc": scan_net_fc,
            "scan_net_bw": scan_net_bw,
        },
        "paper_sizes": paper_sizes,
    }


def collect_enhanced(printer: dict, save_to_db: bool = True) -> dict:
    """
    Advanced data collection using the test_toner.py method
    """
    ip = printer["ip"]
    name = printer["name"]
    nickname = printer.get("nickname", "")
    community = printer.get("community", "public")
    brand = printer.get("brand", "").lower()
    start_time = time.time()

    log.info(f"[ENHANCED] Pulling {name} ({ip})")
    _log_to_toner_report(f"\n{'='*80}")
    _log_to_toner_report(f"🖨  {name} ({ip}) | زمان: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # SNMP detection
    snmp_version = detect_snmp_version(ip, community, timeout=2.0)
    if snmp_version is None:
        elapsed = int((time.time() - start_time) * 1000)
        _log_to_toner_report("   ❌ بدون پاسخ SNMP")
        return {
            "ip": ip, "name": name, "nickname": nickname, "brand": brand,
            "online": False, "last_poll": utc_now_iso(), "poll_ms": elapsed,
            "error": "No SNMP response",
        }

    # Basic information
    sys_desc = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.1.0", community, version=snmp_version, timeout=2.0)
    sys_desc_str = str(sys_desc) if sys_desc else ""
    toshiba_vendor_model_probe = None

    # sensor detection; We don't rely only on sysDescr because some ECS100Gs only respond to specific OID.
    sensor_probe = None
    if brand == "sensor" or "ECS100G" in sys_desc_str.upper():
        sensor_probe = True
    else:
        for sensor_oid in (
            "1.3.6.1.4.1.47206.1.0",
            "1.3.6.1.4.1.47206.110.1.2.0",
            "1.3.6.1.4.1.47206.111.1.2.0",
        ):
            sensor_probe = snmp_get_with_fallback(ip, sensor_oid, community, version=snmp_version, timeout=1.5)
            if sensor_probe is not None:
                break
    if sensor_probe:
        from core.collectors.sensor import collect_sensor
        result = collect_sensor(ip, name, community, start_time)
        result["nickname"] = nickname
        return result

    # ─── Self-healing brand: if the stored brand is incompatible with the real identity of the device ───
    # (caused by IP/DHCP migration or wrong manual entry) fix it and make it permanent.
    sys_object_id_str = ""
    vendor_hint = _vendor_hint_from_identity(sys_desc_str)
    if not vendor_hint and not sys_desc_str:
        _soi = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.2.0", community,
                                      version=snmp_version, timeout=2.0)
        if _soi:
            sys_object_id_str = str(_soi)
            vendor_hint = _vendor_hint_from_identity("", sys_object_id_str)
    if vendor_hint and brand != vendor_hint and brand not in ("sensor",):
        if brand:
            log.warning(
                "  [%s] برند ذخیره‌شده «%s» با دستگاه واقعی («%s») ناسازگار است (%s) — اصلاح خودکار",
                ip, brand, vendor_hint, (sys_desc_str or sys_object_id_str)[:60],
            )
            _log_to_toner_report(f"   ⚠️ اصلاح خودکار برند: {brand} → {vendor_hint}")
            try:
                add_event(ip, "BRAND_CORRECTED", {
                    "message": f"برند از «{brand}» به «{vendor_hint}» اصلاح شد (sysDescr دستگاه معتبرتر از مقدار ذخیره‌شده است)",
                    "severity": "warning",
                })
            except Exception:
                pass
        brand = vendor_hint
        printer["brand"] = brand
        try:
            with store.printers_lock:
                for _p in store.PRINTERS:
                    if _p.get("ip") == ip:
                        _p["brand"] = brand
                        store.save_printers(store.PRINTERS)
                        break
        except Exception as _exc:
            log.error("Persisting corrected brand for %s failed: %s", ip, _exc)

    # The main counters
    # Trying to read total from different OIDs
    total = 0
    total_oids = [
        "1.3.6.1.2.1.43.10.2.1.4.1.1",  # standard
        "1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.4",  # Toshiba
    ]
    for oid in total_oids:
        val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
        if val is not None:
            try:
                total = int(val)
                # ✅ Bug #12: validation of the total value (preventing negative values)
                if total < 0:
                    log.warning(f"Negative total ({total}) for {ip}, using 0")
                    total = 0
                elif total > 0:
                    break
            except (ValueError, TypeError) as e:
                log.warning(f"Total conversion error for {ip}: {val!r}: {e}")

    # Reading advanced information
    supplies = walk_supplies_table(ip, community, brand, snmp_version, timeout=ENHANCED_TIMEOUT, current_total=total)
    trays = walk_input_trays(ip, community, snmp_version, timeout=ENHANCED_TIMEOUT)

    # Color detection
    # A color device should not be mistakenly assumed to be mono if supplies are not read in a poll.
    prev_snapshot = store._prev.get(ip) or {}
    configured_type = (printer.get("device_type") or "").strip().lower()
    previous_type = (prev_snapshot.get("device_type") or "").strip().lower()
    detected_type = detect_printer_type_from_supplies(supplies) if supplies else "unknown"
    device_type = detected_type if detected_type != "unknown" else (configured_type or previous_type or "unknown")
    if device_type == "color":
        # Attempting to read the color counter
        color = 0
        color_oids = [
            "1.3.6.1.2.1.43.10.2.1.4.1.2",  # standard color
            "1.3.6.1.4.1.1129.2.3.50.1.3.21.6.1.2.1.1",  # Toshiba color
        ]
        for oid in color_oids:
            val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
            if val is not None:
                try:
                    color = int(val)
                    if color > 0:
                        break
                except Exception:
                    pass
        # ✅ Bug #16: validation of color value (preventing color > total)
        if color > total and total > 0:
            log.warning(f"color ({color}) > total ({total}) for {ip}, correcting")
            color = None
            bw = total
        elif color > 0:
            bw = max(0, total - color)
        else:
            color = None
            bw = total
    elif device_type == "mono":
        color = None
        bw = total
    else:
        # Device type is uncertain; Don't assume the separation of color/black and white is absolute.
        color = None
        bw = None

    # Some Toshibas do not respond to the standard sysDescr. For these models,
    # We also probe with the specific OID model so that the vendor-specific path is activated.
    if brand != "toshiba" and "toshiba" not in sys_desc_str.lower():
        try:
            toshiba_vendor_model_probe = snmp_get_with_fallback(
                ip,
                OIDS.get("model"),
                community,
                version=snmp_version,
                timeout=2.0,
            )
            if toshiba_vendor_model_probe:
                brand = "toshiba"
        except Exception:
            toshiba_vendor_model_probe = None

    is_toshiba = brand == "toshiba" or "toshiba" in sys_desc_str.lower() or bool(toshiba_vendor_model_probe)
    toshiba_data = None
    toshiba_vendor_total_failed = False
    if is_toshiba:
        try:
            toshiba_total = _read_toshiba_value(ip, community, "print_total", snmp_version, default=None)
            if toshiba_total is not None and toshiba_total >= 0:
                total = toshiba_total
            else:
                # ✅ Fix the oscillation of the Toshiba counter (the main cause of +123 thousand page spikes
                # In the actual log): When the vendor OID is not read, it should not be countered
                # Switched standard/mixed with different scale. The previous snapshot is preserved.
                toshiba_vendor_total_failed = True
                log.warning(
                    "  [%s] Toshiba vendor print_total unreadable this poll; keeping previous snapshot",
                    ip,
                )
            toshiba_color = _read_toshiba_value(ip, community, "print_fc", snmp_version, default=None)
            if toshiba_color is not None:
                color = min(toshiba_color, total) if total is not None and total > 0 else toshiba_color
                device_type = "color" if color > 0 or device_type == "color" else device_type
            bw = max(0, total - (color or 0)) if total is not None else bw
            toshiba_data = _collect_toshiba_job_data(ip, community, snmp_version, total, color)
            has_baseline = bool((store._prev.get(ip) or {}).get("print_total") is not None)
            if (not total or total <= 0) and not has_baseline and toshiba_data and isinstance(toshiba_data.get("suggested_total"), int) and toshiba_data.get("suggested_total") > 0:
                # Only use the combination of side counters in the first poll (without baseline).
                # After baseline, switch to these side counters (such as comp_sum=2124).
                # Actual log vs. print_total=160k) would cause explosive deltas.
                total = toshiba_data["suggested_total"]
                log.info("Toshiba total fallback (first poll only) for %s -> %s", ip, total)
            if (bw is None or bw <= 0) and toshiba_data and isinstance(toshiba_data.get("suggested_bw"), int) and toshiba_data.get("suggested_bw") > 0:
                bw = toshiba_data["suggested_bw"]
                log.info("Toshiba BW fallback for %s -> %s", ip, bw)
        except Exception as exc:
            log.warning("Toshiba vendor counters unavailable for %s: %s", ip, exc)
            toshiba_data = None

    # Model and serial number
    model = "Unknown"
    serial = "N/A"

    # Trying to read models from different OIDs
    model_oids = [
        "1.3.6.1.2.1.43.5.1.1.16.1",  # standard
        "1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1",  # Toshiba
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.2.0",  # HP
        "1.3.6.1.4.1.1602.1.1.1.1.0",  # Canon
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.1",  # Brother
        "1.3.6.1.2.1.1.5.0",  # sysName (last fallback)
    ]
    for oid in model_oids:
        val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
        if val and str(val).strip() not in ("", "N/A", "None"):
            model = str(val).strip()[:100]
            break

    serial_oids = [
        "1.3.6.1.2.1.43.5.1.1.17.1",
        "1.3.6.1.4.1.1129.2.3.50.1.2.4.1.8.1.1",
        "1.3.6.1.4.1.11.2.3.9.1.1.3.1.1.1.1.3.0",
        "1.3.6.1.4.1.1602.1.2.1.4.0",
        "1.3.6.1.4.1.2435.2.4.3.99.3.1.6.1.2.3",  # Brother
    ]
    for oid in serial_oids:
        val = snmp_get_with_fallback(ip, oid, community, version=snmp_version, timeout=2.0)
        if val and str(val).strip() not in ("", "N/A", "None"):
            serial = str(val).strip()[:100]
            break

    # Converting toners to toners format
    toners = {}
    for s in supplies:
        if s["type_name"] in ("toner", "cartridge", "drum", "opc"):
            lowered_name = str(s.get("name", "")).lower()
            if s["type_name"] in ("drum", "opc") or "drum" in lowered_name:
                color_key = "drum"
                display_level = s["percent"]
            else:
                # Bug #8: Using accurate color detection with regex
                color_key = _detect_toner_color(s["name"]) or "black"
                display_level = _canon_display_percent(model, s["name"], s["percent"])

            # ✅ "Keep better value" protection: if for this key already real number
            # registered (eg idx=1 with level=10) and new candidate has no data
            # (phantom/secondary row), do not overwrite the actual value.
            existing = toners.get(color_key)
            if existing and existing.get("level") is not None and s["percent"] is None:
                continue

            # ✅ Fix Canon cartridge name: regional equivalent (137/337/937) → Canonical 737
            # The raw SNMP name is kept in raw_name for tracing.
            raw_supply_name = s["name"]
            display_supply_name = (
                _canonical_cartridge_display_name(raw_supply_name) if brand == "canon" else raw_supply_name
            )

            toners[color_key] = {
                "level": display_level,
                # Fix: raw_level must be really raw to learn yield from data
                # Do not use artificially made. Estimated values ​​(discrete status code
                # Canon, and...) only for display and they make raw None.
                "raw_level": s["percent"] if s.get("percent_source") != "canon_status_code" else None,
                "percent_source": s.get("percent_source"),
                "supply_present": s.get("supply_present", False),
                "status": s["status"] if s["status"] != "N/A" else "unknown",
                "name": display_supply_name,
                "raw_name": raw_supply_name if display_supply_name != raw_supply_name else None,
                "remaining": s["remaining"],
                "max": s["max"],
                "unit": s.get("unit"),
                "unit_code": s.get("unit_code"),
                "device_capacity_pages": s.get("device_capacity_pages"),
                "index": s.get("index"),
            }

    # ─── Toshiba TopAccess fallback ───
    # ✅ Toner accuracy fix: Many e-STUDIOs use toner percentage from prtMarkerSuppliesTable
    # do not give (percent=None) and only have a surface status (Kestrel NE flags) that
    # Estimated steps are converted to 85%/70%/30%/…. In this case, from the scrape panel
    # TopAccess (the same method as the legacy collector with an internal cache) reads the actual percentage.
    if is_toshiba:
        need_scrape = False
        for t in toners.values():
            lvl = t.get("raw_level", t.get("level"))
            if lvl is None and (t.get("status") in ("unknown", "no_sensor", "not_supported", "N/A") or t.get("percent_missing", False)):
                need_scrape = True
                break
        if not any((t.get("raw_level", t.get("level")) is not None) for t in toners.values()):
            need_scrape = True
        if need_scrape:
            try:
                from core.collectors.toshiba import _scrape_toshiba_toners
                scraped = _scrape_toshiba_toners(ip)
                if scraped:
                    for color_key, pct in scraped.items():
                        t = toners.get(color_key)
                        if not t:
                            toners[color_key] = {
                                "level": pct, "raw_level": pct, "status": "unknown",
                                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                                "source": "topaccess_scrape",
                            }
                            continue
                        if (t.get("raw_level") is None):
                            t["raw_level"] = pct
                        if (t.get("level") is None):
                            t["level"] = pct
                            t["source"] = "topaccess_scrape"
                            if pct <= 5:
                                t["status"] = "critical"
                            elif pct <= 15:
                                t["status"] = "low"
                            else:
                                t["status"] = "ok"
                    log.info("  [%s] Toshiba toner percent via TopAccess: %s", ip, scraped)
            except Exception as exc:
                log.debug("Toshiba TopAccess fallback failed for %s: %s", ip, exc)

    # ─── Canon Remote UI fallback ───
    # Canon i-SENSYS fix (like LBP233dw): if no "real" percentage from SNMP path
    # did not arrive (discrete code or completely unknown), we scrip from the device's Remote UI panel —
    # The same method as the legacy collector. The scrape data is real, so is the raw_level
    # It fills honestly.
    if brand == "canon" or "canon" in sys_desc_str.lower():
        if not any(t.get("raw_level") is not None for t in toners.values()):
            try:
                from core.collectors.canon import _scrape_canon_toners
                scraped = _scrape_canon_toners(ip)
                if scraped:
                    for color_key, pct in scraped.items():
                        t = toners.get(color_key)
                        if not t:
                            toners[color_key] = {
                                "level": pct, "raw_level": pct, "status": "unknown",
                                "name": f"{color_key} toner", "remaining": -1, "max": -1,
                                "source": "canon_remote_ui",
                            }
                            continue
                        if t.get("raw_level") is None:
                            t["raw_level"] = pct
                        if t.get("level") is None:
                            t["level"] = pct
                            t["source"] = "canon_remote_ui"
                            if pct == 0:
                                t["status"] = "empty"
                            elif pct <= 10:
                                t["status"] = "critical"
                            elif pct <= 25:
                                t["status"] = "low"
                            else:
                                t["status"] = "ok"
                    log.info("  [%s] Canon toner percent via Remote UI scrape: %s", ip, scraped)
            except Exception as exc:
                log.debug("Canon Remote UI fallback failed for %s: %s", ip, exc)

    # ─── Brother web fallback ───
    # ✅ NC-8xxx/MFC-85xx family in standard table only level=-3 (there is some value)
    # and there is not always a dedicated OID; Their web panel (confirmation: 172.16.25.43)
    # It is usually open and shows the percentage in text. "Actually Missing Toner" condition (not just
    # "no level") until a healthy drum (eg 90%) does not prevent the script from running.
    if brand == "brother" or "brother" in sys_desc_str.lower():
        if _toner_level_missing(toners):
            try:
                from core.collectors.brother import _scrape_brother_toners
                scraped = _scrape_brother_toners(ip)
                if scraped:
                    merged = _merge_scraped_toners(toners, scraped, "brother_web")
                    if merged:
                        log.info("  [%s] Brother toner percent via web scrape: %s", ip, scraped)
            except Exception as exc:
                log.debug("Brother web fallback failed for %s: %s", ip, exc)

    # ─── HP web fallback ───
    # ✅ Real item E52645: device for W9008MC cartridge, value level=-2 (unknown)
    # returns and NPCL private OID also does not respond. Many FutureSmarts port
    # 80 are closed but 443 are open — HTTP+HTTPS+XML DevMgmt consumption attempts.
    if brand == "hp" or "hp" in sys_desc_str.lower() or "laserjet" in sys_desc_str.lower():
        if _toner_level_missing(toners):
            try:
                from core.collectors.hp import _scrape_hp_toners
                scraped = _scrape_hp_toners(ip)
                if scraped:
                    merged = _merge_scraped_toners(toners, scraped, "hp_web")
                    if merged:
                        log.info("  [%s] HP toner percent via web scrape: %s", ip, scraped)
                # ✅ Discovery on real device (E52645 + M401dn): When SNMP for
                # The HP cartridge gives a value of -2 (unknown), usually because the cartridge
                # It is charged/used and the EWS itself does not have the same number. Status scraper
                # Returns "used" so that the UI says what the cause is instead of "ambiguous".
                hp_used = set((scraped or {}).get("_used") or [])
                for color_key in hp_used:
                    t = toners.get(color_key)
                    if t is None:
                        toners[color_key] = {
                            "level": None, "raw_level": None, "status": "used",
                            "name": f"{color_key} cartridge", "remaining": -1,
                            "max": -1, "source": "hp_web_used",
                            "note": "used_cartridge",
                        }
                        continue
                    if t.get("level") is None:
                        t["status"] = "used"
                        t["note"] = "used_cartridge"
                        t.setdefault("source", "hp_web_used")
                        log.info("  [%s] HP %s: کارتریج شارژی/استفاده‌شده (SupplyState=Used) — خود HP درصد را گزارش نمی‌کند",
                                 ip, color_key)
                if not scraped:
                    log.info(
                        "  [%s] HP: تونر از SNMP و وب در دسترس نیست — احتمال کارتریج شارژی/طرح غیراصل "
                        "(chip ناشناخته) یا بسته‌بودن EWS روی دستگاه/فایروال", ip)
            except Exception as exc:
                log.debug("HP web fallback failed for %s: %s", ip, exc)

    # If no toner is found, a default black toner
    if not toners:
        toners["black"] = {"level": None, "raw_level": None, "status": "unknown", "name": "Toner", "remaining": -1, "max": -1}

    # Consumption fields are kept consistent for all brands; If you have given
    # If it doesn't exist, the UI won't display it.
    for toner_data in toners.values():
        toner_data.setdefault("usage", None)
        toner_data.setdefault("usage_m", None)

    if is_toshiba:
        for color_key in ("black", "cyan", "magenta", "yellow"):
            if color_key not in toners:
                continue
            usage_raw = _read_toshiba_value(ip, community, f"toner_{color_key}_usage", snmp_version, default=None)
            if usage_raw is not None and usage_raw > 0:
                toners[color_key]["usage"] = usage_raw
                toners[color_key]["usage_m"] = round(usage_raw / 1_000_000, 2)

    # ─── Cartridge name enrichment: if the toner does not have a name, read from the standard MIB ──
    _color_mib_idx = {"black": 1, "cyan": 2, "magenta": 3, "yellow": 4}
    for col, idx in _color_mib_idx.items():
        t = toners.get(col)
        if t and not t.get("name"):
            try:
                desc = snmp_get_with_fallback(
                    ip, f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}", community,
                    version=snmp_version, timeout=1.5)
                if desc and str(desc).strip():
                    t["name"] = str(desc).strip()
            except Exception:
                pass

    # Apply toner manual override based on page consumption
    prev_override = store._prev.get(ip) or {}
    if prev_override.get('yield_per_page', 2000) == 2000 and not prev_override.get('force_estimate'):
        boot = _bootstrap_yield_from_history(ip, prev_override)
        if boot:
            prev_override = store._prev.get(ip) or prev_override

    pages_since_last_reset = get_pages_since_last_reset(prev_override, total)
    # ✅ "Return manual value to default" fix: all colors with active override
    # They get their display value from the per-color slot — not just the last color.
    # Tracking of expired slots (count down) for on-the-spot clearing.
    try:
        from core.collectors.base import _normalize_overrides as _norm_ov, _overrides_valid_after_total as _ov_valid
        _active_overrides = _ov_valid(_norm_ov(prev_override), total)
    except Exception:
        _active_overrides = {}
    if _active_overrides != dict(_norm_ov(prev_override)):
        store._prev.set(ip, {"toner_overrides": _active_overrides})
    for ov_color in list(_active_overrides.keys()):
        if ov_color not in toners:
            continue
        snmp_level = toners[ov_color].get('level')
        final_level = apply_toner_override(ip, total, snmp_level, color=ov_color)
        if final_level is not None:
            toners[ov_color]['level'] = final_level
            if prev_override.get('force_estimate'):
                toners[ov_color]['source'] = 'forced_estimate'
            if final_level == 0:
                toners[ov_color]['status'] = 'empty'
            elif final_level <= 5:
                toners[ov_color]['status'] = 'critical'
            elif final_level <= 15:
                toners[ov_color]['status'] = 'low'
            else:
                toners[ov_color]['status'] = 'ok'

    # Warnings Warnings
    alerts = []
    for color_key, toner in toners.items():
        level = toner.get("level")
        if level is None:
            continue
        try:
            level = int(level)
        except (TypeError, ValueError):
            continue
        if level > TONER_ALERT_THRESHOLDS.get("warning", 15):
            continue
        status = "critical" if level <= TONER_ALERT_THRESHOLDS.get("critical", 5) else "low"
        toner["status"] = "empty" if level == 0 else status
        alerts.append({
            "message": f"{toner.get('name', color_key)}: {toner['status']} ({level}%)",
            # The warning code must be stable; SNMP table index may change between polls.
            "code": f"toner:{color_key}",
        })

    # ─── uptime ──────────────────────────────────────────────────
    # Important fix: failed read uptime should not be converted to int(0); Zero value in
    # _uptime_reset was incorrectly detecting "reboot" (276 false resets in real log).
    ut = None
    ut_raw = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.3.0", community, version=snmp_version, timeout=2.0)
    if ut_raw is None:
        # SNMP stack of some devices (especially Toshiba) is slow; A more memorable retry
        ut_raw = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.3.0", community, version=snmp_version, timeout=4.0)
    if ut_raw is not None:
        try:
            ut = int(ut_raw)
        except (TypeError, ValueError):
            ut = None
    us = (ut or 0) // 100
    uptime_str = f"{us//86400}d {(us%86400)//3600:02d}:{(us%3600)//60:02d}" if ut else "N/A"

    elapsed = int((time.time() - start_time) * 1000)

    # Register in toner_report.txt
    _log_to_toner_report(f"   SNMP v{snmp_version} | مدل: {model} | نوع: {device_type}")
    bw_display = f"{bw:,}" if isinstance(bw, int) else "نامشخص"
    color_display = f"{color:,}" if isinstance(color, int) else "نامشخص"
    _log_to_toner_report(f"   کل صفحات: {total:,} | رنگی: {color_display} | سیاه‌سفید: {bw_display}")
    for color_key, t in toners.items():
        pct_str = f"{t['level']}%" if t['level'] is not None else "N/A"
        status_icon = {"ok": "✅", "low": "🟡", "critical": "🟠", "empty": "🔴"}.get(t["status"], "❓")
        _log_to_toner_report(f"   {color_key}: {pct_str} {status_icon}")
    _log_to_toner_report(f"   زمان پاسخ: {elapsed}ms")
    try:
        # Save OID profile for this device (update or new build)
        _save_oid_profile(ip, community, snmp_version, model, device_type, total, color, bw, supplies, trays, elapsed)
    except Exception:
        log.debug("Saving oid profile failed, continuing")

    # Bug #2 + #5 + #12: Remove direct DB write
    # Writing to the database is now done after _counters_event
    # (see next lines)

    # Registration of PRINT / REFILL event
    # Bug #2: event registration BEFORE writing to the database
    prev = store._prev.get(ip) or {}
    black_level = None
    # Yield learning and REFILL logic should use the raw surface of the device, not the
    # display value/override; Otherwise, the estimation-learning cycle becomes circular.
    if toners.get("black", {}).get("raw_level") is not None:
        legacy_toner_color = "black"
        black_level = toners["black"]["raw_level"]
        toners["black"]["level"] = toners["black"]["level"] if toners["black"].get("level") is not None else black_level
    elif toners.get("black", {}).get("level") is not None:
        legacy_toner_color = "black"
        black_level = toners["black"]["level"]
    else:
        legacy_toner_color = None
        for ck, t in toners.items():
            if t.get("raw_level") is not None:
                black_level = t["raw_level"]
                legacy_toner_color = ck
                break
            if t.get("level") is not None:
                black_level = t["level"]
                legacy_toner_color = ck
                break

    # Raw level of each color to automatically detect cartridge replacement/charging (event
    # CARTRIDGE_CHANGED in _counters_event). Only real data (raw_level) is entered
    # becomes; Demo estimates (like canon_status_code) don't create fake events.
    toner_levels_for_events = {}
    for ck in ("black", "cyan", "magenta", "yellow"):
        _t = toners.get(ck) or {}
        _rv = _t.get("raw_level")
        if _rv is None:
            continue
        try:
            toner_levels_for_events[ck] = int(_rv)
        except (TypeError, ValueError):
            continue

    # Unique ID of the cartridge (ChipID/serial from SNMP/Web) — definitive signal of replacement.
    # It has a cache of 10 minutes and returns empty in the absence of evidence; fallback same path
    # Level jump. Error reading also never breaks the poll cycle.
    cartridge_ids_event = None
    cartridge_pages_event = None
    cartridge_id_source = "none"
    cartridge_signal_quality = {}
    cartridge_identity_type = {}
    try:
        from core.collectors.cartridge_id import get_cartridge_identity_data
        # If stored brand is empty/invalid, infer from sysDescr (most authoritative source).
        _cid_brand = brand
        if _cid_brand not in ("hp", "canon", "brother", "toshiba"):
            _sd = (sys_desc_str or "").lower()
            if "hp" in _sd or "laserjet" in _sd or "hewlett" in _sd:
                _cid_brand = "hp"
            elif "canon" in _sd:
                _cid_brand = "canon"
            elif "brother" in _sd:
                _cid_brand = "brother"
            elif "toshiba" in _sd:
                _cid_brand = "toshiba"
        _cid = get_cartridge_identity_data(
            ip=ip, brand=_cid_brand, community=community, snmp_version=snmp_version)
        if _cid:
            cartridge_ids_event = _cid.get("ids") or {}
            cartridge_pages_event = _cid.get("supply_pages") or {}
            cartridge_id_source = _cid.get("source") or "none"
            cartridge_signal_quality = _cid.get("signal_quality") or {}
            cartridge_identity_type = _cid.get("identity_type") or {}
        # 🔍 Automatic discovery: if no ID is found for this device and brand yet
        # is known, once added to the OID discovery queue (background worker,
        # dedup + cooldown — has no overhead on the poll itself).
        if not cartridge_ids_event and _cid_brand in ("hp", "canon", "brother", "toshiba"):
            try:
                from core.collectors.cartridge_discovery import queue_discovery
                queue_discovery(ip, _cid_brand, community, snmp_version)
            except Exception as exc:
                log.debug("  [%s] cartridge discovery queue failed: %s", ip, exc)
    except Exception as exc:
        log.debug("  [%s] cartridge identity read failed: %s", ip, exc)

    prev_toner = prev.get("toner_level")
    paper_size = (toshiba_data or {}).get("paper_size")
    a3_total = (toshiba_data or {}).get("a3_total")
    a4_total = (toshiba_data or {}).get("a4_total")
    bw_for_event = (toshiba_data or {}).get("black_white_for_event", bw)

    if is_toshiba and toshiba_vendor_total_failed:
        # Protected path: None → _counters_event alerts and previous snapshot
        # keeps; No fake delta (COUNTER_RESET/PRINT_OVERFLOW chain)
        # It is not made. When the vendor is restarted, the values ​​remain consistent.
        total_for_event = None
    else:
        total_for_event = total

    # Bug #2: Registering the first event (before writing the DB)
    # Extracting the name of supply from toners to detect replacement from the name
    _current_supply_names = {}
    for _ck, _tv in (toners or {}).items():
        if _ck in ('drum', 'opc'):
            continue
        _tn = _tv.get('name') if isinstance(_tv, dict) else None
        if _tn:
            _current_supply_names[_ck] = str(_tn).strip()

    _counters_event(ip, total_for_event, prev, alerts, [a["code"] for a in alerts],
                    full_color=color, black_white=bw_for_event, paper_size=paper_size,
                    current_toner_level=black_level, prev_toner_level=prev_toner,
                    toner_levels=toner_levels_for_events, legacy_toner_color=legacy_toner_color,
                    cartridge_ids=cartridge_ids_event, cartridge_supply_pages=cartridge_pages_event,
                    signal_quality=cartridge_signal_quality,
                    supply_names=_current_supply_names or None,
                    uptime=ut, a3_total=a3_total, a4_total=a4_total,
                    poll_timestamp=to_utc_z(datetime.fromtimestamp(start_time, tz=timezone.utc)),
                    paper_split=(toshiba_data or {}).get("paper_split"))

    # ─── New Yield Engine: per-cartridge/per-color learning based on history and anchor ───
    # This layer is independent of legacy logic and is designed for low power and color printers.
    yield_engine_meta = {}
    try:
        from core.yield_engine import process_printer_yield_snapshot
        yield_engine_meta = process_printer_yield_snapshot(
            ip=ip,
            printer_model=model,
            counters={"total": total, "full_color": color, "black_white": bw},
            toners=toners,
            device_type=device_type,
            timestamp=to_utc_z(datetime.fromtimestamp(start_time, tz=timezone.utc)),
            source="poll",
        )
    except Exception as exc:
        log.exception("Yield engine processing failed for %s: %s", ip, exc)

    # Bug #5: The source of truth is PrevStore/`printer_counters' only.
    # We avoid direct and double writing in the database.
    if save_to_db:
        log.debug("Enhanced snapshot persisted via PrevStore only for %s", ip)

    # The last yield value may be learned/updated inside _counters_event;
    # So we read from PrevStore again for API output.
    final_prev = store._prev.get(ip) or prev_override or {}
    try:
        yield_per_page = int(final_prev.get("yield_per_page", 2000) or 2000)
    except (TypeError, ValueError):
        yield_per_page = 2000
    yield_source = "default" if yield_per_page == 2000 else "learned"
    if final_prev.get("force_estimate"):
        yield_source = "forced_estimate"

    # The legacy value is kept for backward compatibility, but if the Yield Engine
    # To output black, the output API uses the more accurate per-cartridge value.
    api_yield_per_page = yield_per_page
    api_yield_source = yield_source
    api_yield_confidence = "low" if yield_source == "default" else "medium"
    black_meta = yield_engine_meta.get("black") if isinstance(yield_engine_meta, dict) else None
    if black_meta:
        api_yield_per_page = int(black_meta.get("yield_per_page") or yield_per_page)
        api_yield_source = black_meta.get("yield_source") or yield_source
        api_yield_confidence = black_meta.get("confidence") or api_yield_confidence

    # sync with legacy path: missing_yield_printers.txt file and old printer_counters
    # Previously, they only saw the PrevStore value and were unaware of the catalog/device_capacity.
    if api_yield_per_page and api_yield_per_page != yield_per_page:
        try:
            from core.database import update_missing_yield_list
            store._prev.set(ip, {"yield_per_page": api_yield_per_page})
            update_missing_yield_list(ip, api_yield_per_page, api_yield_source)
        except Exception as exc:
            log.exception("legacy yield sync failed for %s: %s", ip, exc)

    for toner_color, toner_data in toners.items():
        engine_info = yield_engine_meta.get(toner_color) if isinstance(yield_engine_meta, dict) else None
        if engine_info:
            toner_yield = int(engine_info.get("yield_per_page") or yield_per_page)
            toner_source = engine_info.get("yield_source") or yield_source
            toner_confidence = engine_info.get("confidence") or "low"
            toner_data["capacity_pages"] = toner_yield
            toner_data["capacity_source"] = toner_source
            toner_data["yield_per_page"] = toner_yield
            toner_data["yield_confidence"] = toner_confidence
            toner_data["yield_sample_count"] = engine_info.get("sample_count", 0)
            toner_data["yield_total_weight"] = engine_info.get("total_weight", 0)
            toner_data["cycle_status"] = engine_info.get("cycle_status")
            toner_data["pages_after_zero"] = engine_info.get("pages_after_zero", 0)
            toner_data["cycle_start_counter"] = engine_info.get("cycle_start_counter")
            toner_data["cycle_start_level"] = engine_info.get("cycle_start_level")
            toner_data["yield_cartridge_key"] = engine_info.get("cartridge_key")
            if engine_info.get("shared_profile"):
                toner_data["shared_yield_profile"] = engine_info["shared_profile"]
        else:
            # Old fallback for devices that do not have a valid level yet.
            toner_data["capacity_pages"] = yield_per_page
            toner_data["capacity_source"] = yield_source if toner_color == "black" else f"{yield_source}_global"
            toner_data["yield_per_page"] = yield_per_page
            toner_data["yield_confidence"] = "low" if yield_source == "default" else "medium"

    if is_toshiba and toshiba_vendor_total_failed:
        prev_counters = (store._prev.get(ip) or {})
        total = prev_counters.get("print_total") if prev_counters.get("print_total") is not None else total
        color = prev_counters.get("full_color", color)
        bw = prev_counters.get("black_white", bw)

    vendor_counters = (toshiba_data or {}).get("counters", {})
    return {
        "ip": ip, "name": name, "nickname": nickname, "brand": brand,
        "device_type": device_type,
        "online": True,
        "last_poll": utc_now_iso(),
        "poll_ms": elapsed,
        "device": {
            "model": model,
            "serial": serial,
            "firmware": "N/A",
            "uptime_str": uptime_str,
        },
        "counters": {
            "total": total,
            "full_color": color if color else None,
            "black_white": bw,
            "printer": vendor_counters.get("printer", total),
            "printer_fc": vendor_counters.get("printer_fc"),
            "printer_bw": vendor_counters.get("printer_bw"),
            "copy": vendor_counters.get("copy"),
            "copy_fc": vendor_counters.get("copy_fc"),
            "copy_bw": vendor_counters.get("copy_bw"),
            "fax": vendor_counters.get("fax"),
            "list": vendor_counters.get("list"),
            "twin": vendor_counters.get("twin"),
            "scan_fc": vendor_counters.get("scan_fc"),
            "scan_bw": vendor_counters.get("scan_bw"),
            "scan_net_fc": vendor_counters.get("scan_net_fc"),
            "scan_net_bw": vendor_counters.get("scan_net_bw"),
            "pages_since_last_reset": pages_since_last_reset,
            "yield_per_page": api_yield_per_page,
            "yield_source": api_yield_source,
            "yield_confidence": api_yield_confidence,
            "force_estimate": final_prev.get("force_estimate", 0),
            "yield_learning_failures": final_prev.get("yield_learning_failures", 0),
        },
        "paper_sizes": (toshiba_data or {}).get("paper_sizes", {}),
        "trays": trays,
        "toners": toners,
        "alerts": alerts,
        # Cartridge unique ID + detection source (for API/dashboard)
        "cartridge_ids": cartridge_ids_event or {},
        "cartridge_id_source": cartridge_id_source,
        "cartridge_signal_quality": cartridge_signal_quality or {},
        "cartridge_identity_type": cartridge_identity_type or {},
    }