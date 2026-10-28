# =============================================================================
# @file:         core/collectors/base.py
# @brief:        Shared helpers used by all brand collectors: safe SNMP value conversion, counter
#                and toner logic.
# @details:      Responsibilities: Provide si()/ss() conversions, page-counter arithmetic, toner
#                overrides and paper-size attribution.
#                Main API: si(), ss(), get_pages_since_last_reset(), apply_toner_override(),
#                attribute_paper_size(), validate_counter_consistency(), fetch_first_web_page(),
#                detect_brand()
#                Side effects: network I/O
# @dependencies: internal: config.settings, core, core.cartridge_change_engine, core.database,
#                core.snmp.protocol, core.time_utils | external: requests, urllib3
# @notes:        None.
# =============================================================================

"""
Common functions of all collectors:
- si, ss: safe conversion of SNMP values
- _g: simple snmp_get wrapper with automatic fallback
- _counters_event: recording of print and warning events (preventing early registration and unreasonable large deltas)
- validate_counter_consistency: checking the consistency of counters
- detect_brand: automatic brand detection
"""

import logging
from datetime import datetime, timezone

from config.settings import POLL_INTERVAL, TONER_ALERT_THRESHOLDS
from core.snmp.protocol import snmp_get_with_fallback
from core import store
from core.database import add_event
from core.time_utils import parse_utc, utc_now_iso

log = logging.getLogger("PrinterMonitor")
DEFAULT_YIELD_PER_PAGE = 2000

# Validation settings
# logical maximum delta: base value for 30s interval, then adjusted according to POLL_INTERVAL
_BASE_MAX_PER_30S = 200
MIN_DELTA_FOR_FALLBACK = 1        # Minimum delta for fallback based on total
MAX_TOTAL_AFTER_RESET = 5000      # If the new value is less than this and the previous one was large, it is reset
# Recognizing the replacement of the cartridge from the "pages printed with this cartridge" reset (EWS and...):
# The number is only ascending; A significant drop below the reset ratio means a newly installed cartridge.
CART_ID_RESET_MIN_PAGES = 100     # At least the previous registered pages until the reset is meaningful
CART_ID_RESET_RATIO = 0.5         # New value < 50% of previous value ⇒ reset/replace


def _elapsed_since_prev(prev: dict) -> float:
    """Returns the elapsed time in seconds since the last saved snapshot."""
    updated_at = (prev or {}).get("updated_at")
    if not updated_at:
        return float(POLL_INTERVAL)
    try:
        # Phase E.3: UTC-aware comparison with stored parsed value (Z).
        elapsed = (datetime.now(timezone.utc) - parse_utc(updated_at)).total_seconds()
        return max(1.0, elapsed)
    except Exception:
        return float(POLL_INTERVAL)


def _dynamic_max_reasonable_delta(prev: dict) -> tuple[int, float]:
    """Calculates the logical upper limit of delta based on the actual distance between two polls."""
    elapsed_seconds = max(float(POLL_INTERVAL), _elapsed_since_prev(prev))
    dynamic_limit = max(100, int(_BASE_MAX_PER_30S * (elapsed_seconds / 30.0)))
    return dynamic_limit, elapsed_seconds


# ─── helpers ────────────────────────────────────────────────────
def si(v, d: int = 0) -> int:
    # Bug #5: log errors instead of hiding + only relevant exceptions
    """Convert an SNMP value to int, returning the default d for missing or invalid values."""

    if v is None:
        return d
    try:
        return int(v)
    except (ValueError, TypeError) as e:
        log.warning(f"si() conversion failed: value={v!r} → default={d} ({e})")
        return d
    except Exception as e:
        log.error(f"si() unexpected error: value={v!r} → default={d} ({e})")
        return d


def ss(v, d: str = "N/A") -> str:
    """Convert an SNMP value to a stripped string, returning the default d when it is empty."""

    return str(v).strip() if v is not None and str(v).strip() else d


def _g(ip: str, oid: str, community: str, timeout: float = 2.5):
    """
    Wrapper for snmp_get_with_fallback that tries v2c first and v1 if it fails.
    """
    return snmp_get_with_fallback(ip, oid, community, timeout=timeout)


def _bootstrap_yield_from_history(ip: str, prev: dict):
    """If yield is still default, use historical snapshots for initial estimation."""
    if (prev or {}).get("yield_per_page", DEFAULT_YIELD_PER_PAGE) != DEFAULT_YIELD_PER_PAGE:
        return None
    try:
        from core.database import estimate_yield_from_history
        result = estimate_yield_from_history(ip, days=7, min_points=3, min_pages=500)
        if not result:
            return None
        estimated_yield = result["yield_per_page"]
        store._prev.set(ip, {
            "yield_per_page": estimated_yield,
            "yield_learning_failures": 0,
        })
        log.info(
            f"  [{ip}] historical yield bootstrap -> {estimated_yield} "
            f"(pages={result['total_pages']}, toner_drop={result['total_drop']}, samples={result['sample_points']})"
        )
        return estimated_yield
    except Exception as exc:
        log.exception("Historical yield bootstrap failed for %s: %s", ip, exc)
        return None



def _learn_yield_per_page(ip: str, delta_pages: int, prev_toner_level: int, current_toner_level: int, prev: dict):
    """Automatic yield_per_page learning based on toner consumption and printed pages."""
    prev = prev or {}
    if delta_pages <= 0 or prev_toner_level is None or current_toner_level is None:
        return
    # Don't use questionable/confirming data to learn yield.
    if prev.get("last_counter_error") or prev.get("pending_overflow_total") is not None or prev.get("pending_refill_new_toner") is not None:
        log.debug("  [%s] yield learning skipped due to pending/anomalous counter state", ip)
        return
    # ✅ Circular learning fix: When manual override is active, the toner level that
    # This function is created from the same yield; Learning from it = self validation.
    if prev.get("manual_override"):
        log.debug("  [%s] yield learning skipped while manual override is active", ip)
        return

    current_yield = int(prev.get("yield_per_page", DEFAULT_YIELD_PER_PAGE) or DEFAULT_YIELD_PER_PAGE)
    if current_yield == DEFAULT_YIELD_PER_PAGE:
        bootstrapped = _bootstrap_yield_from_history(ip, prev)
        if bootstrapped:
            current_yield = bootstrapped

    toner_drop = prev_toner_level - current_toner_level
    if toner_drop <= 0:
        return
    # Very small toner drops have a lot of noise; At least 50 pages are required for an acceptable estimate.
    if toner_drop < 1 or delta_pages < 50:
        return

    try:
        estimated_yield = int(round(delta_pages * 100.0 / toner_drop))
    except ZeroDivisionError:
        log.warning("  [%s] yield learning skipped بسبب تقسیم بر صفر", ip)
        return
    except Exception as exc:
        log.exception("  [%s] yield learning error: %s", ip, exc)
        return

    if estimated_yield < 300 or estimated_yield > 20000:
        log.info(f"  [{ip}] yield learning ignored خارج از بازه: {estimated_yield}")
        return

    diff_ratio = abs(estimated_yield - current_yield) / max(current_yield, 1)
    failures = int(prev.get("yield_learning_failures", 0) or 0)
    force_estimate = int(prev.get("force_estimate", 0) or 0)

    if current_yield != DEFAULT_YIELD_PER_PAGE and diff_ratio > 0.30:
        failures += 1
        log.info(
            f"  [{ip}] yield discrepancy detected: current={current_yield}, estimated={estimated_yield}, "
            f"ratio={diff_ratio:.2f}, failures={failures}/10"
        )
        if failures >= 10 and not force_estimate:
            store._prev.set(ip, {
                "force_estimate": 1,
                "yield_learning_failures": failures,
            })
            log.info(f"  [{ip}] force_estimate enabled after repeated yield discrepancies")
        else:
            store._prev.set(ip, {"yield_learning_failures": failures})
        return

    failures = 0
    if current_yield != DEFAULT_YIELD_PER_PAGE:
        if diff_ratio < 0.05:
            return
        estimated_yield = int(round((current_yield * 0.6) + (estimated_yield * 0.4)))

    if estimated_yield == current_yield:
        return

    source = "auto_learn"
    log.info(
        f"  [{ip}] yield_per_page updated: {current_yield} -> {estimated_yield} "
        f"(source={source}, pages={delta_pages}, toner_drop={toner_drop})"
    )
    store._prev.set(ip, {
        "yield_per_page": estimated_yield,
        "yield_learning_failures": failures,
    })


# Manual toner override slots (per-color)
# ✅ "Manual value reset to default" bug fix: previously only a single value slot
# (override_color/override_start_total/override_start_toner) existed; reset
# The new color clears the override of the previous color and after a poll the raw SNMP value
# would return Now each color has its own independent slot in prev["toner_overrides"].
def _normalize_overrides(prev: dict) -> dict:
    """Restore override slots as dict per-color by migrating from the old format.

    New format: {"<color>": {"start_total": int, "start_toner": int},...}
    The old format (single slot) is read only for compatibility with existing states.
    """
    prev = prev or {}
    raw = prev.get("toner_overrides")
    if isinstance(raw, dict) and raw:
        out = {}
        for color, entry in raw.items():
            if isinstance(entry, dict) and entry:
                out[str(color)] = entry
        if out:
            return out
    # Migrating from the old single-bit slot
    if prev.get("manual_override") and prev.get("override_color"):
        return {str(prev["override_color"]): {
            "start_total": prev.get("override_start_total"),
            "start_toner": prev.get("override_start_toner"),
        }}
    return {}


def _overrides_valid_after_total(overrides: dict, total) -> dict:
    """Removing the slots whose device counter has moved back from their starting point (service/reset)."""
    out = {}
    for color, entry in (overrides or {}).items():
        start_total = (entry or {}).get("start_total")
        try:
            if start_total is not None and total is not None and int(total) < int(start_total):
                continue
        except (TypeError, ValueError):
            pass
        out[color] = entry
    return out


def _mirror_legacy_override_keys(new_prev: dict, overrides: dict):
    """Keeping old single-slot keys in sync with the latest slot (backwards compatibility)."""
    if overrides:
        try:
            last_color = next(reversed(overrides))
        except (TypeError, StopIteration):
            last_color = None
        entry = overrides.get(last_color) or {}
        new_prev["manual_override"] = 1
        new_prev["override_color"] = last_color
        new_prev["override_start_total"] = entry.get("start_total")
        new_prev["override_start_toner"] = entry.get("start_toner")
    else:
        new_prev["manual_override"] = 0
        new_prev["override_color"] = None
        new_prev["override_start_total"] = None
        new_prev["override_start_toner"] = None


def get_pages_since_last_reset(prev: dict, total: int):
    """Calculate the number of pages printed since the last cartridge reset.

    With per-color slots, "last reset" means the shortest distance from the starting points
    (the latest active reset).
    """
    overrides = _normalize_overrides(prev)
    if not overrides:
        return None
    best = None
    for _color, entry in overrides.items():
        override_start_total = (entry or {}).get("start_total")
        if override_start_total is None:
            continue
        try:
            pages_since_override = int(total) - int(override_start_total)
        except (TypeError, ValueError):
            continue
        if pages_since_override < 0:
            continue
        best = pages_since_override if best is None else min(best, pages_since_override)
    return best



def apply_toner_override(ip: str, total: int, snmp_level: int = None, color: str = None):
    """Toner level recalculation based on manual override of "same color" and printed pages.

    Each color has an independent slot; Color reset has no effect on this calculation.
    """
    if color is None:
        return None
    prev = store._prev.get(ip) or {}
    overrides = _normalize_overrides(prev)
    entry = overrides.get(color)
    if not entry:
        return None

    override_start_total = entry.get("start_total")
    override_start_toner = entry.get("start_toner")
    if override_start_total is None or override_start_toner is None:
        return None

    # 🔥 If total is less than override_start_total, it means the device
    # reset and this override is no longer valid → return the raw value of the sensor
    try:
        pages_since_override = int(total) - int(override_start_total)
    except (TypeError, ValueError):
        return snmp_level
    if pages_since_override < 0:
        log.debug(f"  [{ip}] Toner override[{color}] invalidated: total({total}) < start({override_start_total}). "
                  f"Returning raw SNMP level: {snmp_level}")
        return snmp_level

    if pages_since_override == 0:
        return override_start_toner

    yield_per_page = prev.get("yield_per_page", DEFAULT_YIELD_PER_PAGE)
    if not isinstance(yield_per_page, int) or yield_per_page <= 0:
        yield_per_page = DEFAULT_YIELD_PER_PAGE

    estimated_drop = int(round(pages_since_override * 100.0 / yield_per_page))
    final_level = max(0, min(100, override_start_toner - estimated_drop))

    log.debug(f"  [{ip}] apply_toner_override: override_color={color}, total={total}, "
              f"start_total={override_start_total}, start_toner={override_start_toner}, "
              f"yield_per_page={yield_per_page}, pages_since_reset={pages_since_override}, final={final_level}")
    return final_level


# Events
def _counters_event(ip: str, total: int, prev: dict, alerts: list, curr_codes: list,
                    full_color: int = None, black_white: int = None,
                    paper_size: str = None, username: str = None,
                    current_toner_level: int = None, prev_toner_level: int = None,
                    uptime: int = None,
                    a3_total: int = None, a4_total: int = None,
                    poll_timestamp: str = None,
                    paper_split: dict = None,
                    paper_detail: dict = None, func_split: dict = None,
                    toner_levels: dict = None, legacy_toner_color: str = None,
                    cartridge_ids: dict = None, cartridge_supply_pages: dict = None,
                    signal_quality: dict = None,
                    supply_names: dict = None):
    """
    Log print/alert events with protection against SNMP suspicious data.

    Important principle: if counter is suspicious, we don't store it as truth in PrevStore;
    First, the reading error or anomaly is registered rather than the false log chain
    COUNTER_RESET → PRINT_OVERFLOW is prevented.
    """
    prev = prev or {}
    alerts = alerts or []
    curr_codes = curr_codes or []
    cart_id_changed_colors = set()
    cart_id_merged = dict((prev.get("cartridge_ids") or {})) if isinstance(prev.get("cartridge_ids"), dict) else {}
    cart_pages_merged = dict((prev.get("cart_supply_pages") or {})) if isinstance(prev.get("cart_supply_pages"), dict) else {}

    try:
        from core.cartridge_change_engine import evaluate_cartridge_change
    except Exception:
        evaluate_cartridge_change = None

    def _to_int(value, default=None):
        """Convert a value to int, returning the default when conversion fails."""
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _uptime_reset(curr, old) -> bool:
        """Return True when the device uptime went backwards, meaning the counters were reset."""
        curr_i = _to_int(curr)
        old_i = _to_int(old)
        # sysUpTime is in hundredths of seconds; We leave a margin of error of 60 seconds.
        # ✅ False reset bug fix: if reading uptime is unsuccessful (None or 0), never
        # Don't consider it a "reboot". Actual evidence: 276 out of 385 COUNTER_RESET
        # It was logged with current_uptime=0 (SNMP read failed).
        if curr_i is None or curr_i <= 0 or old_i is None or old_i <= 0:
            return False
        return curr_i < old_i - 60 * 100

    total = _to_int(total)
    prev_total = _to_int(prev.get("print_total")) if prev else None
    prev_fc = _to_int(prev.get("full_color")) if prev.get("full_color") is not None else None
    prev_bw = _to_int(prev.get("black_white")) if prev.get("black_white") is not None else None
    prev_uptime = prev.get("uptime") if prev else None
    prev_toner_level = prev_toner_level if prev_toner_level is not None else (prev.get("toner_level") if prev else None)
    reboot_detected = _uptime_reset(uptime, prev_uptime)
    dynamic_max_delta, elapsed_seconds = _dynamic_max_reasonable_delta(prev)

    if total is None:
        add_event(ip, "SNMP_COUNTER_READ_ERROR", {
            "message": "خواندن شمارنده کل نامعتبر بود (total=None)؛ snapshot قبلی حفظ شد",
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": None,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
        })
        snapshot = {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "reset_prev_total": prev.get("reset_prev_total"),
        }
        if cartridge_ids is not None:
            snapshot["cartridge_ids"] = dict((cartridge_ids or {}))
        if cartridge_supply_pages is not None:
            snapshot["cart_supply_pages"] = dict((cartridge_supply_pages or {}))
        store._prev.set(ip, snapshot)
        return

    # ─── Recording of new alarm events by preventing repetition ────────────────
    suppress_toner_alerts = False
    if prev.get("manual_override") and prev.get("override_start_toner") is not None:
        try:
            current_level_int = int(current_toner_level) if current_toner_level is not None else None
            override_start = int(prev.get("override_start_toner"))
            if current_level_int is not None and current_level_int > TONER_ALERT_THRESHOLDS.get("warning", 15) and current_level_int >= override_start - 1:
                suppress_toner_alerts = True
        except (TypeError, ValueError):
            suppress_toner_alerts = False

    if curr_codes and not suppress_toner_alerts:
        alert_codes_list = prev.get("last_alert_codes", []) if prev else []
        new_codes = [c for c in curr_codes if c not in alert_codes_list]
        for code in new_codes:
            msg = next((a.get("message") for a in alerts if a.get("code") == code), f"Error {code}")
            add_event(ip, "ALERT", {"message": msg, "code": code, "severity": "warning"})

    # ─── First poll: get baseline, don't print log ───────────────────────
    if prev_total is None:
        log.warning(f"  [{ip}] جلوگیری از ثبت رویداد PRINT در اولین poll (total={total:,})")
        baseline = {
            "print_total": total,
            "toner_level": current_toner_level,
            "full_color": full_color,
            "black_white": black_white,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime,
            "a3_total": a3_total,
            "a4_total": a4_total,
            "pending_overflow_total": None,
            "pending_refill_new_toner": None,
        }
        if cartridge_ids is not None:
            baseline["cartridge_ids"] = dict((cartridge_ids or {}))
        if cartridge_supply_pages is not None:
            baseline["cart_supply_pages"] = dict((cartridge_supply_pages or {}))
        if supply_names is not None:
            baseline["supply_names"] = dict((supply_names or {}))
        if toner_levels is not None:
            baseline["toner_levels"] = dict((toner_levels or {}))
        if cartridge_ids is not None or cartridge_supply_pages is not None or toner_levels is not None or supply_names is not None:
            candidate_colors = set()
            for mapping in (cartridge_ids, cartridge_supply_pages, toner_levels, supply_names):
                if isinstance(mapping, dict):
                    candidate_colors.update(mapping.keys())
            if legacy_toner_color:
                candidate_colors.add(legacy_toner_color)
            state_by_color = {}
            for color in sorted(candidate_colors):
                state_by_color[color] = {
                    "printer_ip": ip,
                    "brand": prev.get("brand") or "",
                    "model": prev.get("model") or "",
                    "color": color,
                    "chip_id": (cartridge_ids or {}).get(color) if isinstance(cartridge_ids, dict) else None,
                    "serial": (cartridge_ids or {}).get(color) if isinstance(cartridge_ids, dict) else None,
                    "cartridge_model": (supply_names or {}).get(color) if isinstance(supply_names, dict) else None,
                    "supply_description": (supply_names or {}).get(color) if isinstance(supply_names, dict) else None,
                    "supply_counter": (cartridge_supply_pages or {}).get(color) if isinstance(cartridge_supply_pages, dict) else None,
                    "page_counter": total,
                    "remaining_level": (toner_levels or {}).get(color) if isinstance(toner_levels, dict) else None,
                    "supply_life": (toner_levels or {}).get(color) if isinstance(toner_levels, dict) else None,
                    "generation": None,
                }
            baseline["cartridge_state"] = state_by_color
        store._prev.set(ip, baseline)
        return

    # ─── Main guard: total=0 or reduce counter without reboot = SNMP error, not reset ───
    suspicious_zero = total == 0 and prev_total >= 1000 and not reboot_detected
    suspicious_drop = total < prev_total and not reboot_detected
    if suspicious_zero or suspicious_drop:
        etype = "SNMP_COUNTER_READ_ERROR" if suspicious_zero else "COUNTER_ANOMALY"
        add_event(ip, etype, {
            "message": (
                f"خواندن شمارنده مشکوک بود: قبلی {prev_total:,} → جدید {total:,}. "
                "uptime ریست نشده؛ snapshot قبلی حفظ شد."
            ),
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": False,
        })
        log.warning("  [%s] suspicious counter read ignored: prev=%s current=%s uptime %s→%s", ip, prev_total, total, prev_uptime, uptime)
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "a3_total": prev.get("a3_total"),
            "a4_total": prev.get("a4_total"),
            "last_counter_error": etype,
            "reset_prev_total": prev.get("reset_prev_total"),
        })
        return

    # ─── real reset only with strong evidence ────────────────
    # Conditions: (1) the counter is too small (reboot the counter usually
    # Stores in NVRAM; The actual reset of the counter happens only with service/factory reset),
    # (2) the uptime is really low, (3) the previous counter is significantly large.
    # Otherwise, it's a readable anomaly and we just warn and save the snapshot.
    strong_reset_evidence = (
        total < MAX_TOTAL_AFTER_RESET
        and prev_total >= MIN_DELTA_FOR_FALLBACK * 1000
        and reboot_detected
    )
    if total < prev_total and strong_reset_evidence:
        add_event(ip, "COUNTER_RESET", {
            "message": f"شمارنده از {prev_total:,} به {total:,} کاهش یافت (ریبوت دستگاه)",
            "severity": "error",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": True,
        })
        store._prev.set(ip, {
            "print_total": total,
            "toner_level": current_toner_level,
            "full_color": full_color,
            "black_white": black_white,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime,
            "a3_total": a3_total,
            "a4_total": a4_total,
            "reset_prev_total": prev_total,
            # ✅ Fix: reset the learned yield counter and manually adjust the toner
            # does not destroy Per-color slots are only removed when the counter
            # really gone to less than their starting point.
            "yield_per_page": prev.get("yield_per_page", 2000),
            "toner_overrides": _overrides_valid_after_total(_normalize_overrides(prev), total),
        })
        return

    if total < prev_total and reboot_detected and not strong_reset_evidence:
        # Decreasing counter with reboot but no strong evidence of actual reset (SNMP sources
        # different/imperfect reading). We only record the anomaly and save the snapshot.
        add_event(ip, "COUNTER_ANOMALY", {
            "message": (
                f"کاهش شمارنده ({prev_total:,} → {total:,}) با ریبوتِ دستگاه ولی "
                "بدون شواهد ریست واقعی شمارنده؛ snapshot قبلی حفظ شد"
            ),
            "severity": "warning",
            "prev_total": prev_total,
            "current_total": total,
            "delta": total - prev_total,
            "prev_uptime": prev_uptime,
            "current_uptime": uptime,
            "is_reboot": True,
        })
        store._prev.set(ip, {
            "print_total": prev_total,
            "full_color": prev_fc,
            "black_white": prev_bw,
            "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
            "alert_codes": curr_codes,
            "last_alert_codes": curr_codes,
            "uptime": uptime if uptime is not None else prev_uptime,
            "a3_total": prev.get("a3_total"),
            "a4_total": prev.get("a4_total"),
            "last_counter_error": "COUNTER_ANOMALY",
            "reset_prev_total": prev.get("reset_prev_total"),
        })
        return

    actual_delta = total - prev_total
    delta_pages = actual_delta if actual_delta >= 0 else 0

    # ─── counter return after reboot — real bug 172.16.25.36 (e-STUDIO257) ───
    # This model reports total=0 for a few minutes immediately after the reboot, and then the amount
    # Returns permanent NVRAM (actual view: 20,575→0→20,576). Previous versions
    # The return was considered a "new print" and recorded a huge PRINT_GAP (61,813
    # Estimated page in 6 days!). Rule: if the last reset is registered (reset_prev_total)
    # and the counter returns near the same reference, this is the counter return, not the print —
    # The information event COUNTER_RESTORED is registered and we continue normally with the new baseline.
    reset_ref = _to_int(prev.get("reset_prev_total")) if prev.get("reset_prev_total") is not None else None
    if reset_ref is not None and total is not None and total > 0:
        restore_tol = max(200, int(reset_ref * 0.005))
        if reset_ref - 10 <= total <= reset_ref + restore_tol:
            add_event(ip, "COUNTER_RESTORED", {
                "message": f"شمارنده پس از ریبوت به مقدار دائمی بازگشت ({total:,})؛ به‌عنوان چاپ جدید محاسبه نمی‌شود",
                "severity": "info",
                "prev_total": prev_total,
                "restored_total": total,
                "reset_reference": reset_ref,
            })
            log.warning("  [%s] counter restored after reboot: %s→%s (not printing; baseline=%s)",
                        ip, prev_total, total, reset_ref)
            store._prev.set(ip, {
                "print_total": total,
                "full_color": full_color if full_color is not None else prev_fc,
                "black_white": black_white if black_white is not None else prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": a3_total if a3_total is not None else prev.get("a3_total"),
                "a4_total": a4_total if a4_total is not None else prev.get("a4_total"),
                "pending_overflow_total": None,
                "pending_overflow_delta": None,
                "reset_prev_total": None,
                "last_counter_error": None,
            })
            return
        if total > reset_ref + restore_tol:
            if prev_total is not None and prev_total <= reset_ref - 10:
                # ✅ double-count fix after reboot (actual case 20575 → 0 → 21000):
                # The snapshot chain has never reached the reference band since the reset and the counter
                # Jumped to the top of it at once = signature "Return NVRAM + actual print in
                # "Reboot interval", not print from zero. So the actual baseline before
                # It is not discarded from the reset (reset_ref); Delta relative to the same reference
                # Calculated (real_delta = total - reset_ref = 425 not 21000) and
                # LOW NORMAL FLOW (PRINT/OVERFLOW/TONER LEARNING) with the same reference
                # Modified continues — so we have no phantom pages, no double counting.
                # Gradual growth (actual reset that slowly approaches the reference) enters this
                # It does not branch and has the previous behavior.
                real_delta = total - reset_ref
                add_event(ip, "COUNTER_RESTORED", {
                    "message": f"شمارنده پس از ریبوت به {total:,} بازگشت ({real_delta:,} صفحه چاپ واقعی در فاصله‌ی ریبوت)",
                    "severity": "info",
                    "prev_total": prev_total,
                    "restored_total": total,
                    "reset_reference": reset_ref,
                    "real_delta": real_delta,
                })
                log.warning("  [%s] counter restored above reset reference: baseline=%s real_delta=%s",
                            ip, reset_ref, real_delta)
                prev_total = reset_ref
                actual_delta = real_delta
                delta_pages = real_delta if real_delta > 0 else 0
            # Return band passed (actual print after reset) — remove flag and continue
            reset_ref = None

    # ─── Toner surface anti-noise (bottom of single-layer jump)
    # ✅ Actual data: TOSHIBA e-STUDIO306 on 172.16.0.40 — true black level 100%
    # It was stable (Ran 44/15/280 Pali), but every now and then *a* poll reads 8%
    # and again 100%; Each blip creates a dummy REFILL candidate. Drop >25 units in
    # A poll with <5 print pages is physically impossible (multi-thousand page cartridges)
    # So such a reading is noise: the previous value is kept and entered into the logic
    # REFILL/learning does not yield.
    if (prev_toner_level is not None and current_toner_level is not None
            and 0 <= actual_delta < 5):
        try:
            if int(prev_toner_level) - int(current_toner_level) > 25:
                log.info("  [%s] خواندن نویزی تونر رد شد: %s%% → %s%% با %s صفحه چاپ (مقدار قبلی نگه داشته شد)",
                         ip, prev_toner_level, current_toner_level, actual_delta)
                current_toner_level = prev_toner_level
        except (TypeError, ValueError):
            pass

    # ─── Generic evidence-based cartridge detection ─────────────────────
    if cartridge_ids is not None or cartridge_supply_pages is not None:
        prev_ids = prev.get("cartridge_ids") or {}
        prev_pages = prev.get("cart_supply_pages") or {}
        if not isinstance(prev_ids, dict):
            prev_ids = {}
        if not isinstance(prev_pages, dict):
            prev_pages = {}
        cart_id_merged = dict(prev_ids)
        cart_pages_merged = dict(prev_pages)
        if cartridge_ids is not None:
            for ck, raw_sid in (cartridge_ids or {}).items():
                try:
                    sid = str(raw_sid).strip()
                except Exception:
                    continue
                if sid:
                    cart_id_merged[ck] = sid
        if cartridge_supply_pages is not None:
            for ck, val in (cartridge_supply_pages or {}).items():
                try:
                    cur_p = int(val)
                except (TypeError, ValueError):
                    continue
                cart_pages_merged[ck] = cur_p

    if evaluate_cartridge_change is not None and not prev.get("manual_override"):
        prev_cartridge_state = prev.get("cartridge_state") or {}
        if not isinstance(prev_cartridge_state, dict):
            prev_cartridge_state = {}
        prev_ids = prev.get("cartridge_ids") or {}
        prev_pages = prev.get("cart_supply_pages") or {}
        prev_names = prev.get("supply_names") or {}
        prev_levels = prev.get("toner_levels") or {}
        if not isinstance(prev_ids, dict):
            prev_ids = {}
        if not isinstance(prev_pages, dict):
            prev_pages = {}
        if not isinstance(prev_names, dict):
            prev_names = {}
        if not isinstance(prev_levels, dict):
            prev_levels = {}
        current_cartridge_state = {}
        cart_id_changed_colors = set()
        candidate_colors = set()
        for color in (cartridge_ids or {}):
            candidate_colors.add(color)
        for color in (cartridge_supply_pages or {}):
            candidate_colors.add(color)
        for color in (toner_levels or {}):
            candidate_colors.add(color)
        for color in (supply_names or {}):
            candidate_colors.add(color)
        if legacy_toner_color:
            candidate_colors.add(legacy_toner_color)
        cid_colors = {"black": "مشکی", "cyan": "آبی", "magenta": "سرخابی", "yellow": "زرد"}
        for color in sorted(candidate_colors):
            if color not in cid_colors and color != legacy_toner_color:
                continue
            prev_slot = prev_cartridge_state.get(color) or {
                "printer_ip": ip,
                "color": color,
                "chip_id": prev_ids.get(color),
                "serial": prev_ids.get(color),
                "cartridge_model": prev_names.get(color),
                "supply_description": prev_names.get(color),
                "supply_counter": prev_pages.get(color),
                "page_counter": prev.get("print_total"),
                "remaining_level": prev_levels.get(color),
                "supply_life": prev_levels.get(color),
            }
            curr_slot = {
                "printer_ip": ip,
                "brand": prev.get("brand") or "",
                "model": prev.get("model") or "",
                "color": color,
                "chip_id": (cartridge_ids or {}).get(color) if isinstance(cartridge_ids, dict) else None,
                "serial": (cartridge_ids or {}).get(color) if isinstance(cartridge_ids, dict) else None,
                "cartridge_model": (supply_names or {}).get(color) if isinstance(supply_names, dict) else None,
                "supply_description": (supply_names or {}).get(color) if isinstance(supply_names, dict) else None,
                "supply_counter": (cartridge_supply_pages or {}).get(color),
                "page_counter": total,
                "remaining_level": (
                    toner_levels.get(color)
                    if isinstance(toner_levels, dict)
                    else (current_toner_level if color == legacy_toner_color else None)
                ),
                "supply_life": (
                    toner_levels.get(color)
                    if isinstance(toner_levels, dict)
                    else (current_toner_level if color == legacy_toner_color else None)
                ),
                "generation": prev_slot.get("generation") if isinstance(prev_slot, dict) else None,
                "signal_quality": (signal_quality or {}).get(color) if isinstance(signal_quality, dict) else None,
            }
            if curr_slot.get("supply_counter") is None and prev_slot.get("supply_counter") is not None:
                curr_slot["supply_counter"] = prev_slot.get("supply_counter")
            if curr_slot.get("remaining_level") is None and prev_slot.get("remaining_level") is not None:
                curr_slot["remaining_level"] = prev_slot.get("remaining_level")
            if curr_slot.get("supply_life") is None and prev_slot.get("supply_life") is not None:
                curr_slot["supply_life"] = prev_slot.get("supply_life")
            if curr_slot.get("chip_id") is None and prev_slot.get("chip_id") is not None:
                curr_slot["chip_id"] = prev_slot.get("chip_id")
            if curr_slot.get("serial") is None and prev_slot.get("serial") is not None:
                curr_slot["serial"] = prev_slot.get("serial")
            if curr_slot.get("cartridge_model") is None and prev_slot.get("cartridge_model") is not None:
                curr_slot["cartridge_model"] = prev_slot.get("cartridge_model")
            if curr_slot.get("supply_description") is None and prev_slot.get("supply_description") is not None:
                curr_slot["supply_description"] = prev_slot.get("supply_description")
            result = evaluate_cartridge_change(prev_slot, curr_slot, reboot_detected=bool(reboot_detected), poll_gap_ok=True)
            if result.get("should_emit"):
                event = result.get("event") or {}
                event_payload = {
                    "message": event.get("old_identity") and f"Cartridge change detected for {color}: {event.get('old_identity')} -> {event.get('new_identity')}" or "Cartridge change detected",
                    "severity": "info",
                    "auto_detected": True,
                    "confirmed": result.get("confidence") in {"CONFIRMED", "PROBABLE"},
                    "confidence": result.get("confidence"),
                    "detection": result.get("detection_method") or "generic_evidence",
                    "detection_method": result.get("detection_method"),
                    "detection_methods": result.get("detection_methods", []),
                    "color": color,
                    "color_fa": cid_colors.get(color, color),
                    "printer_ip": ip,
                    "brand": prev.get("brand") or "",
                    "model": prev.get("model") or "",
                    "old_identity": result.get("old_identity"),
                    "new_identity": result.get("new_identity"),
                    "evidence": result.get("evidence"),
                    "evidence_source": event.get("evidence_source"),
                    "timestamp": event.get("timestamp"),
                    "event_id": event.get("event_id"),
                }
                if result.get("detection_method") == "chip_id":
                    event_payload["prev_cartridge_id"] = result.get("old_identity")
                    event_payload["cartridge_id"] = result.get("new_identity")
                add_event(ip, "CARTRIDGE_CHANGED", event_payload)
                cart_id_changed_colors.add(color)
                current_cartridge_state[color] = {**curr_slot, "event_id": event_payload.get("event_id"), "confidence": result.get("confidence"), "last_seen_at": event_payload.get("timestamp")}
            else:
                current_cartridge_state[color] = {**curr_slot, "confidence": result.get("confidence"), "last_seen_at": utc_now_iso()}
        if current_cartridge_state:
            prev["cartridge_state"] = current_cartridge_state
            pending_map = prev.get("cart_change_pending") or {}
            if isinstance(pending_map, dict):
                for color in cart_id_changed_colors:
                    pending_map.pop(color, None)
                prev["cart_change_pending"] = pending_map or None
        cart_id_changed_colors = set(cart_id_changed_colors)
    else:
        cart_id_changed_colors = set()
        cid_colors = {"black": "مشکی", "cyan": "آبی", "magenta": "سرخابی", "yellow": "زرد"}
        if cartridge_ids is not None or cartridge_supply_pages is not None:
            prev_ids = prev.get("cartridge_ids") or {}
            prev_pages = prev.get("cart_supply_pages") or {}
            if not isinstance(prev_ids, dict):
                prev_ids = {}
            if not isinstance(prev_pages, dict):
                prev_pages = {}
            if not prev.get("manual_override"):
                for ck, raw_sid in (cartridge_ids or {}).items():
                    if ck not in cid_colors:
                        continue
                    try:
                        sid = str(raw_sid).strip()
                    except Exception:
                        continue
                    if not sid:
                        continue
                    cart_id_merged[ck] = sid
                    prev_sid = prev_ids.get(ck)
                    if prev_sid and sid != prev_sid:
                        sq = (signal_quality or {}).get(ck)
                        if sq == "static":
                            log.debug("  [%s] cartridge ID changed for %s but quality=static — skipping event", ip, ck)
                        else:
                            cart_id_changed_colors.add(ck)
                            add_event(ip, "CARTRIDGE_CHANGED", {
                                "message": (f"تشخیص قطعی تعویض کارتریج {cid_colors[ck]}: "
                                            f"شناسه‌ی تراشه تغییر کرد ({prev_sid} → {sid})"),
                                "severity": "info",
                                "auto_detected": True, "confirmed": True,
                                "detection": "chip_id",
                                "color": ck, "color_fa": cid_colors[ck],
                                "prev_cartridge_id": prev_sid, "cartridge_id": sid,
                            })
                for ck, val in (cartridge_supply_pages or {}).items():
                    if ck not in cid_colors:
                        continue
                    try:
                        cur_p = int(val)
                    except (TypeError, ValueError):
                        continue
                    cart_pages_merged[ck] = cur_p
                    try:
                        prev_p = int(prev_pages[ck]) if prev_pages.get(ck) is not None else None
                    except (TypeError, ValueError):
                        prev_p = None
                    if (prev_p is not None and prev_p >= CART_ID_RESET_MIN_PAGES
                            and cur_p < prev_p * CART_ID_RESET_RATIO):
                        cart_id_changed_colors.add(ck)
                        add_event(ip, "CARTRIDGE_CHANGED", {
                            "message": (f"تشخیص تعویض کارتریج {cid_colors[ck]}: شمارش «صفحات "
                                        f"با کارتریج» ریست شد ({prev_p:,} → {cur_p:,})"),
                            "severity": "info",
                            "auto_detected": True, "confirmed": True,
                            "detection": "supply_pages_reset",
                            "color": ck, "color_fa": cid_colors[ck],
                            "prev_supply_pages": prev_p, "supply_pages": cur_p,
                        })
            if cart_id_changed_colors:
                pend_prev = prev.get("cart_change_pending")
                if isinstance(pend_prev, dict):
                    for ck in cart_id_changed_colors:
                        pend_prev.pop(ck, None)
                if legacy_toner_color in cart_id_changed_colors:
                    prev["pending_refill_new_toner"] = None
                    prev["pending_refill_prev_toner"] = None
                    prev["pending_refill_total"] = None
                    prev["pending_refill_hits"] = None

    # ─── Change detection based on changing the name of supply (가도 你你不电视) ─────────
    # Rename cartridge when Chip ID is not available or its quality is static
    # (eg HP 87A → CC388A) is a strong switching signal.
    # Only checks for colors that have not already been chip_id_changed
    # So that the event is not repeated.
    prev_supply_names = prev.get("supply_names") or {}
    prev_name_pending = prev.get("supply_name_pending") or {}
    if supply_names and not prev.get("manual_override"):
        for ck, cur_name in supply_names.items():
            if ck not in cid_colors:
                continue
            if not cur_name:
                continue
            norm_cur = cur_name.strip().lower()
            prev_name = prev_supply_names.get(ck)
            if not prev_name:
                continue
            norm_prev = prev_name.strip().lower()
            # Only when the name has really changed
            if norm_cur == norm_prev:
                # Clear the same name → pending
                if prev_name_pending.get(ck):
                    prev_name_pending.pop(ck, None)
                continue
            # Ignore if chip_id event already registered for this color
            if ck in cart_id_changed_colors:
                continue
            # If the genuine chip_id is enabled, the name change is not important
            cid = (cartridge_ids or {}).get(ck, "")
            sq = (signal_quality or {}).get(ck)
            if cid and sq == "genuine":
                continue
            # Consistency Gateway: The new name must be the same for 2 consecutive polls
            pending_info = prev_name_pending.get(ck)
            if pending_info and pending_info.get("name") == norm_cur:
                # Confirmed — Log event
                prev_name_pending.pop(ck, None)
                cart_id_changed_colors.add(ck)
                add_event(ip, "CARTRIDGE_CHANGED", {
                    "message": (f"تشخیص تعویض کارتریج {cid_colors[ck]}: "
                                f"نام supply تغییر کرد ({prev_name} → {cur_name})"),
                    "severity": "info",
                    "auto_detected": True, "confirmed": True,
                    "detection": "supply_name_change",
                    "color": ck, "color_fa": cid_colors[ck],
                    "prev_supply_name": prev_name, "supply_name": cur_name,
                })
            else:
                # First view change — record pending
                prev_name_pending[ck] = {"name": norm_cur, "prev_name": prev_name}

    # ─── TWO-STEP AUTO REFILL: Don't register with a definite poll ─────────
    refill_confirmed = False
    pending_refill_new = prev.get("pending_refill_new_toner")
    pending_refill_prev = prev.get("pending_refill_prev_toner")
    pending_refill_total = prev.get("pending_refill_total", prev_total)
    pending_refill_hits = int(prev.get("pending_refill_hits") or 0)
    if current_toner_level is not None and pending_refill_new is not None and not prev.get("manual_override"):
        try:
            pages_since_candidate = total - int(pending_refill_total or prev_total)
            if int(current_toner_level) >= int(pending_refill_new) - 1 and pages_since_candidate < 50:
                pending_refill_hits += 1
                # ✅ FLAP fix (real data: TOSHIBA e-STUDIO306 on 172.16.0.40/41
                # With intermittent jumps of 8↔100 in back polls → 48 fake REFILL in 6 days):
                # One back-to-back verification is not enough because the noise is sometimes high
                # stands The new level must be stable for 2 consecutive polls before charging
                # Confirm (delay ≈ few minutes — no charge for actual charge).
                if pending_refill_hits >= 2:
                    refill_confirmed = True
                    add_event(ip, "REFILL", {
                        "message": f"تشخیص خودکار تاییدشده: کارتریج شارژ شد (تونر از {pending_refill_prev}% به {current_toner_level}%)",
                        "severity": "info",
                        "auto_detected": True,
                        "confirmed": True,
                        "prev_toner": pending_refill_prev,
                        "new_toner": current_toner_level,
                        "delta_pages": max(0, pages_since_candidate),
                    })
        except (TypeError, ValueError):
            refill_confirmed = False

    if (not refill_confirmed and current_toner_level is not None and prev_toner_level is not None and actual_delta >= 0):
        try:
            delta_toner = int(current_toner_level) - int(prev_toner_level)
        except (TypeError, ValueError):
            delta_toner = 0
        if (delta_toner > 20 and delta_pages < 50 and not prev.get("manual_override")):
            log.info("  [%s] refill candidate pending: toner %s%% → %s%%", ip, prev_toner_level, current_toner_level)

        _learn_yield_per_page(ip, delta_pages, prev_toner_level, current_toner_level, prev)

    if (delta_pages > 500 and current_toner_level is not None and prev_toner_level is not None and
        abs(int(current_toner_level) - int(prev_toner_level)) < 5):
        add_event(ip, "WARNING", {
            "message": f"هشدار: {delta_pages} صفحه چاپ شده ولی تونر تغییر نکرده ({prev_toner_level}% → {current_toner_level}%). احتمال گیر کردن چیپ.",
            "severity": "warning",
            "auto_detected": True,
        })

    # ─── Automatic detection of cartridge replacement/charging for other colors (CMY and...) ────
    # REFILL path above only "one" toner (the toner given to current_toner_level,
    # usually black). This block means anti-flap dipole
    # (candidate in mutation >20% → level stability in 2 consecutive polls → event) for
    # The rest of the colors run until the replacement of the cyan/magenta/yellow cartridge in the events
    # be registered It only works on real data (raw_level); Demonstration estimates
    # (canon_status_code and...) are not given to this block at all. Legacy color too
    # It is left out so that a physical replacement does not register two events (REFILL +
    # CARTRIDGE_CHANGED). Status keys like pending_refill in memory only
    # (PrevStore cache) are kept and after restart a new window starts
    # It becomes — the same existing policy.
    cart_merged_levels = None
    cart_pending_next = None
    if toner_levels is not None:
        fa_colors = {"black": "مشکی", "cyan": "آبی", "magenta": "سرخابی", "yellow": "زرد"}
        prev_levels = prev.get("toner_levels") or {}
        pendings_prev = prev.get("cart_change_pending") or {}
        if not isinstance(pendings_prev, dict):
            pendings_prev = {}
        cart_merged_levels = dict(prev_levels)
        cart_pending_next = {}
        for color_key, cur_val in toner_levels.items():
            if color_key not in fa_colors or color_key == legacy_toner_color:
                continue
            if color_key in cart_id_changed_colors:
                # The replacement of the same color of the same poll was recorded with definitive evidence (chip/reset);
                # A level jump candidate should not be made for him so that the second event does not come.
                continue
            try:
                cur = int(cur_val)
            except (TypeError, ValueError):
                continue
            cart_merged_levels[color_key] = cur
            pend = pendings_prev.get(color_key)
            if pend and not prev.get("manual_override") and total is not None:
                # Stability window: the level of the candidate should remain high and the pages should be less than the candidate
                try:
                    pend_new = int(pend.get("new"))
                    pend_total = int(pend.get("total", prev_total))
                    pages_since = total - pend_total
                except (TypeError, ValueError):
                    pages_since = None
                if pages_since is not None and pages_since < 50 and cur >= pend_new - 1:
                    hits = int(pend.get("hits") or 0) + 1
                    if hits >= 2:
                        add_event(ip, "CARTRIDGE_CHANGED", {
                            "message": (f"تشخیص خودکار تاییدشده: کارتریج {fa_colors[color_key]}"
                                        f" تعویض یا شارژ شد (سطح تونر از {pend.get('prev')}% به {cur}%)"),
                            "severity": "info",
                            "auto_detected": True,
                            "confirmed": True,
                            "detection": "toner_jump",
                            "color": color_key,
                            "color_fa": fa_colors[color_key],
                            "prev_toner": pend.get("prev"),
                            "new_toner": cur,
                            "pages": max(0, pages_since),
                        })
                        continue  # confirmed; pending is closed (the event is a shot)
                    cart_pending_next[color_key] = {**pend, "hits": hits}
                    continue
                if pages_since is None or cur < pend_new - 1:
                    # Level returned below candidate (flap/bleep) or inconsistent counter data → invalid candidate
                    continue
                # The pages from the candidate are increased but the level remains high: pending is maintained (legacy behavior)
                cart_pending_next[color_key] = pend
                continue
            # New Candidate: level jump compared to the last known level of the same color
            try:
                prev_lvl = int(prev_levels[color_key]) if prev_levels.get(color_key) is not None else None
            except (TypeError, ValueError):
                prev_lvl = None
            if (prev_lvl is not None and cur - prev_lvl > 20 and delta_pages < 50
                    and not prev.get("manual_override")):
                log.info(f"  [{ip}] cartridge-change candidate ({color_key}): toner {prev_lvl}% → {cur}%")
                cart_pending_next[color_key] = {"prev": prev_lvl, "new": cur, "total": total, "hits": 0}

    # ─── Calculation and validation of color/black and white deltas ────────────────
    delta_fc = (full_color - prev_fc) if (full_color is not None and prev_fc is not None) else 0
    delta_bw = (black_white - prev_bw) if (black_white is not None and prev_bw is not None) else 0
    split_delta = delta_fc + delta_bw
    total_delta = split_delta
    color_unknown = False
    counter_mismatch = False

    if actual_delta > 0:
        partial_negative = delta_fc < 0 or delta_bw < 0
        mismatch = split_delta > 0 and abs(split_delta - actual_delta) > max(2, int(actual_delta * 0.10))
        if partial_negative or mismatch or split_delta <= 0:
            if actual_delta <= dynamic_max_delta:
                total_delta = actual_delta
                split_unavailable = full_color is None and black_white is None
                color_unknown = partial_negative or mismatch or split_unavailable
                counter_mismatch = partial_negative or mismatch
                delta_fc = 0
                delta_bw = actual_delta
                if counter_mismatch:
                    log.warning(
                        "  [%s] split counter mismatch; using total delta. actual=%s split=%s fc=%s bw=%s",
                        ip, actual_delta, split_delta, delta_fc, delta_bw,
                    )
            else:
                total_delta = actual_delta
    else:
        total_delta = 0

    log.debug(f"  [{ip}] PRINT: prev_total={prev_total:,} curr_total={total:,} "
              f"delta_fc={delta_fc}, delta_bw={delta_bw}, total_delta={total_delta}, actual_delta={actual_delta}")

    # ─── Overflow: first time only pending, second time confirmation and estimated registration ───
    estimated = False
    if total_delta > dynamic_max_delta:
        pending_total = prev.get("pending_overflow_total")
        pending_delta = prev.get("pending_overflow_delta")
        if pending_total == total and pending_delta == total_delta:
            estimated = True
            color_unknown = True
            log.warning("  [%s] overflow confirmed on second poll; recording estimated PRINT delta=%s", ip, total_delta)
            # Fix: Estimated overflow event (usually due to monitor failure or
            # Fluctuation source counter) should not be included in the daily consumption statistics as a normal print
            # gathered; A separate PRINT_GAP type is registered and the snapshot is advanced.
            gap_paper = paper_size
            add_event(ip, "PRINT_GAP", {
                "message": (
                    f"جبران فاصله پایش: {total_delta} صفحه نسبت به آخرین snapshot "
                    "(احتمالاً چاپ در زمان قطعی مانیتور یا نوسان خواندن شمارنده؛ تخمینی)"
                ),
            "pages": total_delta,
            "color": "نامشخص",
            "paper_size": gap_paper,
            "severity": "warning",
            "estimated": True,
            "overflow_confirmed": True,
            "gap_backfill": True,
            **({"paper_split": paper_split} if paper_split and (paper_split.get("large") or paper_split.get("small")) else {}),
            **({"paper_detail": paper_detail} if paper_detail else {}),
            **({"func_split": func_split} if func_split else {}),
                "prev_total": prev_total,
                "current_total": total,
                "dynamic_limit": dynamic_max_delta,
                **({"poll_timestamp": poll_timestamp} if poll_timestamp else {}),
            })
            store._prev.set(ip, {
                "print_total": total,
                "full_color": full_color if full_color is not None else prev_fc,
                "black_white": black_white if black_white is not None else prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": a3_total if a3_total is not None else prev.get("a3_total"),
                "a4_total": a4_total if a4_total is not None else prev.get("a4_total"),
                "pending_overflow_total": None,
                "pending_overflow_delta": None,
                "last_counter_error": None,
            })
            return
        else:
            add_event(ip, "PRINT_OVERFLOW", {
                "message": f"افزایش غیرمنتظره صفحات: {total_delta} صفحه در یک بازه؛ برای جلوگیری از لاگ دروغین در حالت pending نگه داشته شد",
                "severity": "warning",
                "delta": total_delta,
                "dynamic_limit": dynamic_max_delta,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "prev_total": prev_total,
                "current_total": total,
                "pending_confirmation": True,
            })
            store._prev.set(ip, {
                "print_total": prev_total,
                "full_color": prev_fc,
                "black_white": prev_bw,
                "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
                "alert_codes": curr_codes,
                "last_alert_codes": curr_codes,
                "uptime": uptime if uptime is not None else prev_uptime,
                "a3_total": prev.get("a3_total"),
                "a4_total": prev.get("a4_total"),
                "pending_overflow_total": total,
                "pending_overflow_delta": total_delta,
                "reset_prev_total": prev.get("reset_prev_total"),
            })
            return

    # Register PRINT
    if total_delta > 0:
        # ✅ Invariant: pages must equal current_total - prev_total.
        # If total_delta drifted from actual_delta (e.g. split_delta != actual),
        # force consistency so the event is auditable.
        snapshot_delta = total - prev_total
        if snapshot_delta != total_delta and snapshot_delta > 0:
            log.warning(
                "  [%s] snapshot invariant: total_delta=%s but snapshot=%s-%s=%s; correcting",
                ip, total_delta, total, prev_total, snapshot_delta,
            )
            total_delta = snapshot_delta
            # Regenerate message from corrected total_delta
            delta_fc = 0
            delta_bw = total_delta
            color_unknown = True
            counter_mismatch = True
        if color_unknown:
            msg = f"{total_delta} صفحه چاپ شد (تفکیک رنگ نامطمئن)"
            color = "نامشخص"
        elif delta_fc > 0 and delta_bw > 0:
            msg = f"{delta_fc} صفحه رنگی + {delta_bw} صفحه سیاه‌سفید = {total_delta} صفحه چاپ شد"
            color = "مختلط"
        elif delta_fc > 0:
            msg = f"{delta_fc} صفحه رنگی چاپ شد"
            color = "رنگی"
        else:
            msg = f"{total_delta} صفحه سیاه‌سفید چاپ شد"
            color = "سیاه‌سفید"
        if estimated:
            msg = "ثبت تخمینی پس از تایید overflow: " + msg

        event_data = {
            "message": msg,
            "pages": total_delta,
            "color": color,
            "paper_size": paper_size,
            "severity": "warning" if estimated or counter_mismatch else "info",
            # Audit Assignment: Delta double-headed counter values in the essence of the save event
            # It is possible (like GAP/RESET) to audit a window after the leg gap
            # Be accurate and show the "number source" tooltip of real numbers.
            "prev_total": prev_total,
            "current_total": total,
        }
        # ✅ Accurate separation of the paper category: in Mixed mode (and PRINT_GAP) only the general label
        # not enough; The exact number of each batch is stored in details so that the log can be audited.
        # ✅ Fix: paper_split should never have a negative value (uncertain Toshiba split).
        if paper_split and (paper_split.get("large") or paper_split.get("small")):
            _ps_large = max(0, paper_split.get("large", 0))
            _ps_small = max(0, paper_split.get("small", 0))
            event_data["paper_split"] = {"large": _ps_large, "small": _ps_small}
        # ✅ More complete audit (user report from real flat): Separation of size family
        # along with the subgroup "computer print" and the separation of functions (recording in details and
        # auto-merge in API via _row_to_dict) — especially for answering questions
        # "How many A3 pages were printed / Who got copies" No guesswork.
        if paper_detail:
            event_data["paper_detail"] = paper_detail
        if func_split:
            event_data["func_split"] = func_split
        if estimated:
            event_data["estimated"] = True
            event_data["overflow_confirmed"] = True
        if counter_mismatch:
            event_data["counter_mismatch"] = True
            event_data["actual_delta"] = actual_delta
            event_data["split_delta"] = split_delta
        if username:
            event_data["username"] = username
        if poll_timestamp:
            event_data["poll_timestamp"] = poll_timestamp

        add_event(ip, "PRINT", event_data)
        log.info(f"  [{ip}] ✓ ثبت چاپ: {total_delta} صفحه ({color})")

    # ─── save new snapshot; partial counters do not roll back ─────────
    safe_fc = full_color if (full_color is not None and (prev_fc is None or full_color >= prev_fc)) else prev_fc
    safe_bw = black_white if (black_white is not None and (prev_bw is None or black_white >= prev_bw)) else prev_bw

    new_prev = {
        "print_total": total,
        "full_color": safe_fc,
        "black_white": safe_bw,
        "toner_level": current_toner_level if current_toner_level is not None else prev_toner_level,
        "alert_codes": curr_codes,
        "last_alert_codes": curr_codes,
        "uptime": uptime if uptime is not None else prev_uptime,
        "pending_overflow_total": None,
        "pending_overflow_delta": None,
        "reset_prev_total": reset_ref,
        "last_counter_error": None,
        # ✅ Maintaining per-color manual override slots: any color that has not been reset
        # The counter hasn't moved back from its starting point, it should stay alive (fix return to
        # default value after a few seconds).
        "toner_overrides": _overrides_valid_after_total(
            _normalize_overrides(prev), total),
    }
    if current_toner_level is not None and prev_toner_level is not None:
        try:
            # Replacement of the legacy color axis identifier of the same registered poll → No REFILL candidate
            skip_legacy_refill = legacy_toner_color in cart_id_changed_colors
            if (int(current_toner_level) - int(prev_toner_level) > 20 and delta_pages < 50
                    and not refill_confirmed and not skip_legacy_refill):
                new_prev["pending_refill_prev_toner"] = prev_toner_level
                new_prev["pending_refill_new_toner"] = current_toner_level
                new_prev["pending_refill_total"] = total
                new_prev["pending_refill_hits"] = 0
            elif (not refill_confirmed and pending_refill_new is not None
                  and int(current_toner_level) >= int(pending_refill_new) - 1):
                # Persistence Window: Candidate and hits counter are kept until
                # Double check complete (FLAP fix - Pending should not be cleared here)
                new_prev["pending_refill_prev_toner"] = pending_refill_prev
                new_prev["pending_refill_new_toner"] = pending_refill_new
                new_prev["pending_refill_total"] = pending_refill_total
                new_prev["pending_refill_hits"] = pending_refill_hits
            else:
                new_prev["pending_refill_prev_toner"] = None
                new_prev["pending_refill_new_toner"] = None
                new_prev["pending_refill_total"] = None
                new_prev["pending_refill_hits"] = None
        except (TypeError, ValueError):
            pass
    if toner_levels is not None:
        # Cartridge replacement detection status (in-memory cache such as pending_refill)
        new_prev["toner_levels"] = cart_merged_levels
        new_prev["cart_change_pending"] = cart_pending_next if cart_pending_next else None
        new_prev["legacy_toner_color"] = legacy_toner_color
    if cartridge_ids is not None or cartridge_supply_pages is not None:
        # Cartridge identifiers — these are also persisted in the database (column
        # cartridge_id_state) to change during server shutdown/restart
        # Compare the last ID stored in the first poll.
        new_prev["cartridge_ids"] = cart_id_merged
        new_prev["cart_supply_pages"] = cart_pages_merged
    if "cartridge_state" in prev or "cartridge_state" in locals():
        persisted_cartridge_state = prev.get("cartridge_state") or {}
        if not isinstance(persisted_cartridge_state, dict):
            persisted_cartridge_state = {}
        if not persisted_cartridge_state:
            persisted_cartridge_state = locals().get("current_cartridge_state") or {}
        if persisted_cartridge_state:
            new_prev["cartridge_state"] = persisted_cartridge_state
    # Save the name of the supply for comparison in the next poll (recognition of replacement from the name)
    # ⚠️ The names of the colors whose change is pending confirmation remain
    # to compare the same old name with the new name in the next poll.
    if supply_names:
        merged_names = dict(prev.get("supply_names") or {})
        for ck, v in supply_names.items():
            if v and ck not in prev_name_pending:
                merged_names[ck] = v
        new_prev["supply_names"] = merged_names
    # save pending change of supply name (consistency gate 2 poll)
    if prev_name_pending:
        new_prev["supply_name_pending"] = prev_name_pending
    elif "supply_name_pending" in prev:
        new_prev["supply_name_pending"] = None
    if a3_total is not None:
        new_prev["a3_total"] = a3_total
    if a4_total is not None:
        new_prev["a4_total"] = a4_total
    # ✅ Flag "Legacy" of paper counters: If this poll reads one of them
    # If not, the delta of the next poll is multi-interval and the attribute_paper_size must be redundant
    # Subtract from this component (not from the correct component).
    new_prev["a3_lagged"] = a3_total is None
    new_prev["a4_lagged"] = a4_total is None
    store._prev.set(ip, new_prev)

# ─── assigning the paper size to the printing event
# Size classification labels. Toshiba plugs are by nature a bunch, not an exact size:
# Branch 207 = all "large" paper (A3+B4+...), branch 208 = all "small" paper (A4+A5+...)
PAPER_SIZE_LARGE_LABEL = "Large (A3/B4)"
PAPER_SIZE_SMALL_LABEL = "Small (A4/A5)"
PAPER_SIZE_MIXED_LABEL = "Mixed"


def attribute_paper_size(total, prev_total, a3_total, prev_a3, a4_total, prev_a4, ip: str = "",
                         a3_lagged: bool = False, a4_lagged: bool = False):
    """
    Extracts the paper size label from the delta of the large/small paper counters.

    ✅ Fix "wrong size in log" bug: on Toshiba identity
    a3_total + a4_total == print_total is proved; So the delta sum of the two branches
    It must be equal to the delta of the total counter. Previously without this label validation
    was placed and was "sure but wrong" in two scenarios:
      1) Unsuccessful reading of one of the counters in the previous poll (old snapshot → Delta
         multi-range),
      2) Poisoning the baseline with a value of 0 in the legacy path when reading SNMP fail
         (si(...)=0 would be stored in PrevStore and the next delta = total counter!).
    In these cases we now warn and return None (unspecified size) instead
    Wrong label. In "Mixed" mode, the exact number of each batch is returned in paper_split
    can be recorded in the details of the event.

    Returns:
        (paper_size: str|None, paper_split: {"large": int, "small": int}, reliable: bool)
    """
    zero_split = {"large": 0, "small": 0}

    def _as_int(v):
        """Convert a value to int, returning None on failure."""
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    a3 = _as_int(a3_total)
    a4 = _as_int(a4_total)
    pa3 = _as_int(prev_a3)
    pa4 = _as_int(prev_a4)
    if a3 is None or a4 is None or pa3 is None or pa4 is None:
        # Incomplete data (first poll or read failed) — return no guess.
        return None, zero_split, False

    delta_a3 = a3 - pa3
    delta_a4 = a4 - pa4
    if delta_a3 < 0 or delta_a4 < 0:
        log.warning(
            "  [%s] paper counters moved backwards (a3 %s→%s, a4 %s→%s); paper size unreliable",
            ip, pa3, a3, pa4, a4,
        )
        return None, zero_split, False

    if delta_a3 == 0 and delta_a4 == 0:
        return None, zero_split, True

    expected = None
    total_i = _as_int(total)
    prev_total_i = _as_int(prev_total)
    if total_i is not None and prev_total_i is not None:
        expected = total_i - prev_total_i

    if expected is not None:
        # Gap between total counter snapshot and paper snapshot = invalid ratio
        tol = max(2, int(0.05 * max(expected, 1)))
        if abs((delta_a3 + delta_a4) - expected) > tol:
            log.warning(
                "  [%s] paper delta misaligned: a3+a4=%s but total delta=%s; "
                "skipping paper-size attribution (stale baseline)",
                ip, delta_a3 + delta_a4, expected,
            )
            return None, zero_split, False

        # ✅ Absorption of counter rounding/delay error within tolerance: if the sum of the difference is small
        # is greater than the actual delta (eg, a page from a previous interval that then
        # its counter was not read), the surplus is subtracted from the "retarded" component
        # (lagged flag) so that paper_split never exceeds event pages.
        # No flag, heuristic: excess is usually in the smaller late component.
        # We don't fill in the deficiency state (a counter that is behind) — we don't falsify attribution.
        if expected >= 0:
            overflow = (delta_a3 + delta_a4) - expected
            if overflow > 0:
                if a3_lagged and not a4_lagged:
                    delta_a3 = max(0, delta_a3 - overflow)
                elif a4_lagged and not a3_lagged:
                    delta_a4 = max(0, delta_a4 - overflow)
                elif delta_a3 <= delta_a4:
                    delta_a3 = max(0, delta_a3 - overflow)
                else:
                    delta_a4 = max(0, delta_a4 - overflow)

    if delta_a3 == 0 and delta_a4 == 0:
        return None, zero_split, True

    split = {"large": delta_a3, "small": delta_a4}
    if delta_a3 > 0 and delta_a4 == 0:
        return PAPER_SIZE_LARGE_LABEL, split, True
    if delta_a4 > 0 and delta_a3 == 0:
        return PAPER_SIZE_SMALL_LABEL, split, True
    return PAPER_SIZE_MIXED_LABEL, split, True


# Compatibility of counters
def validate_counter_consistency(counters: dict, brand: str) -> list:
    """Check counter values against each other and return a list of warning strings."""

    warnings = []
    total   = counters.get("total",       0) or 0
    color   = counters.get("full_color",  0) or 0
    bw      = counters.get("black_white", 0) or 0
    copy_   = counters.get("copy",        0) or 0
    printer = counters.get("printer",     0) or 0

    if brand == "toshiba" and total > 0:
        twin_ = counters.get("twin", 0) or 0
        if color + bw > 0:
            diff = abs(total - (color + bw + twin_))
            if diff > max(100, total * 0.01):
                warnings.append(
                    f"⚠ Toshiba: fc({color:,})+bw({bw:,})+twin({twin_:,})={color+bw+twin_:,} ≠ total({total:,}) diff={diff:,}"
                )
        copy_fc = counters.get("copy_fc",   0) or 0
        ptr_fc  = counters.get("printer_fc",0) or 0
        if color > 0 and (copy_fc + ptr_fc) > color + 1000:
            warnings.append(
                f"⚠ Toshiba FC: copy_fc({copy_fc:,})+ptr_fc({ptr_fc:,})={copy_fc+ptr_fc:,} > fc_total({color:,})"
            )

    if brand == "canon" and total > 0 and copy_ > 0 and printer > 0:
        diff = abs(total - (copy_ + printer))
        if diff > max(300, total * 0.01):
            warnings.append(
                f"⚠ Canon: copy({copy_:,})+print({printer:,})={copy_+printer:,} ≠ total({total:,}) diff={diff:,}"
            )

    return warnings


# Brand recognition
def fetch_first_web_page(ip: str, urls, timeout: float = 4.0):
    """Returns the first web page readable from the device panel.

    For script toner fallbacks (Canon/Brother/HP) when SNMP does not give a number.
    - first requests (if installed) and otherwise urllib
    - HTTPS is also supported; Self-signed certificates from printers are accepted
      (Many HPs close port 80 and only respond on 443).
    Output: (url, html) or (None, None)
    """
    try:
        import requests as _req
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        for url in urls:
            try:
                resp = _req.get(url, timeout=timeout,
                                headers={"User-Agent": "PrinterMonitor/1.0"},
                                allow_redirects=True, verify=False)
                if resp.status_code == 200 and resp.text:
                    return url, resp.text
            except Exception:
                continue
    except ImportError:
        pass
    try:
        import urllib.request as _ur
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        for url in urls:
            try:
                req = _ur.Request(url, headers={"User-Agent": "PrinterMonitor/1.0"})
                with _ur.urlopen(req, timeout=timeout, context=ctx) as resp:
                    data = resp.read(300_000).decode("utf-8", errors="replace")
                    if data:
                        return url, data
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def detect_brand(ip: str, community: str) -> str:
    """Identify the printer brand (hp, canon, brother, toshiba, ...) from its sysObjectID and sysDescr."""

    sys_oid  = snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.2.0", community, timeout=2.0)
    sys_desc = str(snmp_get_with_fallback(ip, "1.3.6.1.2.1.1.1.0", community, timeout=2.0) or "").lower()
    oid_str  = str(sys_oid) if sys_oid else ""

    if "ecs100g" in sys_desc or "1.3.6.1.4.1.47206" in oid_str:
        return "sensor"
    # Some ECS100Gs do not provide a standard sysDescr; We also probe with dedicated OIDs.
    for sensor_oid in (
        "1.3.6.1.4.1.47206.1.0",
        "1.3.6.1.4.1.47206.110.1.2.0",
        "1.3.6.1.4.1.47206.111.1.2.0",
    ):
        if snmp_get_with_fallback(ip, sensor_oid, community, timeout=1.5) is not None:
            return "sensor"
    if "1.3.6.1.4.1.1129" in oid_str or "toshiba"   in sys_desc: return "toshiba"
    if "1.3.6.1.4.1.1602" in oid_str or "canon"     in sys_desc: return "canon"
    if "1.3.6.1.4.1.2435" in oid_str or "brother"   in sys_desc: return "brother"
    if ("1.3.6.1.4.1.11"   in oid_str or
            "jetdirect" in sys_desc or
            "hp " in sys_desc or
            "hewlett" in sys_desc or
            "laserjet" in sys_desc or
            "officejet" in sys_desc or
            "pagewide" in sys_desc):
        return "hp"
    return "unknown"