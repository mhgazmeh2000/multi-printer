# =============================================================================
# @file:         web/security.py
# @brief:        Central security configuration for the Flask app: rate limiting, CSRF and headers.
# @details:      Responsibilities: Initialise Flask-Limiter, Flask-WTF CSRF and security headers.
#                Main API: init_security()
#                Side effects: none at import time
# @dependencies: internal: config | external: flask, flask_limiter, flask_wtf
# @notes:        Rate limits and CSRF are configured here. Check the Flask-Limiter storage backend
#                when running several workers.
# =============================================================================

"""
Centralized module for Flask security configurations:
- Rate Limiting (Flask-Limiter)
- CSRF Protection (Flask-WTF)
- Security Headers (CSP, X-Frame-Options, ...)
"""

import logging

from flask import jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError

from config import settings
from web.captcha import PROVIDER_RECAPTCHA, active_provider

log = logging.getLogger("PrinterMonitor")

# ─── Rate Limiter ────────────────────────────────────────────────
# Default: 200 requests per hour for all endpoints (overall)
# For sensitive endpoints such as login/register/forgot-password, stricter restrictions are applied
# (with @limiter.limit("...") decorator).
limiter = Limiter(
    key_func=get_remote_address,  # User identification based on IP
    default_limits=["1000 per hour"],
    storage_uri="memory://",  # For production, redis://localhost:6379 is recommended
    strategy="fixed-window",
    headers_enabled=True,  # Add X-RateLimit-* headers to the response
)


# ─── CSRF Protection ─────────────────────────────────────────────
csrf = CSRFProtect()


# ─── Security Headers ────────────────────────────────────────────
def _security_headers(response):
    """Add security headers to all responses."""
    # Prevent clickjacking
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Prevent MIME-type sniffing
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Referrer policy: send only origin (not full URL)
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Permissions policy: disabling unnecessary features
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=()",
    )
    # HSTS only in production and only on HTTPS
    if settings.ENVIRONMENT == "production" and request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    # Content-Security-Policy:
    # Note: Chart.js is loaded from a CDN, so its host is whitelisted. CAPTCHA is self-hosted (ALTCHA).
    # 'unsafe-inline' is required for styles at first (to remove inline styles)
    if "Content-Security-Policy" not in response.headers:
        csp_parts = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "worker-src 'self' blob:",  # ALTCHA runs its proof-of-work in a same-origin blob worker
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
        if active_provider() == PROVIDER_RECAPTCHA:
            # Google's reCAPTCHA script, its checkbox iframe and its API are allowed only while
            # reCAPTCHA is the active provider. ALTCHA keeps the strict policy above.
            csp_parts = [
                part.replace("script-src 'self' 'unsafe-inline'",
                             "script-src 'self' 'unsafe-inline' https://www.google.com https://www.gstatic.com")
                    .replace("connect-src 'self'", "connect-src 'self' https://www.google.com")
                for part in csp_parts
            ] + ["frame-src https://www.google.com"]
        response.headers["Content-Security-Policy"] = "; ".join(csp_parts)
    return response


def init_security(app):
    """Setting up all security tools on Flask app."""
    # Rate limiter
    limiter.init_app(app)

    # CSRF protection
    # Enable CSRF protection only on HTML forms (POST/PUT/DELETE/PATCH)
    # For JSON API endpoints (which work with X-CSRFToken header) it is necessary to exempt or use token separately
    csrf.init_app(app)

    # Setting the CSRF token validity period (default 12 hours)
    # The dashboard is a long-lived page (it stays open for hours); with a value of 3600 seconds
    # All POST buttons were dying with 400 after an hour. New token from
    # /api/status is silently injected and apiFetch retries on expiration.
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", 43200)
    app.config.setdefault("WTF_CSRF_SSL_STRICT", settings.ENVIRONMENT == "production")

    # CSRF error handling (JSON response for API, redirect for HTML)
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """Return a JSON error response for a CSRF failure."""
        log.warning("CSRF error from %s: %s", request.remote_addr, e.description)
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "csrf_token_invalid", "message": e.description}), 400
        # For HTML forms: clear message
        return jsonify({
            "error": "csrf_token_invalid",
            "message": "Session منقضی شده است. لطفاً صفحه را تازه کنید و دوباره تلاش کنید.",
        }), 400

    # Security Headers
    app.after_request(_security_headers)

    # Rate limit error management
    @app.errorhandler(429)
    def ratelimit_handler(e):
        """Return a 429 response when the rate limit is exceeded."""
        retry_after = getattr(e, "retry_after", None)
        log.warning(
            "Rate limit exceeded for %s on %s (retry_after=%s)",
            request.remote_addr, request.path, retry_after,
        )
        return jsonify({
            "error": "rate_limit_exceeded",
            "message": "تعداد درخواست‌های شما بیش از حد مجاز است. لطفاً کمی صبر کنید.",
            "retry_after": retry_after,
        }), 429

    log.info("✅ Security initialized: CSRF, Rate Limiting, Security Headers")
