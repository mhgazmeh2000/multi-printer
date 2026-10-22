"""
PrintGuard Data Sources Routes & REST API (Phase J.6 / J.7)
Endpoints for configuring, testing, and querying data sources.
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from config.settings import POLL_INTERVAL
from core.db import get_connection
from core.datasources.base import DataSourceError, DataSourceSecurityError
from core.datasources.manager import (
    datasource_registry,
    create_datasource,
    get_datasource,
    list_datasources,
    update_datasource,
    delete_datasource,
    test_datasource_connection,
    execute_datasource_query,
)

bp = Blueprint("datasources", __name__)
log = logging.getLogger("PrinterMonitor.DataSourcesRoute")


# ─── Web Views ────────────────────────────────────────────────────────────────

@bp.route('/datasources')
def datasources_page():
    """Management view for external and internal data sources."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    username = current_user.username if current_user.is_authenticated else "میهمان"
    return render_template("datasources.html", poll_interval=POLL_INTERVAL, username=username)


# ─── REST APIs: Drivers & Catalogs ───────────────────────────────────────────

@bp.route('/api/datasources/drivers', methods=['GET'])
def api_list_drivers():
    """Returns all registered data source drivers and their schemas."""
    drivers = datasource_registry.list_drivers()
    return jsonify({"drivers": drivers, "total": len(drivers)})


@bp.route('/api/datasources', methods=['GET'])
def api_list_datasources():
    """Lists all configured data sources."""
    org_id = request.args.get("org_id", "default")
    conn = get_connection(timeout=10.0)
    try:
        items = list_datasources(conn, org_id=org_id, mask=True)
        return jsonify({"datasources": items, "total": len(items)})
    finally:
        conn.close()


@bp.route('/api/datasources', methods=['POST'])
def api_create_datasource():
    """Creates a new data source."""
    if not current_user.is_authenticated or current_user.role not in ("admin", "manager"):
        return jsonify({"error": "شما اجازه افزودن منبع داده جدید را ندارید."}), 403

    payload = request.get_json() or {}
    name = payload.get("name")
    ds_type = payload.get("type")
    config = payload.get("config", {})

    if not name or not ds_type:
        return jsonify({"error": "فیلدهای name و type الزامی هستند."}), 400

    conn = get_connection(timeout=10.0)
    try:
        ds = create_datasource(
            conn=conn,
            name=name,
            ds_type=ds_type,
            config=config,
            is_default=payload.get("is_default", False),
            org_id=payload.get("org_id", "default"),
        )
        conn.commit()
        return jsonify(ds), 201
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        conn.rollback()
        log.error(f"Error creating data source: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/datasources/<uid>', methods=['GET'])
def api_get_datasource(uid: str):
    """Retrieves a single data source."""
    conn = get_connection(timeout=10.0)
    try:
        ds = get_datasource(conn, uid, mask=True)
        if not ds:
            return jsonify({"error": "منبع داده یافت نشد."}), 404
        return jsonify(ds)
    finally:
        conn.close()


@bp.route('/api/datasources/<uid>', methods=['PUT'])
def api_update_datasource(uid: str):
    """Updates a data source configuration."""
    if not current_user.is_authenticated or current_user.role not in ("admin", "manager"):
        return jsonify({"error": "شما اجازه ویرایش منبع داده را ندارید."}), 403

    payload = request.get_json() or {}
    conn = get_connection(timeout=10.0)
    try:
        success = update_datasource(
            conn=conn,
            uid=uid,
            name=payload.get("name"),
            config=payload.get("config"),
            is_default=payload.get("is_default"),
        )
        if not success:
            return jsonify({"error": "منبع داده یافت نشد."}), 404
        conn.commit()
        return jsonify({"status": "success", "message": "منبع داده با موفقیت به‌روزرسانی شد."})
    except Exception as e:
        conn.rollback()
        log.error(f"Error updating data source {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/datasources/<uid>', methods=['DELETE'])
def api_delete_datasource(uid: str):
    """Deletes a data source."""
    if not current_user.is_authenticated or current_user.role != "admin":
        return jsonify({"error": "فقط مدیر ارشد مجاز به حذف منبع داده است."}), 403

    conn = get_connection(timeout=10.0)
    try:
        success = delete_datasource(conn, uid)
        if not success:
            return jsonify({"error": "منبع داده یافت نشد."}), 404
        conn.commit()
        return jsonify({"status": "success", "message": "منبع داده با موفقیت حذف شد."})
    except Exception as e:
        conn.rollback()
        log.error(f"Error deleting data source {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/datasources/<uid>/test', methods=['POST'])
def api_test_datasource(uid: str):
    """Tests connectivity to the data source."""
    if not current_user.is_authenticated:
        return jsonify({"error": "احراز هویت الزامی است."}), 401

    conn = get_connection(timeout=10.0)
    try:
        res = test_datasource_connection(conn, uid)
        return jsonify(res)
    except DataSourceError as de:
        return jsonify({"status": "error", "message": str(de)}), 400
    except Exception as e:
        log.error(f"Connection test error for {uid}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/datasources/<uid>/query', methods=['POST'])
def api_query_datasource(uid: str):
    """Executes a query against the data source."""
    if not current_user.is_authenticated:
        return jsonify({"error": "احراز هویت الزامی است."}), 401

    query_params = request.get_json() or {}
    conn = get_connection(timeout=10.0)
    try:
        res = execute_datasource_query(conn, uid, query_params)
        return jsonify(res)
    except DataSourceSecurityError as se:
        return jsonify({"error": f"خطای امنیتی: {se}"}), 403
    except DataSourceError as de:
        return jsonify({"error": str(de)}), 400
    except Exception as e:
        log.error(f"Query execution error for {uid}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
