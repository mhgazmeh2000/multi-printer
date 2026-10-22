"""
PrintGuard Plugin Management REST API (Phase J.5)
Enables/disables plugins dynamically and lists installed extensions.
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify, request
from flask_login import current_user

from core.plugins.loader import plugin_manager

bp = Blueprint("plugins_mgr", __name__)
log = logging.getLogger("PrinterMonitor.PluginsRoute")


@bp.route('/api/plugins', methods=['GET'])
def api_list_plugins():
    """Lists all installed plugins and their runtime statuses."""
    org_id = request.args.get("org_id", "default")
    plugins = plugin_manager.list_plugins(org_id=org_id)
    return jsonify({"plugins": plugins, "total": len(plugins)})


@bp.route('/api/plugins/<name>/enable', methods=['POST'])
def api_enable_plugin(name: str):
    """Enables a plugin at runtime without application restart."""
    if not current_user.is_authenticated or current_user.role != "admin":
        return jsonify({"error": "فقط مدیر ارشد مجاز به فعال‌سازی پلاگین است."}), 403

    payload = request.get_json() or {}
    org_id = payload.get("org_id", "default")

    success = plugin_manager.enable_plugin(name, org_id=org_id)
    if not success:
        return jsonify({"error": f"فعال‌سازی پلاگین '{name}' ناموفق بود."}), 400

    return jsonify({"status": "success", "message": f"پلاگین '{name}' با موفقیت فعال شد."})


@bp.route('/api/plugins/<name>/disable', methods=['POST'])
def api_disable_plugin(name: str):
    """Disables a plugin at runtime without application restart."""
    if not current_user.is_authenticated or current_user.role != "admin":
        return jsonify({"error": "فقط مدیر ارشد مجاز به غیرفعال‌سازی پلاگین است."}), 403

    payload = request.get_json() or {}
    org_id = payload.get("org_id", "default")

    success = plugin_manager.disable_plugin(name, org_id=org_id)
    if not success:
        return jsonify({"error": f"غیرفعال‌سازی پلاگین '{name}' ناموفق بود."}), 400

    return jsonify({"status": "success", "message": f"پلاگین '{name}' با موفقیت غیرفعال شد."})
