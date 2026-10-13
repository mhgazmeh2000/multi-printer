# -*- coding: utf-8 -*-
"""
تست‌های `core/exporter_client.py` (گام G.5.2).

هیچ ترافیک شبکه‌ای در این فایل نیست: transport جعلی (`open_fn`) و fixtureهای
دست‌نویسِ `tests/fixtures/exporter_response_*.txt`. آنچه سنجیده می‌شود:

  ۱) پارسرِ متنِ exposition — خطِ کامنت/خالی، `{name} v` و `name{labels} v`،
     escapingِ لیبل، NaN/±Inf، سریِ تکراری، و ردّکردنِ صریحِ خطای پارس (نه skip)؛
  ۲) URLِ دقیقِ کوئری (`/snmp?target=…&module=…&auth=…`)، timeoutها، و اینکه
     هر خطای HTTP/شبکه به `ExporterError` تبدیل می‌شود (نه دادهٔ نصفه)؛
  ۳) `is_available()` هیچ استثنایی بالا نمی‌آورد؛
  ۴) پرچمِ `USE_SNMP_EXPORTER` پیش‌فرض خاموش است و «۱» تنها حالتِ روشن است؛
  ۵) fixtureها با `monitoring/snmp.yml` می‌خوانند: هر نامِ متریکِ در فایل‌ها،
     در spec هست و هیچ پیشوندِ ماژولی اختراع نشده.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import core.exporter_client as ec

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Resp:
    """جای‌نمای پاسخِ urllib (context manager + status/read)."""

    def __init__(self, body: bytes = b"", status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpen:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        item = self.responses.pop(0) if self.responses else _Resp(b"")
        if isinstance(item, Exception):
            raise item
        return item


# ── ۱) پارسر ──────────────────────────────────────────────────────────────
def test_parser_skips_comments_and_blank_lines():
    text = "# HELP x thing\n# TYPE x gauge\n\n   \nx 1\n"
    assert ec.parse_prometheus_text(text) == {"x": [({}, 1.0)]}


def test_parser_reads_labels_and_bare_forms_together():
    metrics = ec.parse_prometheus_text(
        'a{instance="1.2.3.4",module="m"} 2.5\nb 7e2\nc{inst="1"} 0\n'
    )
    assert metrics["a"] == [({"instance": "1.2.3.4", "module": "m"}, 2.5)]
    assert metrics["b"] == [({}, 700.0)]
    assert metrics["c"][0][1] == 0.0


def test_parser_unescapes_label_values():
    metrics = ec.parse_prometheus_text(r'm{m="He said \"hi\"\nback\\slash"} 1')
    assert metrics["m"][0][0]["m"] == 'He said "hi"\nback\\slash'


def test_parser_rejects_malformed_lines_rather_than_ignoring_them():
    for bad in ("just_a_name", 'broken{a="1" 2', 'x not-a-number', '{} 5'):
        with pytest.raises(ec.ExporterError):
            ec.parse_prometheus_text(bad + "\n")


def test_parser_keeps_duplicate_series_in_order():
    metrics = ec.parse_prometheus_text("x 1\nx 2\nx 3\n")
    assert [v for _labels, v in metrics["x"]] == [1.0, 2.0, 3.0]
    assert ec.metric_value(metrics, "x") == 3.0


def test_nonfinite_values_are_not_data():
    metrics = ec.parse_prometheus_text("a NaN\nb +Inf\nc -Inf\nd 1\n")
    for name in ("a", "b", "c"):
        assert ec.metric_value(metrics, name, "MISSING") == "MISSING", name
    assert ec.metric_value(metrics, "d") == 1.0


def test_label_value_prefers_the_metric_named_label():
    metrics = ec.parse_prometheus_text('hp_model{hp_model="M402dn",instance="i"} 1\n')
    assert ec.label_value(metrics, "hp_model") == "M402dn"
    # لیبلِ هم‌نام باید برنده باشد، حتی وقتی لیبلِ متنیِ دیگری هم هست (ترتیب مهم نیست)
    both = ec.parse_prometheus_text('hp_model{description="D",hp_model="M402dn"} 1\n')
    assert ec.label_value(both, "hp_model") == "M402dn"
    # نسخه‌ای که متن را در لیبلِ دیگری بگذارد: اولین لیبلِ غیراز instance/module
    other = ec.parse_prometheus_text('hp_model{instance="i",description="X"} 1\n')
    assert ec.label_value(other, "hp_model") == "X"
    # هیچ لیبلی نیست ⇒ default
    bare = ec.parse_prometheus_text("hp_model 1\n")
    assert ec.label_value(bare, "hp_model", "N/A") == "N/A"


# ── ۲/۳) کلاینت ───────────────────────────────────────────────────────────
def test_url_for_is_the_documented_probe_url():
    client = ec.ExporterClient("http://localhost:9116")
    assert client.url_for("172.16.0.5", "printer_hp") == (
        'http://localhost:9116/snmp?target=172.16.0.5&module=printer_hp&auth=public_v2'
    )
    assert "auth=v3corp" in client.url_for("1.1.1.1", "printer_std", "v3corp")


def test_query_hits_one_url_with_configured_timeout():
    fake = _FakeOpen(_Resp(_fixture("exporter_response_sample.txt").encode("utf-8")))
    client = ec.ExporterClient("http://127.0.0.1:9116", timeout=1.25, open_fn=fake)
    metrics = client.query("172.16.0.5", "printer_hp")
    assert len(fake.calls) == 1 and fake.calls[0][1] == 1.25
    assert "target=172.16.0.5" in fake.calls[0][0] and "module=printer_hp" in fake.calls[0][0]
    assert ec.label_value(metrics, "hp_serial") == "CNCVC0245A"


def test_query_translates_http_and_network_errors():
    fake_500 = _FakeOpen(_Resp(b"boom", status=500))
    client = ec.ExporterClient(open_fn=fake_500)
    with pytest.raises(ec.ExporterError) as exc:
        client.query("1.1.1.1", "printer_hp")
    assert "500" in str(exc.value)

    fake_down = _FakeOpen(ec.urllib.error.URLError("connection refused"))
    with pytest.raises(ec.ExporterError) as exc2:
        ec.ExporterClient(open_fn=fake_down).query("1.1.1.1", "printer_hp")
    assert "connection refused" in str(exc2.value)


def test_query_accepts_crlf_and_bytes():
    body = "hp_model{hp_model=\"X\"} 1\r\n# comment\r\n".encode("utf-8")
    client = ec.ExporterClient(open_fn=_FakeOpen(_Resp(body)))
    assert ec.label_value(client.query("1.1.1.1", "printer_hp"), "hp_model") == "X"


def test_is_available_never_raises():
    assert ec.ExporterClient(open_fn=_FakeOpen(_Resp(b"<html>", 200))).is_available() is True
    assert ec.ExporterClient(open_fn=_FakeOpen(_Resp(b"", 404))).is_available() is False
    assert ec.ExporterClient(open_fn=_FakeOpen(TimeoutError("x"))).is_available() is False
    assert ec.ExporterClient(open_fn=_FakeOpen(ec.urllib.error.URLError("refused"))).is_available() is False


def test_query_brand_merges_modules_and_refuses_unknown_brands():
    hp, std = _fixture("exporter_response_sample.txt"), _fixture("exporter_response_std.txt")
    fake = _FakeOpen(_Resp(hp.encode()), _Resp(std.encode()))
    client = ec.ExporterClient(open_fn=fake)
    merged = client.query_brand("172.16.0.5", "HP")
    assert "hp_model" in merged and "bro_total" in merged          # هر دو ماژول
    assert [url.split("module=")[1].split("&")[0] for url, _t in fake.calls] == [
        "printer_hp", "printer_std"
    ]
    assert client.query_brand("1.1.1.1", "toshiba") is None        # ماژولِ تعریف‌نشده
    assert client.query_brand("1.1.1.1", "") is None
    assert set(ec.MODULES_BY_BRAND) == {"hp", "canon"}             # فقط همین دو آینه شده‌اند


def test_client_from_env_reads_url_at_call_time(monkeypatch):
    monkeypatch.setenv("SNMP_EXPORTER_URL", "http://10.0.0.9:9117/")
    client = ec.client_from_env()
    assert client.base_url == "http://10.0.0.9:9117"               # rstrip شده
    monkeypatch.delenv("SNMP_EXPORTER_URL")
    assert ec.client_from_env().base_url == ec.DEFAULT_BASE_URL


# ── ۴) پرچم ────────────────────────────────────────────────────────────────
def test_flag_is_off_unless_exactly_one(monkeypatch):
    for value, expected in ((None, False), ("0", False), ("", False), ("1", True),
                            ("1 ", True), ("true", False), ("2", False)):
        if value is None:
            monkeypatch.delenv(ec.ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(ec.ENV_FLAG, value)
        assert ec.exporter_enabled() is expected, repr(value)
    assert ec.ENV_FLAG == "USE_SNMP_EXPORTER"


# ── ۵) fixture ↔ spec ─────────────────────────────────────────────────────
def test_fixture_metric_names_exist_in_the_spec():
    import yaml

    spec = yaml.safe_load((ROOT / "monitoring" / "snmp.yml").read_text(encoding="utf-8"))
    known = {m["name"] for mod in spec["modules"].values() for m in mod["metrics"]}
    self_metrics = {
        "snmp_scrape_duration_seconds", "snmp_scrape_walk_duration_seconds",
        "snmp_scrape_packets_sent", "snmp_scrape_packets_retried", "snmp_scrape_pdus_returned",
    }
    used = set()
    for name in ("exporter_response_sample.txt", "exporter_response_std.txt",
                 "exporter_response_canon.txt", "exporter_response_canon_std.txt"):
        text = _fixture(name)
        metrics = ec.parse_prometheus_text(text)
        assert metrics, name
        for metric in metrics:
            assert metric in known | self_metrics, f"{name}: متریکِ بیرونِ spec: {metric}"
            assert not metric.startswith("printer_"), f"پیشوندِ ماژول اختراع شده: {metric}"
            used.add(metric)
    # fixtureهای HP باید هر دو دسته را پوشش دهند: متریکِ vendor و متریکِ standard
    assert {"hp_model", "bro_total", "toner_remain_1", "sys_uptime"} <= used
