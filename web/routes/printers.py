# =============================================================================
# @file:         web/routes/printers.py
# @brief:        API routes for printer records and debug endpoints.
# @details:      Responsibilities: Expose printer CRUD and debugging data over HTTP.
#                Main API: api_printers(), api_printer(), debug_printer(), debug_brother_toner(),
#                debug_toshiba_snmp(), api_add(), api_bulk_add(), api_remove() (+3 more)
#                Side effects: network I/O, starts threads, defines HTTP routes
# @dependencies: internal: config.settings, core, core.collectors.base, core.database,
#                core.enhanced_collector, core.oid.scanner, core.poller, core.snmp.oid_map,
#                core.snmp.protocol, core.store, core.yield_engine, web.auth | external: flask,
#                flask_login
# @notes:        Reads environment variables: ENABLE_DEBUG_ENDPOINTS.
# =============================================================================

"""Printer management and debugging API routes."""

import logging
import os
import re
import threading

from flask import Blueprint, jsonify, request
from flask_login import current_user

from core import store
from core.supply_usage import counter_basis, genuine_cartridge_ids, new_record, usage_view, tracking_lock
from core.database import add_event
from core.time_utils import utc_now_iso
from core.poller import collect, poll_one
from core.oid.scanner import scan_printer_oids
from config.settings import POLL_INTERVAL, TONER_ALERT_THRESHOLDS
from web.auth import user_can_access_office, allowed_printer_ips, user_allowed_offices, admin_required

log = logging.getLogger("PrinterMonitor")

bp = Blueprint("printers", __name__)

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _is_valid_ip(ip: str) -> bool:
    """IPv4 Validation — Prevent invalid/malicious strings from entering printers.json"""
    if not ip or len(ip) > 15 or not _IP_RE.match(ip):
        return False
    try:
        return all(0 <= int(part) <= 255 for part in ip.split("."))
    except (TypeError, ValueError):
        return False


def _debug_endpoints_enabled() -> bool:
    """🔒 Debug endpoints are enabled only with ENABLE_DEBUG_ENDPOINTS=1."""
    return os.getenv("ENABLE_DEBUG_ENDPOINTS", "0") == "1"


def _debug_forbidden():
    """Return a 404 response for debug routes that are disabled."""
    return jsonify({"error": "not found"}), 404


def _attach_yield_metadata(printer_data: dict, preloaded_states: dict = None) -> dict:
    """Add per-color yield/capacity & interval CI95 metadata to API output."""
    if not isinstance(printer_data, dict):
        return printer_data
    ip = printer_data.get("ip")
    if not ip:
        return printer_data
    prev = store._prev.get(ip) or {}
    try:
        ypp = int(prev.get("yield_per_page", 2000) or 2000)
    except (TypeError, ValueError):
        ypp = 2000
    ysrc = "default" if ypp == 2000 else "learned"
    if prev.get("force_estimate"):
        ysrc = "forced_estimate"

    toners = printer_data.get("toners") or {}
    counters = dict(printer_data.get("counters") or {})
    printer_data["counters"] = counters
    needs_engine_lookup = preloaded_states is None and (
        ("yield_per_page" not in counters or "yield_source" not in counters)
        or (isinstance(toners, dict) and any(
            isinstance(t, dict) and (
                "capacity_pages" not in t
                or "capacity_source" not in t
                or "capacity_ci95" not in t
            )
            for t in toners.values()
        ))
    )

    state_by_color = dict((preloaded_states or {}).get(ip) or {})
    if needs_engine_lookup:
        try:
            from core.yield_engine import get_yield_status
            for row in get_yield_status(ip=ip):
                c_key = str(row.get("color") or "").lower()
                if c_key:
                    state_by_color[c_key] = row
        except Exception:
            state_by_color = {}

    black_state = state_by_color.get("black")
    eff_black_ypp = int((black_state or {}).get("yield_per_page") or ypp)
    eff_black_src = (black_state or {}).get("yield_source") or ysrc
    eff_black_conf = (black_state or {}).get("confidence") or ("low" if eff_black_src == "default" else "medium")

    counters.setdefault("yield_per_page", eff_black_ypp)
    counters.setdefault("yield_source", eff_black_src)
    counters.setdefault("yield_confidence", eff_black_conf)
    counters.setdefault("force_estimate", prev.get("force_estimate", 0))
    counters.setdefault("yield_learning_failures", prev.get("yield_learning_failures", 0))

    if isinstance(toners, dict):
        from core.collectors.base import _normalize_overrides as _norm_ov, evaluate_override_quarantine
        active_ov = _norm_ov(prev)
        cur_tot = counters.get("total")
        enriched_toners = {}
        for color, raw_toner in toners.items():
            if not isinstance(raw_toner, dict):
                enriched_toners[color] = raw_toner
                continue
            toner = dict(raw_toner)
            color_lc = str(color).lower()
            c_state = state_by_color.get(color_lc)
            if c_state:
                c_ypp = int(c_state.get("yield_per_page") or ypp)
                c_src = c_state.get("yield_source") or "default"
                c_conf = c_state.get("confidence") or "low"
                toner.setdefault("capacity_pages", c_ypp)
                toner.setdefault("capacity_source", c_src)
                toner.setdefault("yield_per_page", c_ypp)
                toner.setdefault("yield_confidence", c_conf)
                for k in (
                    "capacity_ci95",
                    "capacity_method",
                    "learning_state",
                    "nominal_capacity_pages",
                    "coverage_index",
                    "eta_pages",
                    "sensor_state",
                    "chip_quarantined",
                    "frozen_chip_level",
                    "overrun_pages",
                    "unregistered_refill_suspected",
                    "last_reset_reason",
                ):
                    if k in c_state:
                        toner.setdefault(k, c_state[k])
            else:
                toner.setdefault("capacity_pages", ypp)
                toner.setdefault("capacity_source", ysrc if color == "black" else f"{ysrc}_global")
                toner.setdefault("yield_per_page", ypp)

            ov_entry = active_ov.get(color_lc)
            if isinstance(ov_entry, dict) and ov_entry and cur_tot is not None:
                ev = evaluate_override_quarantine(
                    ov_entry,
                    cur_tot,
                    snmp_level=toner.get("raw_level"),
                    swapped=False,
                    fallback_yield=toner.get("yield_per_page") or ypp,
                )
                if not ev.get("release"):
                    if ev.get("unregistered_refill_suspected"):
                        toner["sensor_state"] = "overrun_alert"
                        toner["unregistered_refill_suspected"] = True
                    elif str(ov_entry.get("mode") or "") == "auto_frozen_chip":
                        toner["sensor_state"] = "frozen_chip"
                    elif ev.get("chip_quarantined"):
                        toner.setdefault("sensor_state", "chip_quarantined")
                        if toner.get("sensor_state") == "healthy":
                            toner["sensor_state"] = "chip_quarantined"
                    toner["chip_quarantined"] = bool(ev.get("chip_quarantined"))
                    toner["overrun_pages"] = max(int(toner.get("overrun_pages") or 0), int(ev.get("overrun_pages") or 0))
                    if toner.get("frozen_chip_level") is None:
                        raw_cand = toner.get("raw_level", ov_entry.get("chip_level_at_reset"))
                        if isinstance(raw_cand, (int, float)) and 0 <= int(raw_cand) <= 100:
                            toner["frozen_chip_level"] = int(raw_cand)
                    if not toner.get("last_reset_reason") and ov_entry.get("reset_reason") in ("empty", "dirty"):
                        toner["last_reset_reason"] = ov_entry.get("reset_reason")
            # System-counted usage of this cartridge (see core/supply_usage.py); not read from the device.
            _usage_map = prev.get("supply_usage") if isinstance(prev.get("supply_usage"), dict) else {}
            _usage = usage_view(_usage_map.get(color_lc))
            toner["supply_pages"] = _usage["pages"]
            toner["first_install_date"] = _usage["first_install_date"]
            toner["last_use_date"] = _usage["last_use_date"]
            enriched_toners[color] = toner
        printer_data["toners"] = enriched_toners
    return printer_data


def _build_unpolled_printer_entry(p: dict, prev: dict, state_by_color: dict) -> dict:
    """Build an `/api/printers` entry for a configured printer awaiting its first SNMP poll.

    Preserves ``online: None`` (never marking the device online/offline prematurely in
    in-memory ``store.printer_data`` or metrics), while hydrating last-known persisted
    counters, toners, model, and cartridge IDs so startup cards render immediately.
    """
    prev = prev or {}
    state_by_color = state_by_color or {}
    entry = {
        "ip": p["ip"],
        "name": p["name"],
        "nickname": p.get("nickname", ""),
        "device_type": p.get("device_type") or prev.get("device_type") or "unknown",
        "brand": p.get("brand", "unknown"),
        "group": p.get("group", ""),
        "community": p.get("community", "public"),
        "online": None,
        "last_poll": prev.get("updated_at") if prev else None,
        "pending_initial_poll": True,
    }
    counters = {}
    if prev.get("print_total") is not None:
        counters["total"] = prev["print_total"]
    for src_key in ("full_color", "black_white", "a3_total", "a4_total"):
        if prev.get(src_key) is not None:
            counters[src_key] = prev[src_key]
    if counters:
        entry["counters"] = counters

    toners = {}
    for c_lc, c_row in state_by_color.items():
        if not isinstance(c_row, dict) or c_row.get("current_level") is None:
            continue
        toners[c_lc] = {
            "name": c_row.get("cartridge_name") or c_lc.title(),
            "level": int(c_row["current_level"]),
        }
    if not toners and prev.get("toner_level") is not None:
        toners["black"] = {
            "name": "Black Toner",
            "level": int(prev["toner_level"]),
            "estimated": bool(prev.get("manual_override") or prev.get("force_estimate")),
        }
    if toners:
        entry["toners"] = toners

    model_name = next(
        (
            str(c_row.get("printer_model")).strip()
            for c_row in state_by_color.values()
            if isinstance(c_row, dict) and c_row.get("printer_model")
        ),
        None,
    )
    if model_name:
        entry["device"] = {"model": model_name}

    cart_ids = prev.get("cartridge_ids")
    if isinstance(cart_ids, dict) and cart_ids:
        entry["cartridge_ids"] = dict(cart_ids)
    cart_state = prev.get("cartridge_state")
    if isinstance(cart_state, dict) and cart_state:
        entry["cartridge_state"] = dict(cart_state)
        sig_q = {
            c: st.get("signal_quality")
            for c, st in cart_state.items()
            if isinstance(st, dict) and st.get("signal_quality")
        }
        if sig_q:
            entry["cartridge_signal_quality"] = sig_q

    return entry


@bp.route('/api/printers')
def api_printers():
    """Return the printers the current user may see, with their latest data."""

    allowed_offices = user_allowed_offices(current_user)
    allowed_ips = allowed_printer_ips(current_user)
    with store.data_lock:
        snap = [dict(item) for item in store.printer_data.values()]
    with store.printers_lock:
        cfg = list(store.PRINTERS)
    cfg_by_ip = {p.get("ip"): p for p in cfg if p.get("ip")}
    # admin has full access; allowed_offices for admin should not be empty list
    # Remove all printers. Restriction is only for non-admin users.
    if allowed_offices and getattr(current_user, "role", None) != "admin":
        snap = [d for d in snap if d.get("ip") in allowed_ips]
        cfg = [p for p in cfg if p.get("ip") in allowed_ips]
        cfg_by_ip = {p.get("ip"): p for p in cfg if p.get("ip")}
    for data in snap:
        configured = cfg_by_ip.get(data.get("ip")) or {}
        for key in ("device_type", "brand", "group"):
            if configured.get(key) and not data.get(key):
                data[key] = configured[key]

    all_ips = [ip for ip in (list(cfg_by_ip.keys()) + [d.get("ip") for d in snap]) if ip]
    store._prev.warm_many(all_ips)

    preloaded_states = {}
    try:
        from core.yield_engine import get_yield_status
        for row in get_yield_status():
            r_ip = row.get("printer_ip")
            r_color = str(row.get("color") or "").lower()
            if r_ip and r_color:
                preloaded_states.setdefault(r_ip, {})[r_color] = row
    except Exception:
        preloaded_states = {}

    seen = {d["ip"] for d in snap if d.get("ip")}
    for p in cfg:
        p_ip = p.get("ip")
        if p_ip and p_ip not in seen:
            snap.append(
                _build_unpolled_printer_entry(
                    p,
                    store._prev.get(p_ip) or {},
                    preloaded_states.get(p_ip) or {},
                )
            )

    snap = [_attach_yield_metadata(d, preloaded_states=preloaded_states) for d in snap]
    return jsonify({"printers": snap, "meta": {
        "total": len(cfg),
        "online": sum(1 for d in snap if d.get("online")),
        "offline": sum(1 for d in snap if d.get("online") is False),
        "poll_count": store.poll_stats["count"],
        "last_poll": store.poll_stats["last"],
        "poll_interval": POLL_INTERVAL,
    }})


@bp.route('/api/printer/<path:ip>')
def api_printer(ip):
    """Return the data of one printer."""

    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
    with store.data_lock:
        d = dict(store.printer_data.get(ip) or {}) if store.printer_data.get(ip) else None
    return jsonify(_attach_yield_metadata(d)) if d else (jsonify({"error": "not found"}), 404)


@bp.route('/api/debug/printer/<path:ip>')
def debug_printer(ip):
    """Return raw diagnostic data of one printer (only when debug endpoints are enabled)."""

    if not _debug_endpoints_enabled():
        return _debug_forbidden()
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403
    with store.data_lock:
        data = store.printer_data.get(ip)
    if data is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@bp.route('/api/debug/brother-toner/<path:ip>')
@admin_required
def debug_brother_toner(ip):
    """Temporary endpoint to check raw Brother toner OIDs."""
    if not _debug_endpoints_enabled():
        return _debug_forbidden()
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403

    from core.enhanced_collector import (
        detect_snmp_version,
        walk_supplies_table,
        BROTHER_TONER_OID,
        BROTHER_DRUM_OID,
    )
    from core.snmp.protocol import snmp_get_with_fallback

    community = "public"
    printer_name = ip
    with store.printers_lock:
        for printer in store.PRINTERS:
            if printer.get("ip") == ip:
                community = printer.get("community", "public") or "public"
                printer_name = printer.get("name") or ip
                break

    snmp_version = detect_snmp_version(ip, community, timeout=2.0)

    raw_standard = []
    for idx in range(1, 9):
        name_oid = f"1.3.6.1.2.1.43.11.1.1.6.1.{idx}"
        type_oid = f"1.3.6.1.2.1.43.11.1.1.5.1.{idx}"
        max_oid = f"1.3.6.1.2.1.43.11.1.1.8.1.{idx}"
        rem_oid = f"1.3.6.1.2.1.43.11.1.1.9.1.{idx}"
        name_val = snmp_get_with_fallback(ip, name_oid, community, version=snmp_version, timeout=2.0)
        type_val = snmp_get_with_fallback(ip, type_oid, community, version=snmp_version, timeout=2.0)
        max_val = snmp_get_with_fallback(ip, max_oid, community, version=snmp_version, timeout=2.0)
        rem_val = snmp_get_with_fallback(ip, rem_oid, community, version=snmp_version, timeout=2.0)
        if all(v is None for v in (name_val, type_val, max_val, rem_val)):
            continue
        raw_standard.append({
            "index": idx,
            "name_oid": name_oid,
            "name": name_val,
            "type_oid": type_oid,
            "type": type_val,
            "max_oid": max_oid,
            "max": max_val,
            "remaining_oid": rem_oid,
            "remaining": rem_val,
        })

    interpreted = walk_supplies_table(
        ip,
        community,
        brand="brother",
        snmp_version=snmp_version,
        timeout=2.0,
    )

    with store.data_lock:
        current_data = store.printer_data.get(ip, {}) or {}

    return jsonify({
        "ip": ip,
        "name": printer_name,
        "community": community,
        "snmp_version": snmp_version,
        "raw_oids": {
            "brother_toner_oid": BROTHER_TONER_OID,
            "brother_toner_value": snmp_get_with_fallback(ip, BROTHER_TONER_OID, community, version=snmp_version, timeout=2.0),
            "brother_drum_oid": BROTHER_DRUM_OID,
            "brother_drum_value": snmp_get_with_fallback(ip, BROTHER_DRUM_OID, community, version=snmp_version, timeout=2.0),
            "standard_supplies": raw_standard,
        },
        "interpreted_supplies": interpreted,
        "current_dashboard_toners": (current_data.get("toners") or {}),
    })


@bp.route('/api/debug/toshiba-snmp/<path:ip>')
@admin_required
def debug_toshiba_snmp(ip):
    """Temporary endpoint to check raw SNMP probes and Toshiba OIDs."""
    if not _debug_endpoints_enabled():
        return _debug_forbidden()
    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403

    from core.snmp.protocol import snmp_debug_get, _detect_snmp_version
    from core.snmp.oid_map import OIDS as TOSHIBA_OIDS

    community = "public"
    printer_name = ip
    with store.printers_lock:
        for printer in store.PRINTERS:
            if printer.get("ip") == ip:
                community = printer.get("community", "public") or "public"
                printer_name = printer.get("name") or ip
                break

    detected_version = _detect_snmp_version(ip, community, probe_timeout=2.0)

    health_oids = [
        ("sysDescr", "1.3.6.1.2.1.1.1.0"),
        ("sysUpTime", "1.3.6.1.2.1.1.3.0"),
        ("sysName", "1.3.6.1.2.1.1.5.0"),
        ("printerModel", "1.3.6.1.2.1.43.5.1.1.16.1"),
        ("toshibaModel", TOSHIBA_OIDS.get("model")),
        ("toshibaUptime", TOSHIBA_OIDS.get("uptime")),
    ]
    vendor_oids = [
        ("print_total", TOSHIBA_OIDS.get("print_total")),
        ("print_fc", TOSHIBA_OIDS.get("print_fc")),
        ("print_bw", TOSHIBA_OIDS.get("print_bw")),
        ("print_copy_fc", TOSHIBA_OIDS.get("print_copy_fc")),
        ("print_copy_bw", TOSHIBA_OIDS.get("print_copy_bw")),
        ("print_printer_fc", TOSHIBA_OIDS.get("print_printer_fc")),
        ("print_printer_bw", TOSHIBA_OIDS.get("print_printer_bw")),
        ("print_fax", TOSHIBA_OIDS.get("print_fax")),
        ("print_list", TOSHIBA_OIDS.get("print_list")),
        ("a3_total", TOSHIBA_OIDS.get("a3_total")),
        ("a4_total", TOSHIBA_OIDS.get("a4_total")),
    ]

    diagnostics = {"health": [], "vendor": []}
    request_id = 1
    for label, oid in health_oids + vendor_oids:
        if not oid:
            continue
        bucket = "health" if (label, oid) in health_oids else "vendor"
        row = {
            "label": label,
            "oid": oid,
            "v1": snmp_debug_get(ip, oid, community, timeout=2.0, request_id=request_id, version=1),
            "v2c": snmp_debug_get(ip, oid, community, timeout=2.0, request_id=request_id + 100, version=2),
        }
        diagnostics[bucket].append(row)
        request_id += 1

    with store.data_lock:
        current_data = store.printer_data.get(ip, {}) or {}

    return jsonify({
        "ip": ip,
        "name": printer_name,
        "community": community,
        "detected_snmp_version": detected_version,
        "diagnostics": diagnostics,
        "current_dashboard_data": current_data,
    })


@bp.route('/api/printers/add', methods=['POST'])
def api_add():
    """Add a printer from the JSON body."""

    body = request.get_json() or {}
    ip = (body.get("ip") or "").strip()
    name = (body.get("name") or f"Printer {ip}").strip()
    community = (body.get("community") or "public").strip()
    nickname = (body.get("nickname") or "").strip()
    group = (body.get("group") or "").strip()
    if not ip:
        return jsonify({"error": "IP required"}), 400
    if not _is_valid_ip(ip):
        return jsonify({"error": "IP نامعتبر است"}), 400
    with store.printers_lock:
        if any(p["ip"] == ip for p in store.PRINTERS):
            return jsonify({"error": "already exists"}), 409
        new_p = {"ip": ip, "name": name, "community": community, "nickname": nickname, "group": group}
        store.PRINTERS.append(new_p)
        store.save_printers(store.PRINTERS)
    threading.Thread(
        target=lambda: poll_one(new_p),
        daemon=True
    ).start()
    return jsonify({"status": "added", "ip": ip, "name": name})


@bp.route('/api/printers/bulk-add', methods=['POST'])
def api_bulk_add():
    """Add several printers at once."""

    body = request.get_json() or {}
    items = body.get("printers", [])
    do_scan = body.get("scan", True)
    skip_exist = body.get("skip_existing", True)

    if not items or not isinstance(items, list):
        return jsonify({"error": "آرایه printers الزامی است"}), 400
    if len(items) > 50:
        return jsonify({"error": "حداکثر ۵۰ پرینتر در هر درخواست"}), 400

    from core.store import _normalize_printer
    added, skipped, failed = [], [], []

    for raw in items:
        if not isinstance(raw, dict):
            failed.append({"item": raw, "reason": "فرمت نامعتبر"})
            continue
        p = _normalize_printer(raw)
        ip = p["ip"]
        if not ip:
            failed.append({"item": raw, "reason": "IP خالی است"})
            continue
        if not _is_valid_ip(ip):
            failed.append({"item": raw, "reason": "IP نامعتبر است"})
            continue
        with store.printers_lock:
            exists = any(x["ip"] == ip for x in store.PRINTERS)
        if exists:
            (skipped if skip_exist else failed).append({"ip": ip, "reason": "قبلاً وجود دارد"})
            continue
        with store.printers_lock:
            store.PRINTERS.append(p)
            store.save_printers(store.PRINTERS)
        added.append(p)

    if added:
        def _bg_init(new_printers):
            """Initialize the newly added printers in a background thread."""
            for p in new_printers:
                try:
                    if do_scan:
                        profile = scan_printer_oids(p["ip"], p.get("community", "public"))
                        if not p.get("brand") and profile and profile.get("brand", "unknown") != "unknown":
                            p["brand"] = profile["brand"]
                            with store.printers_lock:
                                for i, x in enumerate(store.PRINTERS):
                                    if x["ip"] == p["ip"]:
                                        store.PRINTERS[i] = _normalize_printer(p)
                                store.save_printers(store.PRINTERS)
                            # After updating the brand in PRINTERS, immediately poll_one to update printer_data
                            poll_one(p)
                        else:
                            # If the brand was already known or was not scanned, do an initial poll again
                            result = collect(p)
                            with store.data_lock:
                                store.printer_data[p["ip"]] = result
                    else:
                        result = collect(p)
                        with store.data_lock:
                            store.printer_data[p["ip"]] = result
                except Exception:
                    log.exception("bulk-add init %s failed", p['ip'])
        threading.Thread(target=_bg_init, args=(list(added),), daemon=True).start()

    return jsonify({
        "total_added": len(added),
        "added": added,
        "skipped": skipped,
        "failed": failed,
    }), (200 if added or skipped else 400)


@bp.route('/api/printers/remove', methods=['POST'])
def api_remove():
    """Remove a printer."""

    ip = (request.get_json() or {}).get("ip", "").strip()
    with store.printers_lock:
        before = len(store.PRINTERS)
        store.PRINTERS[:] = [p for p in store.PRINTERS if p["ip"] != ip]
        if len(store.PRINTERS) == before:
            return jsonify({"error": "not found"}), 404
        store.save_printers(store.PRINTERS)
    with store.data_lock:
        store.printer_data.pop(ip, None)
    # Delete the previous baseline so that if the same IP is assigned to another device later, a false log will not be created.
    store._prev.delete(ip)
    return jsonify({"status": "removed", "ip": ip})


@bp.route('/api/discover/auto-add', methods=['POST'])
def api_auto_add_printer():
    """Add a printer that was found by discovery."""

    data = request.json or {}
    ip = data.get("ip", "").strip()
    community = data.get("community", "public")
    custom_name = data.get("name", "").strip()
    if not ip:
        return jsonify({"error": "ip الزامی است"}), 400
    if not _is_valid_ip(ip):
        return jsonify({"error": "IP نامعتبر است"}), 400
    with store.printers_lock:
        if any(p["ip"] == ip for p in store.PRINTERS):
            return jsonify({"error": f"{ip} قبلاً اضافه شده"}), 409
    try:
        profile = scan_printer_oids(ip, community, force=False)
        if not profile:
            return jsonify({"error": f"پرینتر {ip} پاسخ نداد"}), 404
        s = profile["summary"]
        name = custom_name or f"{s['brand'].upper()} {s['model']}"
        new_printer = {
            "ip": ip,
            "name": name,
            "community": community,
            "brand": s["brand"],
            "device_type": s.get("device_type", "sensor" if s.get("brand") == "sensor" else "unknown"),
            "nickname": "",
        }
        with store.printers_lock:
            store.PRINTERS.append(new_printer)
            store.save_printers(store.PRINTERS)

        def _poll_new():
            """Poll the newly added printer once."""
            result = collect(new_printer)
            with store.data_lock:
                store.printer_data[new_printer["ip"]] = result
        threading.Thread(target=_poll_new, daemon=True).start()
        return jsonify({"status": "added", "printer": new_printer, "summary": s})
    except Exception:
        log.exception("auto-add printer %s failed", ip)
        return jsonify({"error": "خطای داخلی سرور رخ داد"}), 500


@bp.route('/api/printer/<path:ip>/update', methods=['POST'])
def api_update_printer(ip):
    """Update the settings of one printer."""

    if not user_can_access_office(current_user, ip):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json() or {}
    new_name = data.get("name", "").strip()
    new_nickname = data.get("nickname", "").strip()
    new_group = data.get("group", "").strip()

    with store.printers_lock:
        found = False
        for p in store.PRINTERS:
            if p["ip"] == ip:
                if new_name:
                    p["name"] = new_name
                p["nickname"] = new_nickname
                p["group"] = new_group
                found = True
                break

        if not found:
            return jsonify({"error": "printer not found"}), 404

        store.save_printers(store.PRINTERS)

    # Instant data update in memory
    with store.data_lock:
        if ip in store.printer_data:
            if new_name:
                store.printer_data[ip]["name"] = new_name
            store.printer_data[ip]["nickname"] = new_nickname
            store.printer_data[ip]["group"] = new_group

    return jsonify({"status": "ok", "ip": ip})


@bp.route('/api/printer/<path:ip>/toner_reset', methods=['POST'])
@admin_required  # 🔒 manual toner reset: administrative operation (admin)
def toner_reset(ip):
    """Reset the toner counters of one printer."""

    try:
        if not user_can_access_office(current_user, ip):
            return jsonify({"error": "forbidden"}), 403

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "invalid JSON body"}), 400

        color = (data.get('color') or '').strip().lower()
        if not color:
            return jsonify({"error": "color required"}), 400

        if color not in ('black', 'cyan', 'magenta', 'yellow'):
            return jsonify({"error": "invalid color"}), 400

        try:
            new_level = int(data.get('new_level', 100))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid new_level"}), 400

        if new_level < 0 or new_level > 100:
            return jsonify({"error": "new_level must be between 0 and 100"}), 400

        raw_reason = data.get('reason', data.get('replacement_reason'))
        reason_norm = None
        if raw_reason is not None and str(raw_reason).strip() != '':
            r_txt = str(raw_reason).strip().lower()
            if r_txt == 'auto':
                reason_norm = None
            elif r_txt in ('empty', 'depleted', 'low_toner', 'faded'):
                reason_norm = 'empty'
            elif r_txt in ('dirty', 'defect', 'streaks', 'smudge', 'drum', 'preventive', 'partial'):
                reason_norm = 'dirty'
            else:
                return jsonify({"error": "invalid reason (expected 'empty' or 'dirty')"}), 400

        warning_threshold = TONER_ALERT_THRESHOLDS.get('warning', 15)
        critical_threshold = TONER_ALERT_THRESHOLDS.get('critical', 5)
        toner_alert_codes = set()
        raw_chip_before_reset = None

        with store.data_lock:
            printer = store.printer_data.get(ip)
            if printer is None:
                return jsonify({"error": "printer not found"}), 404
            toners = printer.get('toners')
            if not isinstance(toners, dict) or color not in toners:
                return jsonify({"error": "color not available"}), 400
            raw_chip_before_reset = (toners.get(color) or {}).get('raw_level')
            toners[color]['level'] = new_level
            if new_level == 0:
                status = 'empty'
            elif new_level <= critical_threshold:
                status = 'critical'
            elif new_level <= warning_threshold:
                status = 'low'
            else:
                status = 'ok'
            toners[color]['status'] = status
            toners[color]['pages_after_zero'] = 0
            toners[color]['overrun_pages'] = 0
            toners[color]['unregistered_refill_suspected'] = False
            toners[color]['sensor_state'] = 'chip_quarantined'
            toners[color]['chip_quarantined'] = True
            if reason_norm:
                toners[color]['last_reset_reason'] = reason_norm

            if isinstance(printer.get('counters'), dict):
                printer['counters']['pages_since_last_reset'] = 0

            if new_level > warning_threshold:
                # Immediately remove toner-related warnings from active status.
                toner_alert_codes = {
                    ((toner_data.get('index') if isinstance(toner_data, dict) else None) or toner_color)
                    for toner_color, toner_data in toners.items()
                }
                toner_alert_code_strs = {str(code) for code in toner_alert_codes}
                toner_alert_code_strs.add(f"toner:{color}")
                printer['alerts'] = [
                    alert for alert in (printer.get('alerts') or [])
                    if str(alert.get('code')) not in toner_alert_code_strs
                ]

        prev = store._prev.get(ip) or {}
        counters = printer.get('counters', {}) if isinstance(printer.get('counters'), dict) else {}
        current_total = counters.get('total', 0)
        current_full_color = counters.get('full_color')
        current_black_white = counters.get('black_white')

        learned_yield = prev.get('yield_per_page', 2000)
        if reason_norm != 'dirty' and prev.get('force_estimate') and prev.get('override_start_total') is not None:
            try:
                pages_since_last_override = int(current_total) - int(prev.get('override_start_total'))
                consumed_pct = int(prev.get('override_start_toner', 100) or 100) - (
                    0 if reason_norm == 'empty' else int(prev.get('toner_level') or 0)
                )
                if pages_since_last_override > 0 and consumed_pct > 0:
                    estimated_yield = int(round(pages_since_last_override * 100.0 / consumed_pct))
                    if 300 <= estimated_yield <= 600000:
                        current_yield = int(prev.get('yield_per_page', 2000) or 2000)
                        if current_yield == 2000:
                            learned_yield = estimated_yield
                        else:
                            learned_yield = int(round((current_yield * 0.7) + (estimated_yield * 0.3)))
                        log.info(
                            "manual toner reset yield update for %s: %s -> %s (pages=%s, consumed_pct=%s)",
                            ip, current_yield, learned_yield, pages_since_last_override, consumed_pct,
                        )
            except Exception as exc:
                log.exception("manual toner reset yield learning failed for %s: %s", ip, exc)

        # Register manual refill in Yield Engine first so per-color cycle_learn/catalog yield
        # is immediately reflected in both toner_overrides[color] and API/UI state.
        refill_meta = {}
        try:
            from core.yield_engine import register_manual_refill
            device_info = printer.get('device') if isinstance(printer.get('device'), dict) else {}
            toner_info = toners.get(color) if isinstance(toners, dict) else {}
            refill_meta = register_manual_refill(
                ip=ip,
                color=color,
                printer_model=device_info.get('model') or 'Unknown',
                cartridge_name=(toner_info or {}).get('name') or color,
                new_level=new_level,
                counters={
                    'total': current_total,
                    'full_color': current_full_color,
                    'black_white': current_black_white,
                },
                device_type=printer.get('device_type', 'unknown'),
                replacement_reason=reason_norm,
                quarantine_chip=True,
            ) or {}
        except Exception as exc:
            log.exception('yield_engine manual refill registration failed for %s/%s: %s', ip, color, exc)

        color_yield = int(refill_meta.get('yield_per_page') or (toners.get(color) or {}).get('capacity_pages') or learned_yield or 2000)
        if color == 'black' and refill_meta.get('yield_per_page'):
            learned_yield = color_yield
        if isinstance(toners, dict) and isinstance(toners.get(color), dict) and refill_meta:
            toners[color]['capacity_pages'] = color_yield
            toners[color]['yield_per_page'] = color_yield
            toners[color]['capacity_source'] = refill_meta.get('yield_source') or toners[color].get('capacity_source', 'default')
            toners[color]['yield_confidence'] = refill_meta.get('confidence') or toners[color].get('yield_confidence', 'low')

        toner_alert_code_strs = {str(code) for code in toner_alert_codes}
        if new_level > warning_threshold:
            cleared_alert_codes = [
                code for code in (prev.get('alert_codes', []) or [])
                if str(code) not in toner_alert_code_strs
            ]
        else:
            cleared_alert_codes = prev.get('alert_codes', [])

        # ✅ Bug fix "manual value return to default after a few seconds":
        # Each color has its own independent override slot (toner_overrides), including its own per-color yield_per_page
        # and chip quarantine metadata (protecting Yield Engine when refilled without installing a new chip).
        from core.collectors.base import _normalize_overrides as _norm_ov
        overrides = _norm_ov(prev)
        chip_aligned = (
            raw_chip_before_reset is not None
            and isinstance(raw_chip_before_reset, (int, float))
            and 0 <= int(raw_chip_before_reset) <= 100
            and abs(int(raw_chip_before_reset) - new_level) <= 2
        )
        raw_black_level = (
            int(raw_chip_before_reset)
            if (
                raw_chip_before_reset is not None
                and isinstance(raw_chip_before_reset, (int, float))
                and 0 <= int(raw_chip_before_reset) <= 100
            )
            else None
        )
        overrides[color] = {
            'start_total': current_total,
            'start_toner': new_level,
            'yield_per_page': color_yield,
            'mode': 'manual_reset',
            'reset_reason': reason_norm or 'auto',
            'chip_level_at_reset': raw_chip_before_reset,
            'chip_aligned_at_reset': bool(chip_aligned),
            'chip_quarantined': True,
        }

        with tracking_lock(ip):
            # Cartridge replaced by the manual reset: system usage counting restarts for this colour.
            latest = store._prev.get(ip) or {}
            usage_map = dict(latest.get('supply_usage') or {}) if isinstance(latest.get('supply_usage'), dict) else {}
            genuine_ids = genuine_cartridge_ids(printer.get('cartridge_ids'), printer.get('cartridge_identity_type'))
            usage_map[color] = new_record(
                counter_basis(color, counters),
                genuine_ids.get(color),
                utc_now_iso()[:10],
            )

            new_prev = {
                **prev,
                'supply_usage': usage_map,
                # A manual reset should sync the counter baseline with the current snapshot;
                # Otherwise, the next poll may make a PRINT or COUNTER_RESET error.
                'print_total': current_total,
                'full_color': current_full_color if current_full_color is not None else prev.get('full_color'),
                'black_white': current_black_white if current_black_white is not None else prev.get('black_white'),
                # Preserve the real SNMP chip reading (or None on chip-less models) instead of
                # writing the virtual override level into raw prev['toner_level'].
                'toner_level': raw_black_level if color == 'black' else prev.get('toner_level'),
                'manual_override': 1,
                'override_color': color,
                'override_base_level': new_level,
                'override_start_total': current_total,
                'override_start_toner': new_level,
                'yield_per_page': learned_yield,
                'force_estimate': prev.get('force_estimate', 0),
                'yield_learning_failures': prev.get('yield_learning_failures', 0),
                'alert_codes': cleared_alert_codes,
                'last_alert_codes': cleared_alert_codes,
                'uptime': prev.get('uptime'),
                'toner_overrides': overrides,
            }
            store._prev.set(ip, new_prev)

        username = current_user.username if current_user.is_authenticated else 'سیستم'
        reason_fa_map = {
            'empty': 'خالی شدن تونر',
            'dirty': 'کثیف بودن چاپ / خرابی قطعات کارتریج',
        }
        reason_suffix = f" (علت: {reason_fa_map[reason_norm]})" if reason_norm in reason_fa_map else ""
        add_event(ip, 'REFILL', {
            'message': f'تنظیم دستی: کارتریج {color} به {new_level}% تنظیم شد{reason_suffix}',
            'severity': 'info',
            'username': username,
            'manual_reset': True,
            'auto_detected': False,
            'reset_color': color,
            'replacement_reason': reason_norm or 'auto',
            'set_level': new_level,
            'total_at_reset': current_total,
            'yield_per_page_at_reset': color_yield,
            'chip_level_at_reset': raw_chip_before_reset,
        })

        return jsonify({
            'status': 'ok',
            'reason': reason_norm or 'auto',
            'yield_per_page': color_yield,
            'chip_quarantined': True,
        })
    except Exception:
        log.exception('toner_reset failed for %s', ip)
        return jsonify({"error": "internal server error"}), 500