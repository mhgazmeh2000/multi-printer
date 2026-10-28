# =============================================================================
# @file:         web/routes/system.py
# @brief:        System endpoints: current status and an on-demand poll trigger.
# @details:      Responsibilities: Report status and start a poll cycle on request.
#                Main API: api_status(), api_poll_now()
#                Side effects: starts threads, defines HTTP routes
# @dependencies: internal: config.settings, core, core.poller | external: flask, flask_wtf
# @notes:        None.
# =============================================================================

"""System status and manual poll API routes."""

import logging

from flask import Blueprint, jsonify

from core import store
from config.settings import POLL_INTERVAL

log = logging.getLogger("PrinterMonitor")

bp = Blueprint("system", __name__)


@bp.route('/api/status')
def api_status():
    # 🔒 Security: Removed host_ip and dashboard_url — server internal IP leak
    # It is not necessary for the client (and in the past for guest users).
    # The front dashboard uses the same origin and does not need host_ip.
    """Return the application status."""

    from flask_wtf.csrf import generate_csrf
    return jsonify({
        "status":        "running",
        "poll_interval": POLL_INTERVAL,
        # Fresh CSRF Token: Dashboard is long-lived page and meta token after
        # WTF_CSRF_TIME_LIMIT expires (apply 400 to all POSTs).
        # The frontend silently updates the status of the token with each response.
        # Safe to expose on this endpoint: same-origin + logged in + no CORS.
        "csrf_token":    generate_csrf(),
        **store.poll_stats,
    })


@bp.route('/api/poll/now', methods=['POST'])
def api_poll_now():
    """Start a poll cycle immediately."""

    import threading
    from core.poller import poll_all, _polling_lock

    if _polling_lock.locked():
        log.warning('Manual pull request rejected: poll_all is already running')
        return jsonify({"status": "busy", "message": "Pull already in progress", "running": True})

    log.info('Manual pull requested via API')
    try:
        threading.Thread(target=poll_all, daemon=True).start()
        return jsonify({"status": "started"})
    except Exception:
        log.exception('Failed to start manual pull')
        # 🔒 Security: Do not return internal error details (str(e)) to the client
        return jsonify({"status": "error", "error": "internal server error"}), 500
