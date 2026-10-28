# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_exporter_client.py
# @brief:        Tests for the snmp_exporter HTTP client (step G.5.2).
# @details:      Responsibilities: Check exposition-format parsing and error handling.
#                Main API: _Resp, _FakeOpen, 16 test functions
#                Side effects: network I/O
# @dependencies: internal: core.exporter_client | external: pytest, yaml
# @notes:        None.
# =============================================================================

"""
`core/exporter_client.py` tests (step G.5.2).

No network traffic in this file: bogus transport (`open_fn`) and fixtures
Manuscript of `tests/fixtures/exporter_response_*.txt`. What is measured:

  1) exposition text parser — comment/empty line, `{name} v` and `name{labels} v`,
     label escaping, NaN/±Inf, repeated series, and explicit rejection of pars error (not skip);
  2) The exact URL of the query (`/snmp?target=…&module=…&auth=…`), timeouts, and that
     Any HTTP/network errors are converted to `ExporterError' (not half data);
  3) `is_available()` does not raise any exception;
  4) ``USE_SNMP_EXPORTER'' flag is off by default and "1" is the only on state;
  5) Fixtures are read with `monitoring/snmp.yml`: any metric name in the files,
     It is in the spec and no module prefix has been invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import core.exporter_client as ec

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _fixture(name: str) -> str:
    """Helper: fixture."""
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Resp:
    """instead of the urllib response view (context manager + status/read)."""

    def __init__(self, body: bytes = b"", status: int = 200):
        """Initialize the instance."""
        self._body = body
        self.status = status

    def read(self) -> bytes:
        """Helper: read."""
        return self._body

    def getcode(self) -> int:
        """Helper: getcode."""
        return self.status

    def __enter__(self):
        """Enter the context and return the object."""
        return self

    def __exit__(self, *exc):
        """Leave the context and release what was acquired."""
        return False


class _FakeOpen:
    """Helper class: fake open."""
    def __init__(self, *responses):
        """Initialize the instance."""
        self.responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url, timeout=None):
        """Call the object like a function."""
        self.calls.append((url, timeout))
        item = self.responses.pop(0) if self.responses else _Resp(b"")
        if isinstance(item, Exception):
            raise item
        return item


# ── 1) Parser
def test_parser_skips_comments_and_blank_lines():
    """Verifies parser skips comments and blank lines."""
    text = "# HELP x thing\n# TYPE x gauge\n\n   \nx 1\n"
    assert ec.parse_prometheus_text(text) == {"x": [({}, 1.0)]}


def test_parser_reads_labels_and_bare_forms_together():
    """Verifies parser reads labels and bare forms together."""
    metrics = ec.parse_prometheus_text(
        'a{instance="1.2.3.4",module="m"} 2.5\nb 7e2\nc{inst="1"} 0\n'
    )
    assert metrics["a"] == [({"instance": "1.2.3.4", "module": "m"}, 2.5)]
    assert metrics["b"] == [({}, 700.0)]
    assert metrics["c"][0][1] == 0.0


def test_parser_unescapes_label_values():
    """Verifies parser unescapes label values."""
    metrics = ec.parse_prometheus_text(r'm{m="He said \"hi\"\nback\\slash"} 1')
    assert metrics["m"][0][0]["m"] == 'He said "hi"\nback\\slash'


def test_parser_rejects_malformed_lines_rather_than_ignoring_them():
    """Verifies parser rejects malformed lines rather than ignoring them."""
    for bad in ("just_a_name", 'broken{a="1" 2', 'x not-a-number', '{} 5'):
        with pytest.raises(ec.ExporterError):
            ec.parse_prometheus_text(bad + "\n")


def test_parser_keeps_duplicate_series_in_order():
    """Verifies parser keeps duplicate series in order."""
    metrics = ec.parse_prometheus_text("x 1\nx 2\nx 3\n")
    assert [v for _labels, v in metrics["x"]] == [1.0, 2.0, 3.0]
    assert ec.metric_value(metrics, "x") == 3.0


def test_nonfinite_values_are_not_data():
    """Verifies nonfinite values are not data."""
    metrics = ec.parse_prometheus_text("a NaN\nb +Inf\nc -Inf\nd 1\n")
    for name in ("a", "b", "c"):
        assert ec.metric_value(metrics, name, "MISSING") == "MISSING", name
    assert ec.metric_value(metrics, "d") == 1.0


def test_label_value_prefers_the_metric_named_label():
    """Verifies label value prefers the metric named label."""
    metrics = ec.parse_prometheus_text('hp_model{hp_model="M402dn",instance="i"} 1\n')
    assert ec.label_value(metrics, "hp_model") == "M402dn"
    # The label with the same name must win, even when there is another text label (order doesn't matter).
    both = ec.parse_prometheus_text('hp_model{description="D",hp_model="M402dn"} 1\n')
    assert ec.label_value(both, "hp_model") == "M402dn"
    # The version that puts the text in another label: the first label other than instance/module
    other = ec.parse_prometheus_text('hp_model{instance="i",description="X"} 1\n')
    assert ec.label_value(other, "hp_model") == "X"
    # There is no label ⇒ default
    bare = ec.parse_prometheus_text("hp_model 1\n")
    assert ec.label_value(bare, "hp_model", "N/A") == "N/A"


# ───────────────────
def test_url_for_is_the_documented_probe_url():
    """Verifies url for is the documented probe url."""
    client = ec.ExporterClient("http://localhost:9116")
    assert client.url_for("172.16.0.5", "printer_hp") == (
        'http://localhost:9116/snmp?target=172.16.0.5&module=printer_hp&auth=public_v2'
    )
    assert "auth=v3corp" in client.url_for("1.1.1.1", "printer_std", "v3corp")


def test_query_hits_one_url_with_configured_timeout():
    """Verifies query hits one url with configured timeout."""
    fake = _FakeOpen(_Resp(_fixture("exporter_response_sample.txt").encode("utf-8")))
    client = ec.ExporterClient("http://127.0.0.1:9116", timeout=1.25, open_fn=fake)
    metrics = client.query("172.16.0.5", "printer_hp")
    assert len(fake.calls) == 1 and fake.calls[0][1] == 1.25
    assert "target=172.16.0.5" in fake.calls[0][0] and "module=printer_hp" in fake.calls[0][0]
    assert ec.label_value(metrics, "hp_serial") == "CNCVC0245A"


def test_query_translates_http_and_network_errors():
    """Verifies query translates http and network errors."""
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
    """Verifies query accepts crlf and bytes."""
    body = "hp_model{hp_model=\"X\"} 1\r\n# comment\r\n".encode("utf-8")
    client = ec.ExporterClient(open_fn=_FakeOpen(_Resp(body)))
    assert ec.label_value(client.query("1.1.1.1", "printer_hp"), "hp_model") == "X"


def test_is_available_never_raises():
    """Verifies is available never raises."""
    assert ec.ExporterClient(open_fn=_FakeOpen(_Resp(b"<html>", 200))).is_available() is True
    assert ec.ExporterClient(open_fn=_FakeOpen(_Resp(b"", 404))).is_available() is False
    assert ec.ExporterClient(open_fn=_FakeOpen(TimeoutError("x"))).is_available() is False
    assert ec.ExporterClient(open_fn=_FakeOpen(ec.urllib.error.URLError("refused"))).is_available() is False


def test_query_brand_merges_modules_and_refuses_unknown_brands():
    """Verifies query brand merges modules and refuses unknown brands."""
    hp, std = _fixture("exporter_response_sample.txt"), _fixture("exporter_response_std.txt")
    fake = _FakeOpen(_Resp(hp.encode()), _Resp(std.encode()))
    client = ec.ExporterClient(open_fn=fake)
    merged = client.query_brand("172.16.0.5", "HP")
    assert "hp_model" in merged and "bro_total" in merged          # Both modules
    assert [url.split("module=")[1].split("&")[0] for url, _t in fake.calls] == [
        "printer_hp", "printer_std"
    ]
    assert client.query_brand("1.1.1.1", "toshiba") is None        # Undefined module
    assert client.query_brand("1.1.1.1", "") is None
    assert set(ec.MODULES_BY_BRAND) == {"hp", "canon"}             # Only these two are mirrored


def test_client_from_env_reads_url_at_call_time(monkeypatch):
    """Verifies client from env reads url at call time."""
    monkeypatch.setenv("SNMP_EXPORTER_URL", "http://10.0.0.9:9117/")
    client = ec.client_from_env()
    assert client.base_url == "http://10.0.0.9:9117"               # rstriped
    monkeypatch.delenv("SNMP_EXPORTER_URL")
    assert ec.client_from_env().base_url == ec.DEFAULT_BASE_URL


# ── 4) Flag
def test_flag_is_off_unless_exactly_one(monkeypatch):
    """Verifies flag is off unless exactly one."""
    for value, expected in ((None, False), ("0", False), ("", False), ("1", True),
                            ("1 ", True), ("true", False), ("2", False)):
        if value is None:
            monkeypatch.delenv(ec.ENV_FLAG, raising=False)
        else:
            monkeypatch.setenv(ec.ENV_FLAG, value)
        assert ec.exporter_enabled() is expected, repr(value)
    assert ec.ENV_FLAG == "USE_SNMP_EXPORTER"


# 5) fixture ↔ spec
def test_fixture_metric_names_exist_in_the_spec():
    """Verifies fixture metric names exist in the spec."""
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
    # HP fixtures should cover both categories: vendor metric and standard metric
    assert {"hp_model", "bro_total", "toner_remain_1", "sys_uptime"} <= used
