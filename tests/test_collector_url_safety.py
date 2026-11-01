# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_collector_url_safety.py
# @brief:        Tests for printer web-page URL scheme checks and the TLS switch.
# @details:      Responsibilities: verify that only http/https URLs are fetched, that
#                certificate verification is off by default and can be enabled with
#                PRINTER_TLS_VERIFY, and that file:// URLs never reach urlopen.
#                Main API: test_* functions only
#                Side effects: none (network calls are patched)
# @dependencies: internal: core.collectors.url_safety, core.collectors.base | external: pytest
# @notes:        None.
# =============================================================================

from unittest.mock import MagicMock, patch

import pytest

from core.collectors import url_safety
from core.collectors.base import fetch_first_web_page


@pytest.mark.parametrize("url", [
    "http://192.0.2.10/",
    "https://192.0.2.10:443/status.html",
    "HTTP://192.0.2.10/",
])
def test_http_and_https_are_allowed(url):
    assert url_safety.is_http_url(url) is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://192.0.2.10/",
    "gopher://192.0.2.10/",
    "javascript:alert(1)",
    "192.0.2.10/status",
    "",
    None,
])
def test_other_schemes_are_rejected(url):
    assert url_safety.is_http_url(url) is False


def test_tls_verify_is_off_by_default(monkeypatch):
    monkeypatch.delenv("PRINTER_TLS_VERIFY", raising=False)
    assert url_safety.tls_verify_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_tls_verify_can_be_enabled(monkeypatch, value):
    monkeypatch.setenv("PRINTER_TLS_VERIFY", value)
    assert url_safety.tls_verify_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_tls_verify_stays_off_for_other_values(monkeypatch, value):
    monkeypatch.setenv("PRINTER_TLS_VERIFY", value)
    assert url_safety.tls_verify_enabled() is False


def test_file_url_never_reaches_urlopen(monkeypatch):
    """A file:// URL in the scrape list must be skipped before any urllib call."""
    monkeypatch.delenv("PRINTER_TLS_VERIFY", raising=False)
    with patch("urllib.request.urlopen") as opened:
        result = fetch_first_web_page("192.0.2.10", ["file:///etc/passwd"], timeout=0.5)
    opened.assert_not_called()
    assert result == (None, None)


def test_requests_verify_follows_switch(monkeypatch):
    """requests.get gets verify=False by default and verify=True when the switch is on."""
    requests = pytest.importorskip("requests")

    def run(env_value):
        if env_value is None:
            monkeypatch.delenv("PRINTER_TLS_VERIFY", raising=False)
        else:
            monkeypatch.setenv("PRINTER_TLS_VERIFY", env_value)
        fake = MagicMock()
        fake.status_code = 500  # keep the loop going; we only inspect the call
        fake.text = ""
        with patch.object(requests, "get", return_value=fake) as get:
            fetch_first_web_page("192.0.2.10", ["https://192.0.2.10/"], timeout=0.5)
        return get.call_args.kwargs["verify"]

    assert run(None) is False
    assert run("1") is True
