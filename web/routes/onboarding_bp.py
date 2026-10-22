"""
PrintGuard Onboarding Tours REST API (Phase J.9)
Endpoints to query, complete, dismiss, and reset Shepherd.js onboarding tours per user.
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify, request
from flask_login import current_user

from core.dashboard.onboarding import (
    get_user_tours_status,
    set_tour_status,
    set_user_onboarding_disabled,
    reset_user_tours,
    KNOWN_TOURS,
)

bp = Blueprint("onboarding", __name__)
log = logging.getLogger("PrintGuard.OnboardingRoute")


def _get_current_user_id() -> int:
    if current_user and current_user.is_authenticated:
        return getattr(current_user, "id", 0) or 0
    return 0


@bp.route("/api/onboarding/status", methods=["GET"])
def api_onboarding_status():
    """Retrieve current user's tour completion and dismissal states."""
    user_id = _get_current_user_id()
    data = get_user_tours_status(user_id)
    return jsonify({
        "success": True,
        "user_id": user_id,
        "available_tours": KNOWN_TOURS,
        **data,
    })


@bp.route("/api/onboarding/complete", methods=["POST"])
def api_onboarding_complete():
    """Mark a tour as completed, dismissed, or toggle all tours off."""
    user_id = _get_current_user_id()
    payload = request.get_json(silent=True) or {}
    tour_name = payload.get("tour")
    action = payload.get("action", "complete")

    if action == "disable_all":
        success = set_user_onboarding_disabled(user_id, disabled=True)
        return jsonify({"success": success, "disabled_all": True})

    if not tour_name:
        return jsonify({"error": "نام تور الزامی است."}), 400

    if action == "dismiss":
        success = set_tour_status(user_id, tour_name, completed=False, dismissed=True)
    else:
        success = set_tour_status(user_id, tour_name, completed=True, dismissed=False)

    return jsonify({"success": success, "tour": tour_name, "action": action})


@bp.route("/api/onboarding/reset", methods=["POST"])
def api_onboarding_reset():
    """Reset tour history for current user so tours can be re-run."""
    user_id = _get_current_user_id()
    payload = request.get_json(silent=True) or {}
    tour_name = payload.get("tour")

    success = reset_user_tours(user_id, tour_name=tour_name)
    set_user_onboarding_disabled(user_id, disabled=False)
    return jsonify({"success": success, "tour_reset": tour_name or "all"})
