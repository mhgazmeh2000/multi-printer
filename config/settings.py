# =============================================================================
# @file:         config/settings.py
# @brief:        Central application settings read from environment variables and config files.
# @details:      Responsibilities: Define the global configuration constants (DB backend, secrets,
#                paths, polling).
#                Main API: none (no public functions or classes)
#                Side effects: file I/O
# @dependencies: external: dotenv
# @notes:        Reads .env values. The default SECRET_KEY triggers a runtime warning and must be
#                overridden in production. Reads environment variables: CORS_ALLOWED_ORIGINS,
#                DEFAULT_PRINTERS_JSON, ENVIRONMENT, OFFICE_SUBNET_ELAHIYE,
#                OFFICE_SUBNET_FALESTIN, OFFICE_SUBNET_IMAMAT, OFFICE_SUBNET_SOROUSH, SECRET_KEY.
# =============================================================================

# config/settings.py

"""
Application global settings
"""

import json
import os

from dotenv import load_dotenv
load_dotenv()

# Files and Paths
PRINTERS_FILE        = "printers.json"
DB_PATH              = "logs.db"
OID_PROFILES_FILE    = "oid_profiles.json"
VALIDATION_LOG_FILE  = "oid_validation_errors.txt"

# Default Printers
# To avoid committing real IPs, the default list is read only from ENV.
# Example:
#   export DEFAULT_PRINTERS_JSON='[{"ip":"192.168.1.10","name":"Printer #1","community":"public"}]'
try:
    DEFAULT_PRINTERS = json.loads(os.getenv("DEFAULT_PRINTERS_JSON", "[]") or "[]")
    if not isinstance(DEFAULT_PRINTERS, list):
        DEFAULT_PRINTERS = []
except Exception:
    DEFAULT_PRINTERS = []

# ─── SNMP ───────────────────────────────────────────────────────
SNMP_PORT     = 161

# ─── Polling ────────────────────────────────────────────────────
# 🔥 Change: from 30 seconds to 60 seconds (1 minute)
POLL_INTERVAL = 60   # seconds (1 minute)

# ─── Flask ──────────────────────────────────────────────────────
FLASK_PORT = 5050

# ─── Environment & SECRET_KEY ───────────────────────────────────
# ENVIRONMENT can be "development" or "production".
# In production, SECRET_KEY must be set from env, otherwise the program will not start.
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
_INSECURE_DEFAULT_KEY = "change-this-secret-key-in-production"
SECRET_KEY = os.getenv("SECRET_KEY", "")

# ─── Load system_config.json overrides ──────────────────────
# This file is written by the admin panel and takes precedence over env vars.
_SYSTEM_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'system_config.json')
try:
    if os.path.exists(_SYSTEM_CONFIG_FILE):
        with open(_SYSTEM_CONFIG_FILE, 'r', encoding='utf-8') as _f:
            _sc = json.load(_f) or {}
        # Helper to get value: system_config > env var > default
        def _cfg(key, default=''):
            """Read a setting from the system config first, then from the environment."""
            if key in _sc and _sc[key] not in (None, ''):
                return _sc[key]
            return os.getenv(key, default)
    else:
        _sc = {}
        def _cfg(key, default=''):
            """Read a setting from the environment, used when the system config is not loaded."""
            return os.getenv(key, default)
except Exception:
    _sc = {}
    def _cfg(key, default=''):
        """Read a setting from the environment, used when the system config cannot be parsed."""
        return os.getenv(key, default)


if not SECRET_KEY:
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "❌ SECRET_KEY environment variable is REQUIRED in production!\n"
            "   Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "   Then: export SECRET_KEY=<the-generated-key>"
        )
    # Development mode: temporary key with warning
    import warnings
    SECRET_KEY = _INSECURE_DEFAULT_KEY
    warnings.warn(
        "⚠  SECRET_KEY is using the INSECURE default value. "
        "Set the SECRET_KEY environment variable for security. "
        "This is acceptable for development only.",
        RuntimeWarning,
        stacklevel=2,
    )
elif SECRET_KEY == _INSECURE_DEFAULT_KEY and ENVIRONMENT == "production":
    raise RuntimeError(
        "❌ SECRET_KEY is set to the default INSECURE value in production!\n"
        "   Generate a new one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

MAIL_SERVER = _cfg("MAIL_SERVER", "")
MAIL_PORT = int(_cfg("MAIL_PORT", 587))
MAIL_USE_TLS = str(_cfg("MAIL_USE_TLS", "1")) in ("1", "true", "True", True)
MAIL_USERNAME = _cfg("MAIL_USERNAME", "")
MAIL_PASSWORD = _cfg("MAIL_PASSWORD", "")
# ─── SMTP OAuth 2.0 (Gmail XOAUTH2) ──────────────────────────
# Activation: MAIL_USE_OAUTH=1 to use OAuth 2.0 instead of App Password
MAIL_USE_OAUTH = str(_cfg("MAIL_USE_OAUTH", "0")) in ("1", "true", "True", True)
MAIL_OAUTH_CLIENT_ID = _cfg("MAIL_OAUTH_CLIENT_ID", "")
MAIL_OAUTH_CLIENT_SECRET = _cfg("MAIL_OAUTH_CLIENT_SECRET", "")
MAIL_OAUTH_REFRESH_TOKEN = _cfg("MAIL_OAUTH_REFRESH_TOKEN", "")
GOOGLE_CLIENT_ID = _cfg("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = _cfg("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = _cfg("GOOGLE_REDIRECT_URI", "")
RECAPTCHA_SITE_KEY = _cfg("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET_KEY = _cfg("RECAPTCHA_SECRET_KEY", "")

# ─── CORS ───────────────────────────────────────────────────────
# List of origins that are allowed to access the API (separate with commas).
# Empty value = no CORS header is set (same-origin only).
# Value "*" = all origins (only for development - dangerous in production!)
# Example: CORS_ALLOWED_ORIGINS="https://app.example.com,https://admin.example.com"
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
if "*" in CORS_ALLOWED_ORIGINS and ENVIRONMENT == "production":
    raise RuntimeError(
        "❌ CORS_ALLOWED_ORIGINS='*' is NOT allowed in production! "
        "Specify exact origins like: https://app.example.com"
    )

# Authorized offices and subnets
# If ENV is not set, fallback to legacy subnets is performed
# Restricting users' access and filtering offices will not work.
_LEGACY_OFFICE_SUBNETS = {
    "imamat": "172.16.25",
    "soroush": "172.16.24",
    "falestin": "172.16.0",
    "elahiye": "172.16.32",
}
OFFICE_SUBNETS = {
    "imamat": os.getenv("OFFICE_SUBNET_IMAMAT") or _LEGACY_OFFICE_SUBNETS["imamat"],
    "soroush": os.getenv("OFFICE_SUBNET_SOROUSH") or _LEGACY_OFFICE_SUBNETS["soroush"],
    "falestin": os.getenv("OFFICE_SUBNET_FALESTIN") or _LEGACY_OFFICE_SUBNETS["falestin"],
    "elahiye": os.getenv("OFFICE_SUBNET_ELAHIYE") or _LEGACY_OFFICE_SUBNETS["elahiye"],
    "other": None,
}

# ─── Support local settings file office_subnets.json ─────────────
# If the file exists, its values will replace the ENV/default values.
try:
    _settings_dir = os.path.dirname(os.path.abspath(__file__))
    _subnets_file = os.path.join(_settings_dir, 'office_subnets.json')
    if os.path.exists(_subnets_file):
        with open(_subnets_file, 'r', encoding='utf-8') as _f:
            _file_data = json.load(_f) or {}
            if isinstance(_file_data, dict):
                # If file exists, it is the source of truth (no merge with legacy defaults)
                OFFICE_SUBNETS = {str(k).strip(): v for k, v in _file_data.items() if str(k).strip()}
except Exception:
    # If the file is corrupt, we use the ENV/default values
    pass

# ─── Thresholds for toner alerts (percent)
TONER_ALERT_THRESHOLDS = {
    "critical": 5,   # Below 5% critical
    "warning": 15,   # Below 15% warning
    "info": 30,      # Under 30% notification (optional)
}