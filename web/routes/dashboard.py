"""
PrintGuard Dashboard Routes & REST API (Phase J.1 / J.3)
Handles dashboard viewing, Builder integration, panel catalog, and full CRUD REST APIs.
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from config.settings import POLL_INTERVAL
from core.db import get_connection
from core.dashboard.panel_registry import panel_registry
from core.dashboard.manager import (
    create_dashboard,
    get_dashboard,
    list_dashboards,
    update_dashboard,
    delete_dashboard,
    add_panel,
    update_panel,
    delete_panel,
    get_dashboard_permissions,
    set_dashboard_permission,
    check_dashboard_access,
    DashboardNotFoundError,
    DashboardPermissionError,
)

bp = Blueprint("dashboard", __name__)
log = logging.getLogger("PrinterMonitor.DashboardRoute")


# ─── Web Views ────────────────────────────────────────────────────────────────

@bp.route('/')
def index():
    """Main live printer monitoring dashboard (retained as per Decision J-D8)."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if not current_user.is_verified:
        return render_template("pending_verification.html", load_dashboard_scripts=False)
    username = current_user.username if current_user.is_authenticated else "میهمان"
    return render_template("dashboard.html", poll_interval=POLL_INTERVAL, username=username)


@bp.route('/dashboards/preview-panels')
def preview_panels():
    """Showcase & interactive verification of the 8 base ECharts panels (Phase J.1)."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    username = current_user.username if current_user.is_authenticated else "میهمان"
    return render_template("panels_preview.html", poll_interval=POLL_INTERVAL, username=username)


@bp.route('/dashboards')
def dashboards_list_view():
    """Dashboard catalog and management page."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    username = current_user.username if current_user.is_authenticated else "میهمان"
    return render_template("dashboards_list.html", poll_interval=POLL_INTERVAL, username=username)


@bp.route('/dashboards/<uid>')
def dashboard_view(uid: str):
    """View a specific dashboard with its panels rendered."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    user_id = current_user.id
    user_role = current_user.role

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="view"):
            return render_template("dashboards_list.html", error="شما اجازه مشاهده این داشبورد را ندارید."), 403

        dash = get_dashboard(conn, uid)
        if not dash:
            return redirect(url_for("dashboard.dashboards_list_view"))

        can_edit = check_dashboard_access(conn, uid, user_id, user_role, action="edit")
        username = current_user.username
        return render_template("dashboard_view.html", dashboard=dash, can_edit=can_edit, poll_interval=POLL_INTERVAL, username=username)
    finally:
        conn.close()


@bp.route('/dashboards/<uid>/edit')
def dashboard_edit(uid: str):
    """Interactive drag & drop Dashboard Builder (Phase J.4)."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    user_id = current_user.id
    user_role = current_user.role

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="edit"):
            return redirect(url_for("dashboard.dashboard_view", uid=uid))

        dash = get_dashboard(conn, uid)
        if not dash:
            return redirect(url_for("dashboard.dashboards_list_view"))

        username = current_user.username
        return render_template("dashboard_builder.html", dashboard=dash, poll_interval=POLL_INTERVAL, username=username)
    finally:
        conn.close()


# ─── REST API: Panels Catalog ─────────────────────────────────────────────────

@bp.route('/api/panels/registry')
def api_panel_registry():
    """Returns the catalog of all available dashboard panels with schemas."""
    return jsonify({
        "panels": panel_registry.list_panels(),
        "total": len(panel_registry.list_panels()),
    })


# ─── REST API: Dashboard CRUD ─────────────────────────────────────────────────

@bp.route('/api/dashboards', methods=['GET'])
def api_list_dashboards():
    """Lists all dashboards accessible to the current user."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        items = list_dashboards(conn, user_id=user_id, user_role=user_role)
        return jsonify({"dashboards": items, "total": len(items)})
    finally:
        conn.close()


@bp.route('/api/dashboards', methods=['POST'])
def api_create_dashboard():
    """Creates a new dashboard."""
    if not current_user.is_authenticated or current_user.role not in ("admin", "manager"):
        return jsonify({"error": "شما اجازه ساخت داشبورد جدید را ندارید."}), 403

    payload = request.get_json() or {}
    title = payload.get("title")
    if not title:
        return jsonify({"error": "فیلد title الزامی است."}), 400

    conn = get_connection(timeout=10.0)
    try:
        dash = create_dashboard(
            conn=conn,
            title=title,
            description=payload.get("description"),
            layout=payload.get("layout"),
            variables=payload.get("variables"),
            owner_id=current_user.id,
            org_id=payload.get("org_id", "default"),
        )
        conn.commit()
        return jsonify(dash), 201
    except Exception as e:
        conn.rollback()
        log.error(f"Error creating dashboard: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/dashboards/<uid>', methods=['GET'])
def api_get_dashboard(uid: str):
    """Retrieves a single dashboard with all panels."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="view"):
            return jsonify({"error": "دسترسی به این داشبورد مجاز نیست."}), 403

        dash = get_dashboard(conn, uid)
        if not dash:
            return jsonify({"error": "داشبورد یافت نشد."}), 404
        return jsonify(dash)
    finally:
        conn.close()


@bp.route('/api/dashboards/<uid>', methods=['PUT'])
def api_update_dashboard(uid: str):
    """Updates dashboard metadata and layout."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="edit"):
            return jsonify({"error": "شما اجازه ویرایش این داشبورد را ندارید."}), 403

        payload = request.get_json() or {}
        update_dashboard(
            conn=conn,
            uid=uid,
            title=payload.get("title"),
            description=payload.get("description"),
            layout=payload.get("layout"),
            variables=payload.get("variables"),
            is_starred=payload.get("is_starred"),
        )
        conn.commit()
        return jsonify({"status": "success", "message": "داشبورد با موفقیت به‌روزرسانی شد."})
    except DashboardNotFoundError:
        return jsonify({"error": "داشبورد یافت نشد."}), 404
    except Exception as e:
        conn.rollback()
        log.error(f"Error updating dashboard {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/dashboards/<uid>', methods=['DELETE'])
def api_delete_dashboard(uid: str):
    """Deletes a dashboard."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="delete"):
            return jsonify({"error": "شما اجازه حذف این داشبورد را ندارید."}), 403

        success = delete_dashboard(conn, uid)
        if not success:
            return jsonify({"error": "داشبورد یافت نشد."}), 404
        conn.commit()
        return jsonify({"status": "success", "message": "داشبورد با موفقیت حذف شد."})
    except Exception as e:
        conn.rollback()
        log.error(f"Error deleting dashboard {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ─── REST API: Panels CRUD ────────────────────────────────────────────────────

@bp.route('/api/dashboards/<uid>/panels', methods=['POST'])
def api_add_panel(uid: str):
    """Adds a new panel to the dashboard."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="edit"):
            return jsonify({"error": "شما اجازه ویرایش این داشبورد را ندارید."}), 403

        payload = request.get_json() or {}
        panel_type = payload.get("type")
        title = payload.get("title")
        if not panel_type or not title:
            return jsonify({"error": "فیلدهای type و title الزامی هستند."}), 400

        panel = add_panel(
            conn=conn,
            dashboard_uid=uid,
            panel_type=panel_type,
            title=title,
            description=payload.get("description"),
            query=payload.get("query"),
            config=payload.get("config"),
            grid_pos=payload.get("grid_pos"),
        )
        conn.commit()
        return jsonify(panel), 201
    except DashboardNotFoundError:
        return jsonify({"error": "داشبورد یافت نشد."}), 404
    except Exception as e:
        conn.rollback()
        log.error(f"Error adding panel to {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/dashboards/<uid>/panels/<panel_uid>', methods=['PUT'])
def api_update_panel(uid: str, panel_uid: str):
    """Updates an existing panel."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="edit"):
            return jsonify({"error": "شما اجازه ویرایش این داشبورد را ندارید."}), 403

        payload = request.get_json() or {}
        success = update_panel(
            conn=conn,
            panel_uid=panel_uid,
            title=payload.get("title"),
            description=payload.get("description"),
            query=payload.get("query"),
            config=payload.get("config"),
            grid_pos=payload.get("grid_pos"),
        )
        if not success:
            return jsonify({"error": "پنل یافت نشد."}), 404
        conn.commit()
        return jsonify({"status": "success", "message": "پنل با موفقیت به‌روزرسانی شد."})
    except Exception as e:
        conn.rollback()
        log.error(f"Error updating panel {panel_uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/dashboards/<uid>/panels/<panel_uid>', methods=['DELETE'])
def api_delete_panel(uid: str, panel_uid: str):
    """Deletes a panel from the dashboard."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="edit"):
            return jsonify({"error": "شما اجازه ویرایش این داشبورد را ندارید."}), 403

        success = delete_panel(conn, panel_uid)
        if not success:
            return jsonify({"error": "پنل یافت نشد."}), 404
        conn.commit()
        return jsonify({"status": "success", "message": "پنل با موفقیت حذف شد."})
    except Exception as e:
        conn.rollback()
        log.error(f"Error deleting panel {panel_uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ─── REST API: Permissions ────────────────────────────────────────────────────

@bp.route('/api/dashboards/<uid>/permissions', methods=['GET'])
def api_get_dashboard_permissions(uid: str):
    """Gets dashboard permission entries."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="admin"):
            return jsonify({"error": "دسترسی مدیریت این داشبورد برای شما مجاز نیست."}), 403

        perms = get_dashboard_permissions(conn, uid)
        return jsonify({"permissions": perms, "total": len(perms)})
    finally:
        conn.close()


@bp.route('/api/dashboards/<uid>/permissions', methods=['POST'])
def api_set_dashboard_permissions(uid: str):
    """Grants or updates a permission entry on the dashboard."""
    user_id = current_user.id if current_user.is_authenticated else None
    user_role = current_user.role if current_user.is_authenticated else "viewer"

    conn = get_connection(timeout=10.0)
    try:
        if not check_dashboard_access(conn, uid, user_id, user_role, action="admin"):
            return jsonify({"error": "دسترسی مدیریت این داشبورد برای شما مجاز نیست."}), 403

        payload = request.get_json() or {}
        role = payload.get("role")
        target_user_id = payload.get("user_id")
        target_group_id = payload.get("group_id")

        if not role:
            return jsonify({"error": "فیلد role الزامی است."}), 400

        set_dashboard_permission(
            conn=conn,
            dashboard_uid=uid,
            role=role,
            user_id=target_user_id,
            group_id=target_group_id,
        )
        conn.commit()
        return jsonify({"status": "success", "message": "مجوز دسترسی اعمال شد."})
    except Exception as e:
        conn.rollback()
        log.error(f"Error setting permission on {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
