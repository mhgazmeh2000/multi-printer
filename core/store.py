# =============================================================================
# @file:         core/store.py
# @brief:        Process-wide in-memory store for printers, current data and polling statistics.
# @details:      Responsibilities: Hold shared state under locks and load or save the
#                printers.json file.
#                Main API: PrevStore, load_printers(), save_printers()
#                Side effects: starts threads, file I/O
# @dependencies: internal: config.settings, core.database, core.time_utils
# @notes:        Shared in-memory state: always take the matching lock before reading or writing.
# =============================================================================

"""
Program global mode:
- List of printers (PRINTERS) and its lock
- Current data (printer_data) and its lock
- Polling statistics
- Printers.json file management
- Saving the previous values of the counters in the database (PrevStore class)
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

from config.settings import PRINTERS_FILE, DEFAULT_PRINTERS
from core.database import (
    load_printer_counters,
    save_printer_counters,
    delete_printer_counters,
    update_missing_yield_list,
    record_toner_snapshot,
)
from core.time_utils import parse_utc, utc_now_iso

log = logging.getLogger("PrinterMonitor")

# - Locks - Locks
printers_lock = threading.Lock()
data_lock = threading.Lock()
_prev_lock = threading.Lock()   # Lock for PrevStore

# Global data
printer_data = {}          # ip → dict complete printer data
poll_stats = {"count": 0, "last": None, "errors": 0, "is_polling": False}


# ─── Class store previous values with database backup ──────────────────────
class PrevStore:
    """
    Storing previous values of counters in memory (cache) and database.
    
    🔥 Important change: get() returns None if there is no record in the database
    (not an empty dictionary) so that the first poll can be recognized.
    """

    def __init__(self):
        """Create an empty cache with its lock."""
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, ip: str):
        """
        Get previous values ​​for a printer.
        🔥 Returns None if there is no previous data in the database or cache.
        """
        with self._lock:
            # If it is in the cache, return it
            if ip in self._cache:
                return self._cache[ip].copy() if self._cache[ip] else None

            # Read from the database
            db_data = load_printer_counters(ip)

            if db_data is None:
                # There is no data in the database
                self._cache[ip] = None
                return None

            # The data is in the database
            self._cache[ip] = db_data
            return db_data.copy()

    def set(self, ip: str, data: dict):
        """Save new values ​​for a printer (in cache and database) with baseline protection."""
        with self._lock:
            try:
                existing = self._cache.get(ip) or load_printer_counters(ip) or {}
                updated_at = data.get("updated_at") or utc_now_iso()
                normalized = {
                    "device_type": existing.get("device_type"),
                    "toner_level": existing.get("toner_level"),
                    "last_alert_codes": existing.get("last_alert_codes", []),
                    "manual_override": existing.get("manual_override", 0),
                    "override_color": existing.get("override_color"),
                    "override_base_level": existing.get("override_base_level"),
                    "override_start_total": existing.get("override_start_total"),
                    "override_start_toner": existing.get("override_start_toner"),
                    "yield_per_page": existing.get("yield_per_page", 2000),
                    "force_estimate": existing.get("force_estimate", 0),
                    "yield_learning_failures": existing.get("yield_learning_failures", 0),
                    **existing,
                    **data,
                    "updated_at": updated_at,
                }

                # Second guard: even if the caller makes a mistake, the suspicious total=0 should not break the baseline.
                try:
                    old_total = existing.get("print_total")
                    new_total = normalized.get("print_total")
                    old_uptime = existing.get("uptime")
                    new_uptime = normalized.get("uptime")
                    uptime_reset = (
                        old_uptime is not None and new_uptime is not None and
                        int(new_uptime) < int(old_uptime) - 60 * 100
                    )
                    if old_total is not None and int(old_total) >= 1000 and int(new_total or 0) == 0 and not uptime_reset:
                        log.warning(
                            "PrevStore guard: ignoring suspicious total=0 for %s (old_total=%s, uptime %s→%s)",
                            ip, old_total, old_uptime, new_uptime,
                        )
                        normalized["print_total"] = old_total
                        normalized["full_color"] = existing.get("full_color")
                        normalized["black_white"] = existing.get("black_white")
                        normalized["a3_total"] = existing.get("a3_total")
                        normalized["a4_total"] = existing.get("a4_total")
                except Exception:
                    # If the guard itself throws an error, we don't stop the main save.
                    pass

                self._cache[ip] = normalized.copy()
                save_printer_counters(ip, normalized)

                # Prevent uncontrolled growth of toner_history: only when there is a significant change or every 30 minutes.
                should_snapshot = True
                try:
                    old_total = existing.get("print_total")
                    old_toner = existing.get("toner_level")
                    new_total = normalized.get("print_total")
                    new_toner = normalized.get("toner_level")
                    changed = old_total != new_total or old_toner != new_toner
                    old_updated = existing.get("updated_at")
                    elapsed = 999999
                    if old_updated:
                        # Phase E.3: UTC-aware "now" and parsed stored value (Z);
                        # Unmigrated/broken value throws exception ⇒ same fallback as before.
                        elapsed = (datetime.now(timezone.utc) - parse_utc(old_updated)).total_seconds()
                    should_snapshot = changed or elapsed >= 1800
                except Exception:
                    should_snapshot = True

                if should_snapshot:
                    record_toner_snapshot(
                        ip,
                        print_total=normalized.get("print_total"),
                        toner_level=normalized.get("toner_level"),
                        yield_per_page=normalized.get("yield_per_page"),
                        timestamp=updated_at,
                        source="prev_store",
                    )
                update_missing_yield_list(ip, int(normalized.get("yield_per_page") or 2000))
                log.debug(
                    f"PrevStore.set: {ip} -> total={normalized.get('print_total')} "
                    f"toner={normalized.get('toner_level')} updated_at={updated_at} snapshot={should_snapshot}"
                )
            except Exception as e:
                log.error(f"PrevStore.set error for {ip}: {e}")

    def delete(self, ip: str):
        """Removing the values ​​of a printer and its baseline from the database."""
        with self._lock:
            self._cache.pop(ip, None)
            update_missing_yield_list(ip, -1)
            delete_printer_counters(ip)

# PrevStore global example
_prev = PrevStore()

# Allowed fields in printers.json
_PRINTER_ALLOWED_FIELDS = {"ip", "name", "community", "brand", "nickname", "device_type", "group"}


def _normalize_printer(p: dict) -> dict:
    """Normalize a printer record: strip text fields and fill in defaults."""
    return {
        "ip": str(p.get("ip", "")).strip(),
        "name": str(p.get("name", "")).strip() or str(p.get("ip", "")),
        "community": str(p.get("community", "public")).strip() or "public",
        "nickname": str(p.get("nickname", "")).strip(),
        "device_type": str(p.get("device_type", "")).strip() or "unknown",
        "group": str(p.get("group", "")).strip(),
        **({"brand": str(p["brand"]).strip().lower()}
           if p.get("brand") and p.get("brand") not in ("", "unknown") else {}),
    }


def _deduplicate_printers(printers: list) -> list:
    """Remove duplicate printer records, keeping the first occurrence of each IP."""
    seen = set()
    result = []
    for p in printers:
        ip = str(p.get("ip", "")).strip()
        if ip and ip not in seen:
            seen.add(ip)
            result.append(p)
    return result


def load_printers() -> list:
    """Load the printer list from printers.json."""

    if os.path.exists(PRINTERS_FILE):
        try:
            with open(PRINTERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            normalized = _deduplicate_printers([_normalize_printer(p) for p in data])
            if normalized:
                return normalized
        except Exception as e:
            log.exception("printers.json خواندن ناموفق: %s", e)
    return [_normalize_printer(p) for p in DEFAULT_PRINTERS]


def save_printers(printers: list) -> list:
    """Write the printer list to printers.json after normalising and de-duplicating it."""

    clean = _deduplicate_printers([_normalize_printer(p) for p in printers])
    with open(PRINTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


# ─── initial list
PRINTERS: list = load_printers()