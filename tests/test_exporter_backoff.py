# =============================================================================
# @file:         tests/test_exporter_backoff.py
# @brief:        Tests for backoff and circuit breaker in the exporter client (step H.2).
# @details:      Responsibilities: Verify the retry ladder and the open-circuit window.
#                Main API: _Clock, _Resp, _FakeOpen, _BoomClient, 16 test functions
#                Side effects: network I/O, starts threads
# @dependencies: internal: core.collectors.exporter_adapter, core.exporter_client | external: pytest
# @notes:        None.
# =============================================================================

"""Step H.2 — backoff and circuit-breaker in `core/exporter_client.py`.

Test rules: no real clock is advanced (clock is injected) and no socket is opened
(fake transport, the familiar `_Resp`/`_FakeOpen` of `tests/test_exporter_client.py`). no number
This file is not "from your own head": all 4 numbers of ladders (0 / 300 / 1800 / 21600) from the H.2 step program
comes and is locked with `test_ladder_matches_the_plan`.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.collectors.exporter_adapter as ea  # noqa: E402
import core.exporter_client as ec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class _Clock:
    """Controllable clock (unix seconds, what `DeviceState.next_retry_at` counts)."""

    def __init__(self, t: float = 1_700_000_000.0):
        """Initialize the instance."""
        self.t = t

    def __call__(self) -> float:
        """Call the object like a function."""
        return self.t

    def advance(self, seconds: float) -> None:
        """Helper: advance."""
        self.t += seconds


class _Resp:
    """Helper class: resp."""
    def __init__(self, body: bytes = b"", status: int = 200):
        """Initialize the instance."""
        self._body, self.status = body, status

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
    """Fake transport: Sorts the answers and then repeats the last one."""

    def __init__(self, *responses):
        """Initialize the instance."""
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url, timeout=None):
        """Call the object like a function."""
        self.calls.append(url)
        if not self.responses:
            raise ec.urllib.error.URLError("connection refused")
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def n(self) -> int:
        """Helper: n."""
        return len(self.calls)


GOOD = (FIXTURES / "exporter_response_sample.txt").read_text(encoding="utf-8").encode("utf-8")


def _refused(times: int = 1):
    """Network error `times' (after which the last response is repeated — i.e. always fails)."""
    return [ec.urllib.error.URLError("connection refused") for _ in range(times)]


def _down_then_up(failures: int):
    """Sequential fic: ``failures'' failure times, then always a healthy response."""
    return _FakeOpen(*_refused(failures), _Resp(GOOD))


def _client(*responses, clock=None):
    """Helper: client."""
    fake = _FakeOpen(*responses) if responses else _FakeOpen()
    return ec.ExporterClient("http://127.0.0.1:9116", open_fn=fake, clock=clock or _Clock()), fake


def _fail_n(client, fake, clock, n, ip="10.0.0.1", module="printer_hp"):
    """n *real* failures (with an interval of 6 hours, so that none fall into backoff)."""
    for _ in range(n):
        clock.advance(ec.CIRCUIT_COOLDOWN + 1)
        with pytest.raises(ec.ExporterError):
            client.query(ip, module)
    return fake.n


# ── 1) The ladder itself
def test_ladder_matches_the_plan():
    """1-2 instant · 3-5 → 5 minutes · 10-6 → 30 minutes · >10 → 6 hours."""
    assert [ec.delay_for_failures(i) for i in range(0, 13)] == [
        0.0, 0.0, 0.0,            # Zero/one/two failures ⇒ immediate
        300.0, 300.0, 300.0,     # three to five
        1800.0, 1800.0, 1800.0, 1800.0, 1800.0,   # six to ten
        21600.0, 21600.0,       # Eleven onwards = open circuit
    ]
    assert ec.CIRCUIT_OPEN_AFTER == 10 and ec.CIRCUIT_COOLDOWN == 6 * 60 * 60.0
    assert ec.BACKOFF_STEPS == ((2, 0.0), (5, 300.0), (10, 1800.0))


# ── 2) Client's behavior
def test_first_two_failures_do_not_delay_anything():
    """Verifies first two failures do not delay anything."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    assert _fail_n(client, fake, clock, 2) == 2
    assert client.retry_in("10.0.0.1", "printer_hp") == 0.0, "دو شکست اول پنجره ندارد"
    with pytest.raises(ec.ExporterError):
        client.query("10.0.0.1", "printer_hp")    # The third request goes immediately
    assert fake.n == 3 and client.consecutive_failures("10.0.0.1", "printer_hp") == 3


def test_third_failure_opens_a_five_minute_window_and_skips_the_sixth_request():
    """Program criteria: "Three failures ⇒ 6th request is rejected" — and rejection means *no* sockets."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3)
    assert fake.n == 3 and client.retry_in("10.0.0.1", "printer_hp") == 300.0

    for _ in range(3):                      # Attempts 4, 5 and 6 in the same window
        with pytest.raises(ec.ExporterCircuitOpen) as exc:
            client.query("10.0.0.1", "printer_hp")
        assert exc.value.retry_in > 0
    assert fake.n == 3, "در زمانِ backoff نباید درخواستی برود"
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 3, "شمارنده جلو نمی‌رود"


def test_url_is_called_again_after_the_wait():
    """After the window, exactly one new request — so the returned device comes without manual manipulation."""
    clock = _Clock()
    fake = _down_then_up(3)                      # Three failures, and since then healthy
    client = ec.ExporterClient("http://127.0.0.1:9116", open_fn=fake, clock=clock)
    _fail_n(client, fake, clock, 3)
    before = fake.n

    clock.advance(299.0)
    with pytest.raises(ec.ExporterCircuitOpen):
        client.query("10.0.0.1", "printer_hp")
    assert fake.n == before, "یک ثانیه زودتر ⇒ نه"

    clock.advance(2.0)                        # 301 from the last failure
    metrics = client.query("10.0.0.1", "printer_hp")
    assert fake.n == before + 1 and metrics, "درخواست رفت و داده آمد"
    assert client.device_states() == {}, "موفقیت ⇒ رکورد حذف"


def test_success_resets_the_counter():
    """Verifies success resets the counter."""
    clock = _Clock()
    fake = _FakeOpen(*(_refused(2) + [_Resp(GOOD)] + _refused(2)))   # Failure, failure, healthy, failure, failure
    client = ec.ExporterClient("http://127.0.0.1:9116", open_fn=fake, clock=clock)
    _fail_n(client, fake, clock, 2)
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 2
    clock.advance(ec.CIRCUIT_COOLDOWN)
    assert client.query("10.0.0.1", "printer_hp"), "پاسخِ سالم"
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 0
    _fail_n(client, fake, clock, 2)                                  # Two new failures
    assert client.retry_in("10.0.0.1", "printer_hp") == 0.0, "دوباره از اولِ نردبان"
    assert fake.n == 5 and len(fake.calls) == 5


def test_circuit_opens_after_eleven_and_probes_once_every_six_hours():
    """Verifies circuit opens after eleven and probes once every six hours."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 11)
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 11
    state = client.device_states()["10.0.0.1|printer_hp"]
    assert state["circuit_open"] is True and 21500 < state["retry_in"] <= 21600.0

    clock.advance(ec.CIRCUIT_COOLDOWN - 60)
    with pytest.raises(ec.ExporterCircuitOpen):
        client.query("10.0.0.1", "printer_hp")
    clock.advance(120)
    with pytest.raises(ec.ExporterError):
        client.query("10.0.0.1", "printer_hp")     # A test attempt, and again failure
    assert fake.n == 12, "دقیقاً یک پروب، نه هر poll"
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 12


def test_backoff_is_per_ip_and_per_module():
    """Verifies backoff is per ip and per module."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="10.0.0.1", module="printer_hp")
    assert client.retry_in("10.0.0.1", "printer_hp") == 300.0
    assert client.retry_in("10.0.0.2", "printer_hp") == 0.0, "پرینترِ دیگر بی‌خبر"
    assert client.retry_in("10.0.0.1", "printer_std") == 0.0, "ماژولِ دیگر بی‌خبر"


def test_query_brand_refuses_the_whole_brand_when_one_module_is_open():
    """We don't have half reads: if one of the modules is in backoff, the whole brand is rejected."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="10.0.0.1", module="printer_std")
    before = fake.n
    with pytest.raises(ec.ExporterCircuitOpen) as exc:
        client.query_brand("10.0.0.1", "hp")
    assert "printer_std" in str(exc.value)
    assert fake.n == before, "نباید حتی printer_hp هم خوانده شود"


def test_circuit_open_is_an_exporter_error_so_old_handlers_still_work():
    """Backwards compatibility: covers `except ExporterError` (like `collect_via_exporter`)."""
    assert issubclass(ec.ExporterCircuitOpen, ec.ExporterError)
    err = ec.ExporterCircuitOpen("x", retry_in=42)
    assert err.retry_in == 42.0


def test_healthy_devices_leave_no_state_behind():
    """"state in memory" criterion: only the failed device has a record (memory does not grow with polling)."""
    clock = _Clock()
    client, fake = _client(_Resp(GOOD), clock=clock)
    for i in range(500):
        client.query(f"10.0.{i % 7}.{i % 23}", "printer_hp")
    assert fake.n == 500 and client.device_states() == {}


def test_parse_error_counts_as_a_failure():
    """Exporter is alive but the response is broken ⇒ the same behavior of the network; Otherwise, we throw an exception every 60 seconds."""
    clock = _Clock()
    client, fake = _client(_Resp(b"this line is not prometheus\n"), clock=clock)
    with pytest.raises(ec.ExporterError):
        client.query("10.0.0.1", "printer_hp")
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 1


def test_circuit_open_warning_is_emitted_once(caplog):
    """Verifies circuit open warning is emitted once."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        _fail_n(client, fake, clock, ec.CIRCUIT_OPEN_AFTER + 1)
        for _ in range(20):                     # Twenty missed cycles
            with pytest.raises(ec.ExporterCircuitOpen):
                client.query("10.0.0.1", "printer_hp")
    # Only the program's own logger: `caplog.records' takes all root and otherwise in order of execution
    # becomes sensitive (an irrelevant record from elsewhere would make this assert "false failure").
    warns = [r.getMessage() for r in caplog.records
             if r.name == "PrinterMonitor" and r.levelno >= logging.WARNING]
    assert len(warns) == 1 and "مدارِ 10.0.0.1/printer_hp باز شد" in warns[0]


def test_reset_backoff_by_ip_and_globally():
    """Verifies reset backoff by ip and globally."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="10.0.0.1")
    _fail_n(client, fake, clock, 3, ip="10.0.0.2")
    assert len(client.device_states()) == 2
    assert client.reset_backoff("10.0.0.1") == 1
    assert list(client.device_states()) == ["10.0.0.2|printer_hp"]
    assert client.retry_in("10.0.0.1", "printer_hp") == 0.0
    assert client.reset_backoff() == 1 and client.device_states() == {}


# ── 3) Mirror: the collector path must read "skip" silently ────────────────
def test_adapter_turns_circuit_open_into_a_quiet_none(caplog):
    """No partial snapshots, no repeated warnings: just debug and ``None'' to go poller legacy."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="172.16.0.5", module="printer_hp")
    _fail_n(client, fake, clock, 3, ip="172.16.0.5", module="printer_std")

    with caplog.at_level(logging.DEBUG, logger="PrinterMonitor"):
        assert ea.collect_via_exporter(client, "172.16.0.5", "HP-05", "hp") is None
    ours = [r for r in caplog.records if r.name == "PrinterMonitor"]
    assert [r for r in ours if r.levelno >= logging.WARNING] == [], "backoff هشدار نیست"
    assert any("در backoff است" in r.getMessage() for r in ours if r.levelno == logging.DEBUG)


class _BoomClient:
    """Fake client that only gives `ExporterCircuitOpen' (no real state)."""

    def __init__(self):
        """Initialize the instance."""
        self.calls = []

    def query_brand(self, ip, brand, auth):
        """Helper: query brand."""
        self.calls.append((ip, brand, auth))
        raise ec.ExporterCircuitOpen("backoff", retry_in=60.0)


def test_adapter_does_not_query_the_web_when_the_brand_is_skipped(monkeypatch):
    """The correct order: ask the exporter first; If the circuit was open, web scrap should not run either."""
    hits = []
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda *a, **k: hits.append("hp") or None)
    client = _BoomClient()
    assert ea.collect_via_exporter(client, "172.16.0.5", "x", "hp") is None
    assert client.calls and hits == []


# ── 4) At the same time (poller polls every printer in a thread — core/poller.py:402) ─────
def test_concurrent_failures_lose_nothing():
    """state is under lock; This test takes *damage*, not "lock necessity proof".

    Honestly: In CPython, ``x += 1'' race on dict is rare, so this test is possible without lock.
    Stay Green — What it measures is that neither the count nor the record jumps under the same time load
    rots (and if the lock is ever removed, at least the upstream `retry_in` test is still valid).
    """
    clock = _Clock()
    client, fake = _client(clock=clock)
    errors = []

    def worker(idx):
        """Helper: worker."""
        try:
            for _ in range(10):
                # Each time outside the window, until the "true failure" is counted (the failed attempt counter
                # does not advance — the same as the "counter does not jump in backoff" test closes)
                clock.advance(ec.CIRCUIT_COOLDOWN + 1)
                with pytest.raises(ec.ExporterError):
                    client.query(f"10.9.9.{idx}", "printer_hp")
        except Exception as exc:                        # pragma: no cover - test failure only
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert fake.n == 160, "هر شکست یک درخواست، بدونِ دوباره‌شماری/گم‌شدن"
    assert all(client.consecutive_failures(f"10.9.9.{i}", "printer_hp") == 10 for i in range(16))
