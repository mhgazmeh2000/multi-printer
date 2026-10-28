# =============================================================================
# @file:         core/oid/validator.py
# @brief:        Validates OID values and records validation errors to a log file.
# @details:      Responsibilities: Check OID value shapes and append errors for later review.
#                Main API: validate_oid_value(), log_validation_error()
#                Side effects: file I/O
# @dependencies: internal: config.settings
# @notes:        None.
# =============================================================================

"""
Validate OID values and record errors in the log file
"""

import logging
from datetime import datetime

from config.settings import VALIDATION_LOG_FILE

log = logging.getLogger("PrinterMonitor")

# Valid negative values in SNMP
# -1 = other, -2 = unknown, -3 = not supported


def _validate_oid_value_strict(key: str, value, category: str) -> tuple:
    """Validate an OID value strictly; returns (ok, message, status code)."""
    if value is None:
        return False, "مقدار None است", "null_value"

    if category == "counter":
        if not isinstance(value, int):
            return False, f"counter باید int باشد، دریافت: {type(value).__name__}", "type_error"
        if value < 0 or value > 999_000_000:
            return False, f"counter خارج از بازه [0, 999M]: {value}", "range_error"

    elif category == "supply":
        if not isinstance(value, int):
            return False, f"supply باید int باشد، دریافت: {type(value).__name__}", "type_error"
        # 🔥 Change: Accept values -1, -2, -3 (valid SNMP negative values)
        if value < -3 or value > 200_000_000:
            return False, f"supply خارج از بازه [-3, 200M]: {value}", "range_error"

    elif category == "tray":
        if not isinstance(value, int):
            return False, f"tray باید int باشد، دریافت: {type(value).__name__}", "type_error"
        if value < -9 or value > 5000:
            return False, f"tray خارج از بازه [-9, 5000]: {value}", "range_error"

    elif category == "status":
        if not isinstance(value, int):
            return False, f"status باید int باشد، دریافت: {type(value).__name__}", "type_error"
        if value < 0 or value > 30:
            return False, f"status خارج از بازه [0, 30]: {value}", "range_error"

    elif category in ("identity", "sys"):
        str_val = str(value).strip()
        if not str_val or str_val in ("None", "0"):
            return False, f"{category} خالی است: {value!r}", "empty_value"

    return True, "✓ معتبر", "valid"


def validate_oid_value(key: str, value, category: str) -> tuple:
    """Returns: (valid: bool, reason: str)"""
    valid, reason, _ = _validate_oid_value_strict(key, value, category)
    return valid, reason


def log_validation_error(ip: str, error_type: str, details: str):
    """Append a validation error line for one printer to the validation log file."""

    try:
        timestamp = datetime.now().isoformat()
        with open(VALIDATION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] IP: {ip} | Type: {error_type}\n")
            f.write(f"  Details: {details}\n\n")
    except Exception as e:
        log.error(f"خطا در نوشتن validation log: {e}")