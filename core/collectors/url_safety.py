# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/collectors/url_safety.py
# @brief:        Shared TLS switch and URL scheme check for printer web-page scraping.
# @details:      Responsibilities: decide whether printer HTTPS certificates are verified
#                (PRINTER_TLS_VERIFY, default off because printers use self-signed certs),
#                and allow only http/https URLs so urllib can never open file://, ftp:// etc.
#                Main API: tls_verify_enabled(), is_http_url()
#                Side effects: none (reads an environment variable)
# @dependencies: external: none (standard library only)
# @notes:        Kept dependency-free so collectors can import it without a cycle.
# =============================================================================

import os
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")


def tls_verify_enabled() -> bool:
    """Return True when printer web pages must be verified with TLS certificates.

    Default is False (the historical behaviour). Set PRINTER_TLS_VERIFY=1 to enforce
    verification; use it only for printers that have certificates the host trusts.
    """
    return os.getenv("PRINTER_TLS_VERIFY", "0").strip().lower() in ("1", "true", "yes", "on")


def is_http_url(url) -> bool:
    """Return True only for absolute http:// or https:// URLs."""
    if not isinstance(url, str) or not url:
        return False
    try:
        return urlparse(url).scheme.lower() in ALLOWED_SCHEMES
    except ValueError:
        return False
