"""
PrintGuard Alert Engine REST API (Phase J.11)
Endpoints for active alert retrieval, severity filtering, details modal, and summary stats.
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify, request
from flask_login import current_user

from core.alerting.alert_service import (
    get_active_alerts,
    get_alert_summary,
    get_alert_by_id,
)

bp = Blueprint("alerts_engine", __name__)
log = logging.getLogger("PrintGuard.AlertsRoute")


@bp.route("/api/alerts/active", methods=["GET"])
def api_active_alerts():
    """
    Retrieve active system and device alerts.
    Query params:
      - severity: 'critical', 'warning', 'info', or 'all'
      - printer: IP or device name
      - limit: max alerts count (default 50)
      - since_hours: lookup window in hours (default 72)
    """
    severity = request.args.get("severity")
    printer = request.args.get("printer")
    limit = min(int(request.args.get("limit", 50)), 200)
    since_hours = int(request.args.get("since_hours", 72))

    alerts = get_active_alerts(
        severity=severity,
        printer_ip=printer,
        limit=limit,
        since_hours=since_hours,
    )
    return jsonify({
        "success": True,
        "count": len(alerts),
        "alerts": alerts,
    })


@bp.route("/api/alerts/summary", methods=["GET"])
def api_alert_summary():
    """Return summary count of active alerts by severity and category."""
    since_hours = int(request.args.get("since_hours", 72))
    summary = get_alert_summary(since_hours=since_hours)
    return jsonify({
        "success": True,
        "summary": summary,
    })


@bp.route("/api/alerts/<alert_id>", methods=["GET"])
def api_alert_detail(alert_id: str):
    """Return full details and suggested resolution for a specific alert."""
    alert = get_alert_by_id(alert_id)
    if not alert:
        return jsonify({"error": "هشدار مورد نظر یافت نشد."}), 404
    return jsonify({
        "success": True,
        "alert": alert,
    })
