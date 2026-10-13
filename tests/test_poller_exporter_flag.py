# -*- coding: utf-8 -*-
"""
تستِ پرچمِ `USE_SNMP_EXPORTER` در `core/poller.py` (گام G.5.4).

دروازهٔ پذیرشِ فاز می‌گوید: با پرچمِ خاموش (پیش‌فرض) poller **هیچ‌وقت** واردِ
شاخهٔ exporter نمی‌شود. این‌جا همان به‌صورت رفتار سنجیده می‌شود، نه با grep تنها:

  ۱) پرچم خاموش ⇒ `client_from_env` هرگز صدا زده نمی‌شود و `is_available` هرگز
     پرسیده نمی‌شود (فاکتوری که throw می‌کند؛ اگر صدا زده شود تست می‌میرد)؛
  ۲) پرچم روشن + exporter در دسترس ⇒ داده از exporter می‌آید و `collect_enhanced`
     اجرا نمی‌شود (اسپای throw)؛
  ۳) پرچم روشن + exporter نبود / پاسخ کافی نبود ⇒ همان مسیرِ امروز، بدونِ خطا؛
  ۴) مسیرِ کامل (URL → پارس → adapter → poller) با transport جعلی و fixtureهای
     واقعیِ فاز G، برای یک HP؛ برای Toshiba هیچ کوئری‌ای تا adapter نمی‌رسد؛
  ۵) ترتیبِ شرط‌ها در کد (`exporter_enabled() and …is_available()`) و جای آن در
     `_collect_inner` — تنها یک‌بار، بیرونِ هیچ try/pas-sی.
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
    pass


class _FakeResp:
    def __init__(self, body: bytes = b"", status: int = 200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _QueueOpen:
    """صفّی از پاسخ‌ها؛ هر درخواست یک پاسخ می‌گیرد (order-sensitive، همان‌طور که poller می‌خواند)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fixture_client():
    hp = (FIXTURES / "exporter_response_sample.txt").read_text(encoding="utf-8").encode("utf-8")
    std = (FIXTURES / "exporter_response_std.txt").read_text(encoding="utf-8").encode("utf-8")
    return ExporterClient(open_fn=_QueueOpen([_FakeResp(b"<title>SNMP Exporter</title>"),
                                               _FakeResp(hp), _FakeResp(std)]))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """هر تست: پرچم خاموش، کلاینتِ cache نشده، و SNMPِ جعلیِ موفق."""
    monkeypatch.setattr(poller, "_exporter_client_cache", None, raising=False)
    monkeypatch.delenv("USE_SNMP_EXPORTER", raising=False)
    monkeypatch.setattr(poller, "snmp_get_first",
                        lambda ip, oids, community, timeout=0.0: ("ok", oids[0]))
    monkeypatch.setattr(poller, "_reset_health_failure", lambda ip: None)
    monkeypatch.setattr(poller, "_note_health_failure", lambda ip: 0)
    monkeypatch.setattr(poller, "add_event", lambda *a, **k: None)


def _no_exporter_factory(monkeypatch):
    def boom(*args, **kwargs):
        raise _Boom("با پرچمِ خاموش نباید هیچ شیئی از exporter ساخته شود")
    monkeypatch.setattr(poller, "client_from_env", boom)


def _enhanced_marker(monkeypatch, calls=None):
    """`collect_enhanced` جعلی که *ثبت* می‌کند، نه throw.

    چرا throw نه: مسیرِ فعلی، `collect_enhanced` را داخلِ `try/except Exception` صدا
    می‌زند و `pytest.fail` (Failed ← Exception) آن‌جا قورت داده می‌شد و تست را به
    legacy collector می‌کشاند. پس «نباید اجرا شود» با ثبتِ فراخوانی سنجیده می‌شود.
    """
    calls = [] if calls is None else calls

    def fake_enhanced(printer):
        calls.append("enhanced")
        return {"ip": printer["ip"], "name": printer["name"], "brand": printer["brand"],
                "online": True, "from": "enhanced"}
    monkeypatch.setattr(poller, "collect_enhanced", fake_enhanced)
    return calls


# ── ۱) پیش‌فرض: صفر تماس ──────────────────────────────────────────────────
def test_flag_off_never_builds_an_exporter_client(monkeypatch):
    _no_exporter_factory(monkeypatch)
    _enhanced_marker(monkeypatch)
    result = poller._collect_inner(dict(_PRINTER))
    assert result["from"] == "enhanced"
    assert poller._exporter_client_cache is None          # هیچ lazy-init ای رخ نداده


def test_flag_off_does_not_even_probe_availability(monkeypatch):
    class NoProbe:
        def is_available(self):
            raise _Boom("is_available با پرچمِ خاموش نباید صدا زده شود")

    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: NoProbe())
    _enhanced_marker(monkeypatch)
    assert poller._collect_inner(dict(_PRINTER))["from"] == "enhanced"


# ── ۲/۳) پرچم روشن ────────────────────────────────────────────────────────
def test_flag_on_and_available_uses_exporter_and_skips_enhanced(monkeypatch):
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    seen = []

    def fake_via_exporter(client, ip, name, brand, start=None, auth=None):
        seen.append((ip, name, brand))
        return dict(_SHAPE)

    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: _FakeClient(True))
    monkeypatch.setattr(poller, "collect_via_exporter", fake_via_exporter)
    enhanced = _enhanced_marker(monkeypatch)
    result = poller._collect_inner(dict(_PRINTER))
    assert seen == [("172.16.0.5", "HP-05", "hp")]
    assert enhanced == []                               # مسیرِ مستیق اصلاً اجرا نشد
    assert result["nickname"] == "پیش‌نمایش" and result["device_type"] == "printer"
    assert result["online"] is True


def test_flag_on_but_exporter_absent_falls_back(monkeypatch):
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: _FakeClient(False))
    monkeypatch.setattr(poller, "collect_via_exporter",
                        lambda *a, **k: pytest.fail("exporter در دسترس نیست؛ نباید صدا زده شود"))
    enhanced = _enhanced_marker(monkeypatch)
    assert poller._collect_inner(dict(_PRINTER))["from"] == "enhanced"
    assert enhanced == ["enhanced"]                     # یعنی به مسیرِ امروز برگشت


def test_flag_on_and_exporter_says_not_enough_data_falls_back(monkeypatch):
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")
    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: _FakeClient(True))
    monkeypatch.setattr(poller, "collect_via_exporter", lambda *a, **k: None)
    enhanced = _enhanced_marker(monkeypatch)
    assert poller._collect_inner(dict(_PRINTER))["from"] == "enhanced"
    assert enhanced == ["enhanced"]


class _FakeClient:
    def __init__(self, available: bool):
        self.available = available

    def is_available(self):
        return self.available


# ── ۴) مسیرِ کامل با transport جعلی ────────────────────────────────────────
def test_full_path_from_url_to_shape(monkeypatch):
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
    assert urls[0].endswith("/")                                   # پروبِ availability
    assert "module=printer_hp" in urls[1] and "module=printer_std" in urls[2]
    assert "auth=public_v2" in urls[1]


def test_unsupported_brand_never_reaches_the_exporter(monkeypatch):
    monkeypatch.setenv("USE_SNMP_EXPORTER", "1")

    class NoQuery:
        def is_available(self):
            return True

        def query_brand(self, *a, **k):
            raise _Boom("برای toshiba نباید از exporter پرسیده شود (SUPPORTED_BRANDS)")

    monkeypatch.setattr(poller, "client_from_env", lambda *a, **k: NoQuery())
    _enhanced_marker(monkeypatch)
    printer = dict(_PRINTER, brand="toshiba")
    result = poller._collect_inner(printer)
    assert result["from"] == "enhanced"


# ── ۵) شکلِ کدِ شاخه ──────────────────────────────────────────────────────
def test_branch_is_one_short_circuit_condition_inside_collect_inner():
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
    assert condition.op.__class__.__name__ == "And"     # short-circuit ⇒ با پرچمِ ۰ سوکت نیست
    body = ast.unparse(ifs[0].body)
    assert "collect_via_exporter" in body and "return exporter_result" in body
    assert "try" not in body and "pass" not in body     # قورت‌دادنِ خطا نداریم
    assert "exporter" not in ast.unparse(ifs[0].orelse)  # مسیرِ fallback همان collect_enhanced است


def test_exporter_layer_does_not_touch_snmp_directly():
    """لایهٔ exporter فقط HTTP است؛ هیچ import ای به pysnmp/`snmp_get` ندارد.

    دلیلِ این تست: اگر روزی کسی «برای راحتی» خواندنِ مستقیم SNMP را به مسیرِ exporter
    قاطی کند، parity سنجیده‌شده در G.6 بی‌معنی می‌شود.
    """
    for rel in ("core/exporter_client.py", "core/collectors/exporter_adapter.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for banned in ("pysnmp", "snmp_get", "octetstring", "easysnmp"):
            assert banned not in src, f"{rel}: {banned}"
