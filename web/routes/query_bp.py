"""
PrintGuard Query Builder REST API (Phase J.8)
Endpoints for prebuilt catalog, query previewing, and security validation.
"""

from __future__ import annotations

import logging
from flask import Blueprint, jsonify, request
from flask_login import current_user

from core.db import get_connection
from core.datasources.base import DataSourceError, DataSourceSecurityError
from core.dashboard.query_engine import (
    get_prebuilt_catalog,
    execute_query,
    validate_and_sanitize_sql,
)

bp = Blueprint("query_engine", __name__)
log = logging.getLogger("PrinterMonitor.QueryRoute")


@bp.route('/api/query/prebuilt-catalog', methods=['GET'])
def api_prebuilt_catalog():
    """Returns the catalog of all available prebuilt metrics and queries."""
    return jsonify({"catalog": get_prebuilt_catalog()})


@bp.route('/api/query/preview', methods=['POST'])
def api_query_preview():
    """Executes a query in prebuilt, visual, or sql mode and returns tabular preview."""
    if not current_user.is_authenticated:
        return jsonify({"error": "احراز هویت الزامی است."}), 401

    query_spec = request.get_json() or {}
    conn = get_connection(timeout=10.0)
    try:
        result = execute_query(conn, query_spec)
        return jsonify(result)
    except DataSourceSecurityError as se:
        return jsonify({"error": f"خطای امنیتی کوئری: {se}"}), 403
    except DataSourceError as de:
        return jsonify({"error": str(de)}), 400
    except Exception as e:
        log.error(f"Error previewing query: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/query/validate', methods=['POST'])
def api_query_validate():
    """Validates query syntax and security without executing."""
    if not current_user.is_authenticated:
        return jsonify({"error": "احراز هویت الزامی است."}), 401

    payload = request.get_json() or {}
    mode = payload.get("mode", "sql")

    if mode == "sql":
        raw_sql = payload.get("raw_sql", "")
        try:
            sanitized = validate_and_sanitize_sql(raw_sql)
            return jsonify({"status": "valid", "sanitized_sql": sanitized})
        except Exception as e:
            return jsonify({"status": "invalid", "error": str(e)}), 400

    return jsonify({"status": "valid"})
