# =============================================================================
# @file:         web/routes/yield_status.py
# @brief:        API routes for cartridge yield status and reports.
# @details:      Responsibilities: Return yield status and reports.
#                Main API: api_yield_status(), api_yield_report()
#                Side effects: defines HTTP routes
# @dependencies: internal: core.yield_engine, web.auth | external: flask
# @notes:        None.
# =============================================================================

"""Cartridge yield status API routes."""

from flask import Blueprint, jsonify, request

from core.yield_engine import get_yield_status, write_yield_status_report
from web.auth import admin_required

bp = Blueprint("yield_status", __name__)


@bp.route("/api/yield/status")
@admin_required
def api_yield_status():
    """Yield Engine status report per-cartridge/per-color."""
    ip = (request.args.get("ip") or "").strip() or None
    rows = get_yield_status(ip=ip)
    by_learning_state = {
        "prior": sum(1 for r in rows if r.get("learning_state") == "prior"),
        "learning": sum(1 for r in rows if r.get("learning_state") == "learning"),
        "converged": sum(1 for r in rows if r.get("learning_state") == "converged"),
        "suspect": sum(1 for r in rows if r.get("learning_state") == "suspect"),
    }
    by_method = {}
    for r in rows:
        m_name = r.get("capacity_method")
        if m_name:
            by_method[m_name] = by_method.get(m_name, 0) + 1
    by_sensor_state = {
        "healthy": sum(1 for r in rows if (r.get("sensor_state") or "healthy") == "healthy"),
        "chip_quarantined": sum(1 for r in rows if r.get("sensor_state") == "chip_quarantined"),
        "frozen_chip": sum(1 for r in rows if r.get("sensor_state") == "frozen_chip"),
        "overrun_alert": sum(1 for r in rows if r.get("sensor_state") == "overrun_alert"),
    }
    summary = {
        "total_cartridges": len(rows),
        "high": sum(1 for r in rows if r.get("confidence") == "high"),
        "medium": sum(1 for r in rows if r.get("confidence") == "medium"),
        "low": sum(1 for r in rows if r.get("confidence") == "low"),
        "default": sum(1 for r in rows if r.get("yield_source") == "default"),
        "catalog": sum(1 for r in rows if r.get("yield_source") == "catalog"),
        "device_capacity": sum(1 for r in rows if r.get("yield_source") == "device_capacity"),
        "shared_profile": sum(1 for r in rows if r.get("yield_source") == "shared_profile"),
        "auto_learn": sum(1 for r in rows if r.get("yield_source") == "auto_learn"),
        "cycle_learn": sum(1 for r in rows if r.get("yield_source") == "cycle_learn"),
        "by_learning_state": by_learning_state,
        "by_method": by_method,
        "by_sensor_state": by_sensor_state,
    }
    return jsonify({"summary": summary, "items": rows})


@bp.route("/api/yield/report", methods=["POST"])
@admin_required
def api_yield_report():
    """Reconstruction of runtime file `yield_status_report.txt`."""
    write_yield_status_report()
    return jsonify({"status": "ok", "file": "yield_status_report.txt"})
