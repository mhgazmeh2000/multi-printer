# =============================================================================
# @file:         web/captcha.py
# @brief:        CAPTCHA provider selection: self-hosted ALTCHA (default) or Google reCAPTCHA.
# @details:      Responsibilities: pick the active provider (altcha | recaptcha | none), create
#                ALTCHA proof-of-work challenges, verify ALTCHA and reCAPTCHA submissions, reject
#                replayed ALTCHA solutions, and expose the provider to the templates.
#                Main API: active_provider(), captcha_enabled(), create_challenge(),
#                verify_captcha(form), init_captcha(app)
#                Side effects: keeps an in-memory set of accepted ALTCHA signatures; calls Google
#                siteverify only when reCAPTCHA is the active provider.
# @dependencies: internal: config.settings | external: altcha, requests
# @notes:        Fail-Open: provider "none" (or CAPTCHA_ENABLED=false) skips every check. For
#                reCAPTCHA a network failure while calling Google allows the submission, so an
#                outage does not lock users out. A missing secret always fails closed.
#                The ALTCHA replay set is per process; several worker processes would need a
#                shared store to block a replay across them.
# =============================================================================
"""CAPTCHA helpers for the Flask application.

The active provider comes from ``CAPTCHA_PROVIDER`` (admin panel or environment).
``CAPTCHA_ENABLED=false`` turns CAPTCHA off for every provider. ``ALTCHA_ENABLED=false``
turns off only the ALTCHA provider.
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import threading
import time
from typing import Mapping

import altcha
import requests
from flask import has_request_context, request

from config import settings

log = logging.getLogger(__name__)

#: Provider identifiers accepted in ``CAPTCHA_PROVIDER``.
PROVIDER_ALTCHA = "altcha"
PROVIDER_RECAPTCHA = "recaptcha"
PROVIDER_NONE = "none"
PROVIDERS = (PROVIDER_ALTCHA, PROVIDER_RECAPTCHA, PROVIDER_NONE)

#: Form field that carries the ALTCHA payload (must match the widget's ``name``).
FIELD_NAME = "altcha"
#: Form field that Google's reCAPTCHA widget fills in.
RECAPTCHA_FIELD = "g-recaptcha-response"
RECAPTCHA_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"
RECAPTCHA_TIMEOUT_SECONDS = 10

_replay_lock = threading.Lock()
# Set after the one-time error log about a missing ALTCHA_HMAC_SECRET (avoids a log line per request).
_missing_secret_logged = False
# Accepted challenge signature -> time after which the entry may be forgotten.
_accepted_signatures: dict[str, float] = {}


@functools.lru_cache(maxsize=16)
def _parse_networks(raw: str) -> tuple:
    """Parse a comma-separated list of IP networks from a setting.

    Args:
        raw: The raw setting value, for example ``172.16.25.0/24,10.0.0.1``.

    Returns:
        tuple: Valid ``ipaddress`` network objects. Invalid entries are logged and skipped.
    """
    networks = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.error("Ignoring invalid network in a CAPTCHA setting: %r", part)
    return tuple(networks)


def request_bypasses_captcha() -> bool:
    """Return True when the current request is a direct connection from a bypass network.

    A request is NOT bypassed when any of these holds:
        - it carries ``X-Forwarded-For`` or ``X-Real-IP`` (it came through a reverse proxy);
        - its connection peer is listed in ``CAPTCHA_TRUSTED_PROXIES`` (nginx itself);
        - its connection peer is outside ``CAPTCHA_BYPASS_NETWORKS``.

    Public traffic therefore always follows the admin panel, and the rule does not depend on
    how the reverse proxy is configured.

    Returns:
        bool: True if CAPTCHA must be skipped for this request.
    """
    if not has_request_context():
        return False
    bypass = _parse_networks(settings.CAPTCHA_BYPASS_NETWORKS)
    if not bypass:
        return False
    if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
        return False
    try:
        peer = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    if any(peer in net for net in _parse_networks(settings.CAPTCHA_TRUSTED_PROXIES)):
        return False
    return any(peer in net for net in bypass)


def active_provider() -> str:
    """Return the CAPTCHA provider in effect right now.

    Order of precedence:
        1. ``CAPTCHA_ENABLED=false`` (environment only) -> ``none`` for every provider.
        1b. A direct request from a ``CAPTCHA_BYPASS_NETWORKS`` network -> ``none``
            (see ``request_bypasses_captcha``).
        2. ``CAPTCHA_PROVIDER`` from the admin panel or the environment. Unknown values
           fall back to ``altcha`` (logged as a warning).
        3. ``ALTCHA_ENABLED=false`` (environment only) turns the ALTCHA choice into ``none``.
        4. ALTCHA without ``ALTCHA_HMAC_SECRET`` turns into ``none`` (Fail-Open): users are
           not locked out by a server misconfiguration. The error is logged and the admin
           panel shows a warning.

    Returns:
        str: One of ``altcha``, ``recaptcha`` or ``none``.
    """
    if not settings.CAPTCHA_ENABLED:
        return PROVIDER_NONE
    if request_bypasses_captcha():
        return PROVIDER_NONE
    provider = str(settings.CAPTCHA_PROVIDER or "").strip().lower()
    if provider not in PROVIDERS:
        log.warning("Unknown CAPTCHA_PROVIDER %r; falling back to %s", settings.CAPTCHA_PROVIDER, PROVIDER_ALTCHA)
        provider = PROVIDER_ALTCHA
    if provider == PROVIDER_ALTCHA and not settings.ALTCHA_ENABLED:
        return PROVIDER_NONE
    if provider == PROVIDER_ALTCHA and altcha_secret_missing():
        return PROVIDER_NONE
    return provider


def altcha_secret_missing() -> bool:
    """Return True when ALTCHA_HMAC_SECRET is empty, and log the problem once.

    Returns:
        bool: True if no signing secret is configured.
    """
    global _missing_secret_logged
    if settings.ALTCHA_HMAC_SECRET:
        return False
    if not _missing_secret_logged:
        log.error("ALTCHA_HMAC_SECRET is not set: CAPTCHA is OFF for all forms (Fail-Open) "
                  "until it is set in .env and the server is restarted")
        _missing_secret_logged = True
    return True


def captcha_enabled() -> bool:
    """Return True when a CAPTCHA is shown and checked on the protected forms.

    Returns:
        bool: True unless the active provider is ``none``.
    """
    return active_provider() != PROVIDER_NONE


def captcha_context() -> dict:
    """Template variables used by the CAPTCHA include.

    Returns:
        dict: ``captcha_provider`` and ``captcha_site_key`` (the site key only for reCAPTCHA).
    """
    provider = active_provider()
    return {
        "captcha_provider": provider,
        "captcha_site_key": settings.RECAPTCHA_SITE_KEY if provider == PROVIDER_RECAPTCHA else "",
    }


def init_captcha(app) -> None:
    """Register the CAPTCHA template variables on a Flask app.

    Args:
        app: The Flask application.
    """
    app.context_processor(captcha_context)


def create_challenge() -> dict:
    """Create a fresh ALTCHA proof-of-work challenge.

    Returns:
        dict: The challenge in the JSON shape the widget expects
        (``parameters`` plus ``signature``).

    Raises:
        RuntimeError: If ``ALTCHA_HMAC_SECRET`` is not configured.
    """
    secret = settings.ALTCHA_HMAC_SECRET
    if not secret:
        raise RuntimeError("ALTCHA_HMAC_SECRET is not set")
    challenge = altcha.create_challenge(
        algorithm="PBKDF2/SHA-256",
        cost=settings.ALTCHA_COST,
        expires_at=int(time.time()) + settings.ALTCHA_CHALLENGE_TTL_SECONDS,
        hmac_secret=secret,
    )
    return challenge.to_dict()


def verify_captcha(form: Mapping, remote_ip: str | None = None) -> bool:
    """Check the CAPTCHA field of a submitted form with the active provider.

    Args:
        form: The submitted form data (for example ``request.form``).
        remote_ip: Client address, forwarded to Google for reCAPTCHA.

    Returns:
        bool: True if the submission may proceed.
    """
    provider = active_provider()
    if provider == PROVIDER_NONE:
        return True
    if provider == PROVIDER_RECAPTCHA:
        return _verify_recaptcha(form.get(RECAPTCHA_FIELD, ""), remote_ip)
    return _verify_altcha(form.get(FIELD_NAME, ""))


def _verify_altcha(payload: str | None) -> bool:
    """Check a submitted ALTCHA payload.

    Args:
        payload: Base64 payload from the form field, or None.

    Returns:
        bool: True for a valid, unused, unexpired solution, or True (Fail-Open) when no
        signing secret is configured.
    """
    if altcha_secret_missing():
        # Fail-Open: active_provider() normally prevents this path; kept as a safety net.
        return True
    if not payload:
        return False
    secret = settings.ALTCHA_HMAC_SECRET
    try:
        result = altcha.verify_solution(payload, secret)
        if not result.verified:
            log.info("ALTCHA solution rejected (expired=%s, signature=%s, solution=%s)",
                     result.expired, result.invalid_signature, result.invalid_solution)
            return False
        signature = altcha.Payload.from_base64(payload).challenge.signature
    except Exception:  # malformed base64/JSON or an unexpected library error
        log.warning("ALTCHA payload could not be verified", exc_info=True)
        return False
    return _accept_once(signature)


def _verify_recaptcha(token: str | None, remote_ip: str | None) -> bool:
    """Check a Google reCAPTCHA token with the siteverify API.

    Fail-Open: if Google cannot be reached (outage or blocked network), the submission is
    allowed and a warning is logged. A missing secret key fails closed.

    Args:
        token: The ``g-recaptcha-response`` value from the form.
        remote_ip: Client address (optional, sent to Google).

    Returns:
        bool: True if Google accepted the token, or if Google could not be reached.
    """
    secret = settings.RECAPTCHA_SECRET_KEY
    if not secret:
        log.error("reCAPTCHA is the active provider but RECAPTCHA_SECRET_KEY is not set; rejecting")
        return False
    if not token:
        return False
    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        response = requests.post(RECAPTCHA_VERIFY_URL, data=payload, timeout=RECAPTCHA_TIMEOUT_SECONDS)
        data = response.json()
    except (requests.RequestException, ValueError):
        log.warning("reCAPTCHA siteverify unreachable; allowing the submission (Fail-Open)", exc_info=True)
        return True
    success = bool(data.get("success"))
    if not success:
        log.info("reCAPTCHA rejected the token: %s", data.get("error-codes"))
    return success


def _accept_once(signature: str | None) -> bool:
    """Record a challenge signature as used, and refuse it if it was used before.

    Args:
        signature: The challenge signature from the verified payload.

    Returns:
        bool: True the first time, False for any repeat within the TTL.
    """
    if not signature:
        return False
    now = time.time()
    with _replay_lock:
        # Forget entries whose challenge can no longer verify anyway.
        for key, forget_after in list(_accepted_signatures.items()):
            if forget_after <= now:
                del _accepted_signatures[key]
        if signature in _accepted_signatures:
            log.warning("ALTCHA replay attempt rejected")
            return False
        _accepted_signatures[signature] = now + settings.ALTCHA_CHALLENGE_TTL_SECONDS
    return True
