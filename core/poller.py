# =============================================================================
# @file:         core/poller.py
# @brief:        Background polling loop that collects data from every configured printer.
# @details:      Responsibilities: Route each printer to the right collector, enforce per-IP
#                locking and grace windows, and record events.
#                Main API: collect(), poll_one(), poll_all(), polling_loop()
#                Side effects: network I/O, starts threads
# @dependencies: internal: config.settings, core, core.collectors.base,
#                core.collectors.base_enhanced, core.collectors.brother, core.collectors.canon,
#                core.collectors.exporter_adapter, core.collectors.hp, core.collectors.sensor,
#                core.collectors.toshiba, core.database, core.exporter_client, core.snmp.oid_map,
#                core.snmp.protocol, core.time_utils
# @notes:        Per-IP locks prevent concurrent polls of one printer. The SNMP exporter path is
#                off unless USE_SNMP_EXPORTER is enabled.
# =============================================================================

# core/poller.py (PATCHED - All Critical Bugs Fixed)

"""
Polling cycle:
- collect: collecting data from a printer with routing to the appropriate collector
- poll_all: parallel polling of all printers
- polling_loop: infinite loop with POLL_INTERVAL
"""

import logging
import threading
import time
from threading import Lock

from config.settings import POLL_INTERVAL
from core import store
from core.database import add_event
from core.snmp.protocol import snmp_get_first, snmp_debug_get
from core.snmp.oid_map import OIDS
from core.collectors.base import detect_brand
# 🔥 Change: Use enhanced_collector instead of separate collectors
from core.collectors.base_enhanced import collect_enhanced
# Enhanced fallback to brand-oriented collectors to preserve event logging in case of error
from core.collectors.sensor import collect_sensor
# Phase G · Step G.5 — exporter test path (default off; see exporter_enabled())
from core.exporter_client import client_from_env, exporter_enabled
from core.collectors.exporter_adapter import collect_via_exporter
from core.collectors.toshiba import collect_toshiba
from core.collectors.hp import collect_hp
from core.collectors.canon import collect_canon
from core.collectors.brother import collect_brother
from core.time_utils import utc_now_iso

log = logging.getLogger("PrinterMonitor")

# Lock to prevent concurrent execution of poll_all
_polling_lock = threading.Lock()

#: only created when the flag is on (lazy) — with the flag off no object
#: It is not created and no socket is opened.
_exporter_client_cache = None


def _exporter_client():
    """Return the shared exporter client, creating it on first use."""
    global _exporter_client_cache
    if _exporter_client_cache is None:
        _exporter_client_cache = client_from_env()
    return _exporter_client_cache

# Bug #1: separate lock for processed_ips (avoid race condition)
_processed_ips_lock = Lock()
_ip_locks_guard = Lock()
_ip_poll_locks = {}

_SNMP_REACHABLE_STATUSES = {
    "ok",
    "no_such_object",
    "no_such_instance",
    "end_of_mib_view",
    "error_status",
    "request_id_mismatch",
}

# Arafaq window before offline announcement: a lost UDP cycle (common in
# Busy and slow SNMP stack (Toshibas) should not take the device offline. Device only after
# Two consecutive health failures are declared offline and the STATUS event is recorded.
_OFFLINE_GRACE_CYCLES = 2
_health_fail_lock = threading.Lock()
_health_fail_counts: dict[str, int] = {}


def _note_health_failure(ip: str) -> int:
    """Increment and return the consecutive health-check failure count of a printer."""
    with _health_fail_lock:
        n = _health_fail_counts.get(ip, 0) + 1
        _health_fail_counts[ip] = n
        return n


def _reset_health_failure(ip: str) -> None:
    """Clear the consecutive health-check failure count of a printer."""
    with _health_fail_lock:
        _health_fail_counts.pop(ip, None)


def _get_ip_lock(ip: str) -> Lock:
    """To avoid simultaneous polling of an IP, even if the previous thread finishes late."""
    with _ip_locks_guard:
        lock = _ip_poll_locks.get(ip)
        if lock is None:
            lock = Lock()
            _ip_poll_locks[ip] = lock
        return lock


def _probe_snmp_agent_reachable(ip: str, community: str, health_oids: list[str], timeout: float = 1.5):
    """Checks if the SNMP agent is responding, even if it has no readable OID."""
    seen = set()
    for idx, oid in enumerate(health_oids, 1):
        if not oid or oid in seen:
            continue
        seen.add(oid)
        for version in (2, 1):
            diag = snmp_debug_get(ip, oid, community, timeout=timeout, request_id=(idx * 10 + version), version=version)
            if diag.get("status") in _SNMP_REACHABLE_STATUSES:
                return diag
    return None


def collect(printer: dict) -> dict:
    """
    Collect data from a printer.
    🔥 Change: use enhanced_collector for all devices (except sensor)

    ✅ Duplicate fix: per-IP lock is taken here (not only in poll_all) until
    No two polls ever occur on the same device — previously the paths api_add /
    bulk-add / auto-add called collect without locking and the possibility of two PRINT
    At the same time there was a snapshot.
    """
    ip = printer["ip"]
    ip_lock = _get_ip_lock(ip)
    if not ip_lock.acquire(blocking=False):
        log.warning("Skipping %s because another poll for this IP is still running", ip)
        with store.data_lock:
            previous = store.printer_data.get(ip) or {}
        # Return the old output so that the UI is not replaced with incomplete data
        result = dict(previous) if previous else {
            "ip": ip,
            "name": printer.get("name", ip),
            "nickname": printer.get("nickname", ""),
            "brand": printer.get("brand", ""),
            "device_type": printer.get("device_type", "unknown"),
            "online": True,
            "partial": True,
            "last_poll": None,
            "error": "poll already running",
        }
        result["skipped_concurrent_poll"] = True
        return result
    try:
        return _collect_inner(printer)
    finally:
        ip_lock.release()


def _collect_inner(printer: dict) -> dict:
    """The main body of collect — only called via collect() (with a lock)."""
    ip = printer["ip"]
    name = printer["name"]
    nickname = printer.get("nickname", "")
    community = printer.get("community", "public")
    brand = printer.get("brand", "").lower()
    device_type = printer.get("device_type", "unknown")

    log.info(f"Pulling {name} ({ip}) [{brand or 'auto'}] - using enhanced collector")
    start = time.time()

    # Initial test to be online
    # Some devices (like some Toshibas) do not respond to sysDescr but to sysUpTime
    # or vendor-specific OIDs give a valid response. For backward compatibility,
    # We try several health OIDs in order.
    health_oids = [
        "1.3.6.1.2.1.1.1.0",           # sysDescr
        "1.3.6.1.2.1.1.3.0",           # standard sysUpTime
        "1.3.6.1.2.1.1.5.0",           # sysName
        OIDS.get("uptime", "1.3.6.1.2.1.1.3.0"),  # vendor uptime (e.g. Toshiba)
        "1.3.6.1.2.1.43.5.1.1.16.1",   # Printer-MIB model/name
        OIDS.get("model"),             # vendor model (e.g. Toshiba)
    ]
    # ✅ Toshiba: some models do not respond to standard OIDs in limited view;
    # Bring the vendor counter to the beginning of the list so that the health probe succeeds faster.
    if brand == "toshiba":
        health_oids = [
            OIDS.get("print_total"),   # Toshiba vendor total counter
            OIDS.get("model"),         # Toshiba vendor model
            OIDS.get("uptime", "1.3.6.1.2.1.1.3.0"),
        ] + [o for o in health_oids if o not in (OIDS.get("model"),)]
    # Removing duplicates while maintaining order
    if brand == "sensor":
        health_oids = [
            "1.3.6.1.4.1.47206.1.0",
            "1.3.6.1.4.1.47206.110.1.2.0",
            "1.3.6.1.4.1.47206.111.1.2.0",
        ] + health_oids
    health_oids = list(dict.fromkeys([oid for oid in health_oids if oid]))
    health_timeout = 3.0 if brand == "toshiba" else 2.0
    test, used_oid = snmp_get_first(ip, health_oids, community, timeout=health_timeout)
    online = test is not None
    snmp_reachable_diag = None
    if online and used_oid != "1.3.6.1.2.1.1.1.0":
        log.info(f"Online probe fallback succeeded for {ip} using OID {used_oid}")
    if not online:
        snmp_reachable_diag = _probe_snmp_agent_reachable(ip, community, health_oids, timeout=1.5)

    with store.data_lock:
        was_online = store.printer_data.get(ip, {}).get("online", None)

    if online:
        _reset_health_failure(ip)

    if not online:
        # ✅ Arafaq: The first failure of health does not immediately take the already online device offline
        fail_count = _note_health_failure(ip)
        if was_online and fail_count < _OFFLINE_GRACE_CYCLES:
            with store.data_lock:
                previous_data = dict(store.printer_data.get(ip, {}) or {})
            previous_data["degraded"] = True
            previous_data["degraded_reason"] = (
                f"health probe ناموفق (تلاش {fail_count}/{_OFFLINE_GRACE_CYCLES}) — حفظ وضعیت قبلی تا چرخه بعد"
            )
            log.info(
                "%s آفلاین اعلام نشد (grace %s/%s): UDP drop/پاسخ ناقص موقتی؛ چرخه بعد تأیید می‌شود",
                ip, fail_count, _OFFLINE_GRACE_CYCLES,
            )
            return previous_data

        elapsed = int((time.time() - start) * 1000)

        # If the agent gives an SNMP response but the OIDs cannot be read, the device as
        # We display reachable, but with a clear warning, so as not to be confused with the real offline mode.
        if snmp_reachable_diag:
            msg = (
                f"SNMP agent reachable but required OIDs are not readable "
                f"(community={community}, status={snmp_reachable_diag.get('status')}, oid={snmp_reachable_diag.get('oid')})"
            )
            log.warning("%s -> %s", ip, msg)
            with store.data_lock:
                previous_data = store.printer_data.get(ip, {}) or {}
            prev = store._prev.get(ip) or {}
            return {
                "ip": ip,
                "name": name,
                "nickname": nickname,
                "brand": brand,
                "device_type": device_type,
                "online": True,
                "partial": True,
                "last_poll": utc_now_iso(),
                "poll_ms": elapsed,
                "error_type": "snmp_restricted",
                "error": msg,
                "device": previous_data.get("device") or {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
                "counters": previous_data.get("counters") or {
                    "total": prev.get("print_total", 0) or 0,
                    "full_color": prev.get("full_color"),
                    "black_white": prev.get("black_white", 0),
                },
                "paper_sizes": previous_data.get("paper_sizes") or {},
                "trays": previous_data.get("trays") or [],
                "toners": previous_data.get("toners") or {},
                "alerts": [
                    {
                        "message": "SNMP پاسخ می‌دهد اما OIDهای لازم با community فعلی قابل خواندن نیستند",
                        "code": "snmp_restricted",
                    }
                ],
            }

        if was_online:
            add_event(ip, "STATUS", {"message": "دستگاه آفلاین شد", "severity": "error"})
        return {
            "ip": ip, "name": name, "nickname": nickname, "brand": brand, "device_type": device_type,
            "online": False,
            "last_poll": utc_now_iso(),
            "poll_ms": elapsed,
            "error": "Device unreachable",
        }

    if was_online is False:
        add_event(ip, "STATUS", {"message": "دستگاه آنلاین شد", "severity": "success"})

    # Brand recognition (if not already known)
    if brand == "sensor":
        # Sensors with their own collector
        result = collect_sensor(ip, name, community, start)
        result["nickname"] = nickname
        result["device_type"] = "sensor"
        return result

    if not brand or brand == "unknown":
        brand = detect_brand(ip, community)
        log.info(f"  → برند شناسایی شد: {brand}")
        with store.printers_lock:
            for p in store.PRINTERS:
                if p["ip"] == ip:
                    p["brand"] = brand
                    store.save_printers(store.PRINTERS)
                    break

    # ── Phase G · Step G.5: exporter path, only with USE_SNMP_EXPORTER=1 ──────────
    # The order of the condition is important: exporter_enabled() is evaluated first, then with the off flag
    # (Phase default) No is_available() and no socket occur, and today's behavior
    # Word for word it is the same path as collect_enhanced.
    # This path intentionally does not register the counter event (_counters_event is in collectors and
    # collectors/ remains intact in this phase); That is why it is experimental and until
    # Step G.6 does not turn on (docs/snmp-exporter-setup.md §9).
    if exporter_enabled() and _exporter_client().is_available():
        exporter_result = collect_via_exporter(_exporter_client(), ip, name, brand, start)
        if exporter_result is not None:
            exporter_result["nickname"] = nickname
            exporter_result["device_type"] = exporter_result.get("device_type", device_type)
            return exporter_result

    # 🔥 Use enhanced_collector for all printers
    try:
        result = collect_enhanced(printer)
        result["nickname"] = nickname
        result["device_type"] = result.get("device_type", device_type)
        return result
    except Exception as e:
        log.error(f"Enhanced collector failed for {ip}: {e}, falling back to legacy collector")

        legacy_collectors = {
            "toshiba": collect_toshiba,
            "hp": collect_hp,
            "canon": collect_canon,
            "brother": collect_brother,
        }
        legacy_collector = legacy_collectors.get(brand)
        if legacy_collector is not None:
            try:
                result = legacy_collector(ip, name, community, start)
                result["nickname"] = nickname
                result["device_type"] = result.get("device_type", device_type)
                result["error"] = str(e)
                return result
            except Exception as legacy_error:
                log.exception("Legacy collector also failed for %s: %s", ip, legacy_error)

        # Last fallback: keeping the previous snapshot instead of zeroing the counters
        prev = store._prev.get(ip) or {}
        prev_total = prev.get("print_total", 0) or 0
        elapsed = int((time.time() - start) * 1000)
        return {
            "ip": ip, "name": name, "nickname": nickname, "brand": brand,
            "online": True,
            "last_poll": utc_now_iso(),
            "poll_ms": elapsed,
            "device": {"model": "Unknown", "serial": "N/A", "firmware": "N/A", "uptime_str": "N/A"},
            "counters": {
                "total": prev_total,
                "full_color": prev.get("full_color"),
                "black_white": prev.get("black_white", 0),
            },
            "paper_sizes": {}, "trays": [], "toners": {}, "alerts": [],
            "error": str(e),
        }


def poll_one(p: dict):
    """Poll a single printer"""
    data = collect(p)
    with store.data_lock:
        store.printer_data[p["ip"]] = data


def poll_all():
    """Run poll for all printers by preventing them from running at the same time"""
    with _polling_lock:
        # ✅ Status flag for UI: Manual Pull button when cycle is running
        # It shows "Running" status instead of the confusing 409 error.
        store.poll_stats["is_polling"] = True
        try:
            with store.printers_lock:
                current = list(store.PRINTERS)

            log.info(f"🔄 Starting pull cycle for {len(current)} devices (interval={POLL_INTERVAL}s)")
            results = {}
            processed_ips = set()

            def _poll(p):
                """Poll one printer and store its results."""
                ip = p["ip"]
                # Bug #1: Race Condition - Use of lock to prevent double polling
                with _processed_ips_lock:
                    if ip in processed_ips:
                        log.warning(f"Skipping duplicate poll for {ip}")
                        return
                    processed_ips.add(ip)
                try:
                    results[ip] = collect(p)
                except Exception as e:
                    log.error(f"Error polling {ip}: {e}")

            threads = [threading.Thread(target=_poll, args=(p,), daemon=True) for p in current]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)  # ✅ Increased timeout to 60 seconds

            with store.data_lock:
                store.printer_data.update(results)
                store.poll_stats["count"] += 1
                store.poll_stats["last"] = utc_now_iso()
                store.poll_stats["errors"] = sum(1 for d in results.values() if not d.get("online"))

            log.info(f"✅ Pull cycle completed: {len(results)} devices, "
                     f"{store.poll_stats['errors']} errors, "
                     f"next pull in {POLL_INTERVAL}s")
        finally:
            store.poll_stats["is_polling"] = False


def polling_loop():
    """Infinite polling loop"""
    # poll_all executes an immediate cycle at startup; This sleep prevents execution
    # Immediately, the second cycle and registration of repeated PRINTs will be done in the first few seconds.
    time.sleep(POLL_INTERVAL)
    while True:
        try:
            poll_all()
        except Exception as e:
            log.error(f"Error in pull loop: {e}")
        time.sleep(POLL_INTERVAL)