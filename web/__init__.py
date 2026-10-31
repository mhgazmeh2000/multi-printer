# =============================================================================
# @file:         web/__init__.py
# @brief:        Flask application factory that creates the app and registers the blueprints.
# @details:      Responsibilities: Build the app, configure extensions and register all routes.
#                Main API: create_app()
#                Side effects: none at import time
# @dependencies: internal: config, web.auth, web.routes.dashboard, web.routes.discover,
#                web.routes.export_bp, web.routes.groups, web.routes.import_db, web.routes.logs,
#                web.routes.metrics, web.routes.printers, web.routes.scan, web.routes.security,
#                web.routes.stats, web.routes.system, web.routes.users, web.routes.validation,
#                web.routes.yield_status, web.security | external: flask, flask_login, werkzeug
# @notes:        Reads environment variables: ASSET_VERSION, TRUST_PROXY.
# =============================================================================

"""
Creating Flask app and registering all blueprints
"""

import os

from flask import Flask, jsonify, redirect, request, url_for
from flask_login import current_user
from werkzeug.middleware.proxy_fix import ProxyFix

from web.routes.dashboard  import bp as bp_dashboard
from web.routes.printers   import bp as bp_printers
from web.routes.logs       import bp as bp_logs
from web.routes.export_bp  import bp as bp_export
from web.routes.scan       import bp as bp_scan
from web.routes.discover   import bp as bp_discover
from web.routes.stats      import bp as bp_stats
from web.routes.validation import bp as bp_validation
from web.routes.system     import bp as bp_system
from web.routes.users      import bp as bp_users
from web.routes.security   import bp as bp_security_audit
from web.routes.groups     import bp as bp_groups
from web.routes.import_db import bp as bp_import_db
from web.routes.yield_status import bp as bp_yield_status
from web.routes.metrics import bp as bp_metrics
from web.routes.contact import bp as bp_contact
from web.auth import auth_bp, init_auth, login_redirect_url, user_can_access_module
from web.captcha import init_captcha
from web.security import init_security
from config import settings

# The absolute path of the web/ folder (the same file is located in web/)
_WEB_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app() -> Flask:
    """Create and configure the Flask application."""

    app = Flask(
        __name__,
        template_folder=os.path.join(_WEB_DIR, "templates"),
        static_folder=os.path.join(_WEB_DIR, "static"),
    )

    # --- Reverse proxy (nginx) -----------------------------------
    # Only when TRUST_PROXY=1, X-Forwarded-* headers from nginx directly
    # The front of the program is considered valid — the most important is X-Forwarded-Proto
    # request.is_secure behind TLS-termination is true (CSRF SSL-strict, redirects).
    # Without this flag the headers are ignored so they cannot be forged in development/direct access.
    # x_for/x_proto/x_host/x_port = 1 means only one proxy layer is trusted.
    if os.getenv("TRUST_PROXY", "") in ("1", "true", "yes"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    app.config.update(
        SECRET_KEY=settings.SECRET_KEY,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_USE_TLS=settings.MAIL_USE_TLS,
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=settings.MAIL_PASSWORD,
        MAIL_USE_OAUTH=settings.MAIL_USE_OAUTH,
        MAIL_OAUTH_CLIENT_ID=settings.MAIL_OAUTH_CLIENT_ID,
        MAIL_OAUTH_CLIENT_SECRET=settings.MAIL_OAUTH_CLIENT_SECRET,
        MAIL_OAUTH_REFRESH_TOKEN=settings.MAIL_OAUTH_REFRESH_TOKEN,
        GOOGLE_CLIENT_ID=settings.GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET=settings.GOOGLE_CLIENT_SECRET,
        ASSET_VERSION=os.getenv("ASSET_VERSION", "20260907-resilience-proxy"),
    )

    init_auth(app)
    init_captcha(app)
    init_security(app)

    for blueprint in (
        auth_bp,
        bp_dashboard, bp_printers, bp_logs, bp_export,
        bp_scan, bp_discover, bp_stats, bp_validation, bp_system, bp_users,
        bp_security_audit, bp_import_db, bp_yield_status, bp_groups, bp_metrics, bp_contact,
    ):
        app.register_blueprint(blueprint)

    # ─── Exemption of JSON API endpoints from CSRF ─────────────────────────
    # For JSON endpoints that authenticate from session, CSRF protection
    # It is controlled, but the browser/client must send the X-CSRFToken header.
    # For now, the GET endpoints (which have no side-effects) and the whole bp_export
    # which only have GET, do not need CSRF.
    # Flask-WTF automatically exempts GET/HEAD/OPTIONS.

    @app.before_request
    def protect_routes():
        """Before-request hook: require login for every endpoint that is not in the public list."""
        endpoint = request.endpoint or ""
        public_endpoints = {
            "static",
            "auth.login",
            "auth.register",
            "auth.forgot_password",
            "auth.reset_password",
            "auth.google_login",
            "auth.google_callback",
            "auth.altcha_challenge",
            # NOTE: "metrics.prometheus_metrics" intentionally omitted from public list —
            # Its authentication is done with the Bearer token (METRICS_TOKEN) in the view itself.
            # NOTE: "system.api_status" intentionally omitted from public list —
            # Without login, polling statistics and service status should not be seen.
        }
        if endpoint.startswith("static"):
            return None
        # ─── /metrics: The authentication decision is made in the view itself ────────────
        # This endpoint is no longer in public_endpoints, but its path is under /api/
        # is not Without this exception, the general guard will redirect it to the login page
        # and the Bearer route was never executed. Viv itself gives 401
        # (missing/wrong token) or allows METRICS_TOKEN if missing.
        if endpoint == "metrics.prometheus_metrics":
            return None
        if not current_user.is_authenticated:
            if endpoint in public_endpoints:
                return None
            # E.6.6 (TD-002): `next` is just the path to this site — not the full request.url.
            # So that scheme+host is not reflected in the body of 401/Location header and is guaranteed
            # Let the value be what `_next_url` accepts after entering.
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "unauthorized", "login_url": login_redirect_url()}), 401
            return redirect(login_redirect_url())

        if not getattr(current_user, "is_verified", False):
            if request.path in ("/", "/auth/logout") or endpoint == "auth.logout":
                return None
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "pending_verification"}), 403
            return redirect(url_for("dashboard.index"))

        endpoint_modules = {
            "printers.api_printers": "printers",
            "printers.api_printer": "printers",
            "printers.debug_printer": "printers",
            "printers.api_add": "printers",
            "printers.api_bulk_add": "printers",
            "printers.api_remove": "printers",
            "printers.api_auto_add_printer": "printers",
            "printers.rename_printer": "printers",
            "logs.api_printer_log": "logs",
            "logs.api_all_logs": "logs",
            "logs.api_clear_logs": "logs",
            "logs.api_manual_event": "logs",
            "export.export_excel": "excel",
            "export.export_logs": "excel",
            "users.users_page": "users",
            "users.api_office_options": "users",
            "users.api_users": "users",
            "users.api_user_role": "users",
            "users.api_user_verify": "users",
            "users.api_delete_user": "users",
            "users.api_user_add": "users",
            "users.api_user_access": "users",
            "users.api_sensor_alert_get": "users",
            "users.api_sensor_alert_set": "users",
            "users.api_system_settings_get": "users",
            "users.api_system_settings_set": "users",
            "users.api_system_settings_test_email": "users",
        }
        module_name = endpoint_modules.get(endpoint)
        if module_name and not user_can_access_module(current_user, module_name):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("dashboard.index"))

        role = getattr(current_user, "role", "viewer")
        if role == "admin":
            return None

        viewer_allowed = {
            "dashboard.index",
            "system.api_status",
            "groups.api_groups",
            "printers.api_printers",
            "printers.api_printer",
            "logs.api_printer_log",
            "logs.api_all_logs",
            "stats.api_daily_stats",
            "auth.logout",
        }
        manager_allowed = viewer_allowed | {
            "export.export_excel",
            "users.users_page",
            "users.api_office_options",
            "users.api_users",
            "users.api_user_add",
            "users.api_user_role",
            "users.api_user_verify",
            "users.api_user_access",
            "users.api_delete_user",
        }

        if role == "manager":
            if endpoint == "export.export_logs":
                if request.args.get("format", "csv").lower() == "excel":
                    return None
            elif endpoint in manager_allowed:
                return None

            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("dashboard.index"))

        if endpoint in viewer_allowed:
            return None
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "forbidden"}), 403
        return redirect(url_for("dashboard.index"))

    # ─── CORS (only if explicitly configured via env) ────────────
    # By default no CORS headers are set (same-origin only).
    # Configure via CORS_ALLOWED_ORIGINS env variable.
    if settings.CORS_ALLOWED_ORIGINS:
        @app.after_request
        def cors(r):
            """After-request hook: add CORS headers when the request origin is allowed."""
            origin = request.headers.get("Origin", "")
            allowed = settings.CORS_ALLOWED_ORIGINS
            # The requesting domain must be whitelisted (or '*' for dev)
            if "*" in allowed:
                r.headers['Access-Control-Allow-Origin'] = '*'
            elif origin in allowed:
                r.headers['Access-Control-Allow-Origin'] = origin
                # Vary: Origin is important so that caches keep different origins separate
                r.headers['Vary'] = 'Origin'
            else:
                # origin is not allowed → don't put CORS header
                return r
            r.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-CSRFToken'
            r.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            r.headers['Access-Control-Allow-Credentials'] = 'true'
            r.headers['Access-Control-Max-Age'] = '3600'
            return r

    @app.context_processor
    def inject_settings():
        # Build a list of office group defs for templates/JS, using subnets from settings
        """Template context processor: expose the office group definitions to templates and JavaScript."""
        name_map = {
            'imamat': 'دفتر امامت',
            'soroush': 'دفتر سروش',
            'falestin': 'دفتر فلسطین',
            'elahiye': 'دفتر الهیه',
            'other': 'سایر',
        }
        icon_map = {
            'other': '🖨'
        }
        color_map = {
            'imamat': 'cyan', 'soroush': 'green', 'falestin': 'yellow', 'elahiye': 'magenta', 'other': 'orange'
        }
        office_groups = []
        for oid, subnet in settings.OFFICE_SUBNETS.items():
            office_groups.append({
                'id': oid,
                'name': name_map.get(oid, oid),
                'subnet': subnet,
                'icon': icon_map.get(oid, '🏢'),
                'color': color_map.get(oid, 'cyan'),
            })

        return dict(poll_interval=settings.POLL_INTERVAL, OFFICE_GROUPS=office_groups)

    return app