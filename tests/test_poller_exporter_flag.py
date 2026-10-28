# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_poller_exporter_flag.py
# @brief:        Tests for the USE_SNMP_EXPORTER flag in core/poller.py (step G.5.4).
# @details:      Responsibilities: Verify the flag selects the exporter path only when enabled and
#                available.
#                Main API: _Boom, _FakeResp, _QueueOpen, _FakeClient, 9 test functions
#                Side effects: network I/O
# @dependencies: internal: core.exporter_client, core.poller | external: pytest
# @notes:        None.
# =============================================================================

"""
Testing `USE_SNMP_EXPORTER` flag in `core/poller.py` (step G.5.4).

The phase acceptance gateway says: with poller flag off (default) **never** inbound
It does not become an exporter branch. Here it is measured by behavior, not by grep alone:

  1) Off flag ⇒ `client_from_env` is never called and `is_available` is never called
     not asked (factor that throws; test dies if called);
  2) flag on + exporter available ⇒ data comes from exporter and ``collect_enhanced''
     does not execute (spy throw);
  3) the flag was not on + exporter / the response was not enough ⇒ the same path as today, without errors;
  4) Full path (URL → Pars → adapter → poller) with fake transport and fixtures
     real phase G, for one HP; For Toshiba, no query reaches the adapter;
  5) The order of conditions in the code (`exporter_enabled() and...is_available()`) and its place in
     `_collect_inner` — only once, outside of any try/pas-si.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import core.poller as poller
from core.exporter_client import ExporterClient

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
POLLER_SRC = (ROOT / "core" / "poller.py").read_text(encoding="utf-8")

_PRINTER = {"ip": "172.16.0.5", "name": "HP-05", "brand": "hp", "community": "public",
            "device_type": "printer", "nickname": "پیش‌نمایش"}

_SHAPE = {
    "ip": "172.16.0.5", "name": "HP-05", "brand": "hp", "online": True,
    "last_poll": "2026-01-01T00:00:00Z", "poll_ms": 12, "device": {}, "counters": {"total": 1},
    "paper_sizes": {}, "trays": [], "toners": {}, "alerts": [],
}


class _Boom(Exception):
    """Helper class: boom."""
    pass


class _FakeResp:
    """Helper class: fake resp."""
    def __init__(self, body: bytes = b"", status: int = 200):
        """Initialize the instance."""
        self._body, self.status = body, status

    def read(self):
        """Helper: read."""
        return self._body

    def getcode(self):
        """Helper: getcode."""
        return self.status

    def __enter__(self):
        """Enter the context and return the object."""
        return self

    def __exit__(self, *exc):
        """Leave the context and release what was acquired."""
        return False


class _QueueOpen:
    """A series of answers; Each request gets a response (order-sensitive, as the poller calls it)."""

    def __init__(self, responses):
        """Initialize the instance."""
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, timeout=None):
        """Call the object like a function."""
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fixture_client():
    """Helper: fixture client."""
    hp = (FIXTURES / "exporter_response_sample.txt").read_text(encoding="utf-8").encode("utf-8")
    std = (FIXTURES / "exporter_response_std.txt").read_text(encoding="utf-8").encode("utf-8")
    return ExporterClient(open_fn=_QueueOpen([_FakeResp(b"<title>SNMP Exporter</title>"),
                                               _FakeResp(hp), _FakeResp(std)]))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Each test: flag off, client not cached, and fake SNMP successful."""
    monkeypatch.setattr(poller, "_exporter_client_cache", None, raising=False)
    monkeypatch.delenv("USE_SNMP_EXPORTER", raising=False)
    monkeypatch.setattr(poller, "snmp_get_first",
                        lambda ip, oids, community, timeout=0.0: ("ok", oids[0]))
    monkeypatch.setattr(poller, "_reset_health_failure", lambda ip: None)
    monkeypatch.setattr(poller, "_note_health_failure", lambda ip: 0)
    monkeypatch.setattr(poller, "add_event", lambda *a, **k: None)


def _no_exporter_factory(monkeypatch):
    """Helper: no exporter factory."""
    def boom(*args, **kwargs):
        """Helper: boom."""
        raise _Boom("با پرچمِ خاموش نباید هیچ شیئی از exporter ساخته شود")
    monkeypatch.setattr(poller, "client_from_env", boom)


def _enhanced_marker(monkeypatch, calls=None):
    """Fake `collect_enhanced` that *registers*, not throw.

    Why not throw: current path, call `collect_enhanced` inside `try/except Exception`
    and `pytest.fail' (Failed → Exception) was swallowed there and the test was
    Draws legacy collector. So "shouldn't run" is measured by the call log.
    """
    calls = [] if calls is None else calls

    def fake_enhanced(printer):
        """Helper: fake enhanced."""
        calls.append("enhanced")
        return {"ip": printer["ip"], "name": printer["name"], "brand": printer["brand"],
                "online": True, "from": "enhanced"}
    monkeypatch.setattr(poller, "collect_enhanced", fake_enhanced)
    return calls


# ── 1) Default: zero calls
def test_flag_off_never_builds_an_exporter_client(monkeypatch):
    """Verifies flag off never builds an exporter client."""
    _no_exporter_factory(monkeypatch)
    _enhanced_marker(monkeypatch)
    result = poller._collect_inner(dict(_PRINTER))
    assert result["from"] == "enhanced"
    assert poller._exporter_client_cache is None          # No lazy-init occurred


def test_flag_off_does_not_even_probe_availability(monkeypatch):
    """Verifies flag off does not even probe availability."""
    class NoProbe:
        """Helper class: no probe."""
        def is_available(self):
            """Helper: is available."""
            raise _Boom("is_available با پرچمِ خاموش نباید صدا زده شود")

    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: NoProbe())
    _enhanced_marker(monkeypatch)
    assert poller._collect_inner(dict(_PRINTER))["from"] == "enhanced"


# ──────────────────
def test_flag_on_and_available_uses_exporter_and_skips_enhanced(monkeypatch):
    """Verifies flag on and available uses exporter and skips enhanced."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    seen = []

    def fake_via_exporter(client, ip, name, brand, start=None, auth=None):
        """Helper: fake via exporter."""
        seen.append((ip, name, brand))
        return dict(_SHAPE)

    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: _FakeClient(True))
    monkeypatch.setattr(poller, "collect_via_exporter", fake_via_exporter)
    enhanced = _enhanced_marker(monkeypatch)
    result = poller._collect_inner(dict(_PRINTER))
    assert seen == [("172.16.0.5", "HP-05", "hp")]
    assert enhanced == []                               # Mustiq's route was not executed at all
    assert result["nickname"] == "پیش‌نمایش" and result["device_type"] == "printer"
    assert result["online"] is True


def test_flag_on_but_exporter_absent_falls_back(monkeypatch):
    """Verifies flag on but exporter absent falls back."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: _FakeClient(False))
    monkeypatch.setattr(poller, "collect_via_exporter",
                        lambda *a, **k: pytest.fail("exporter در دسترس نیست؛ نباید صدا زده شود"))
    enhanced = _enhanced_marker(monkeypatch)
    assert poller._collect_inner(dict(_PRINTER))["from"] == "enhanced"
    assert enhanced == ["enhanced"]                     # It means back to today's path


def test_flag_on_and_exporter_says_not_enough_data_falls_back(monkeypatch):
    """Verifies flag on and exporter says not enough data falls back."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: _FakeClient(True))
    monkeypatch.setattr(poller, "collect_via_exporter", lambda *a, **k: None)
    enhanced = _enhanced_marker(monkeypatch)
    assert poller._collect_inner(dict(_PRINTER))["from"] == "enhanced"
    assert enhanced == ["enhanced"]


class _FakeClient:
    """Helper class: fake client."""
    def __init__(self, available: bool):
        """Initialize the instance."""
        self.available = available

    def is_available(self):
        """Helper: is available."""
        return self.available


# ── 4) Complete path with fake transport
def test_full_path_from_url_to_shape(monkeypatch):
    """Verifies full path from url to shape."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    client = _fixture_client()
    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: client)
    enhanced = _enhanced_marker(monkeypatch)
    result = poller._collect_inner(dict(_PRINTER))
    assert enhanced == []
    assert result["counters"]["total"] == 12345
    assert result["counters"]["full_color"] == 1200
    assert result["device"]["model"] == "HP LaserJet Pro M402dn"
    assert result["toners"]["black"]["level"] == 42
    urls = client._open.urls
    assert urls[0].endswith("/")                                   # availability probe
    assert "module=printer_hp" in urls[1] and "module=printer_std" in urls[2]
    assert "auth=public_v2" in urls[1]


def test_unsupported_brand_never_reaches_the_exporter(monkeypatch):
    """Verifies unsupported brand never reaches the exporter."""
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")

    class NoQuery:
        """Helper class: no query."""
        def is_available(self):
            """Helper: is available."""
            return True

        def query_brand(self, *a, **k):
            """Helper: query brand."""
            raise _Boom("برای toshiba نباید از exporter پرسیده شود (SUPPORTED_BRANDS)")

    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: NoQuery())
    _enhanced_marker(monkeypatch)
    printer = dict(_PRINTER, brand="toshiba")
    result = poller._collect_inner(printer)
    assert result["from"] == "enhanced"


# ── 5) The form of the code of the branch
def test_branch_is_one_short_circuit_condition_inside_collect_inner():
    """Verifies branch is one short circuit condition inside collect inner."""
    tree = ast.parse(POLLER_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_collect_inner")
    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)
           and isinstance(n.test, ast.BoolOp)
           and "exporter_enabled" in ast.unparse(n.test)]
    assert len(ifs) == 1, ast.unparse(fn)[:200]
    condition = ifs[0].test
    left, right = condition.values
    assert "exporter_enabled()" in ast.unparse(left), ast.unparse(left)
    assert "is_available()" in ast.unparse(right), ast.unparse(right)
    assert condition.op.__class__.__name__ == "And"     # short-circuit ⇒ not socket with 0 flag
    body = ast.unparse(ifs[0].body)
    assert "collect_via_exporter" in body and "return exporter_result" in body
    assert "try" not in body and "pass" not in body     # We do not swallow mistakes
    assert "exporter" not in ast.unparse(ifs[0].orelse)  # The fallback path is collect_enhanced


def test_exporter_layer_does_not_touch_snmp_directly():
    """The exporter layer is only HTTP; It has no import to pysnmp/`snmp_get`.

    The reason for this test: if one day someone reads SNMP directly to the exporter path "for convenience".
    Slow mixing, parity measured in G.6 becomes meaningless.
    """
    for rel in ("core/exporter_client.py", "core/collectors/exporter_adapter.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for banned in ("pysnmp", "snmp_get", "octetstring", "easysnmp"):
            assert banned not in src, f"{rel}: {banned}"
