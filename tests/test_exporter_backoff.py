"""گام H.2 — backoff و circuit-breaker در `core/exporter_client.py`.

قاعدهٔ تست‌ها: هیچ ساعتِ واقعی‌ای جلو نمی‌رود (clock تزریق می‌شود) و هیچ سوکتی باز نمی‌شود
(transport جعلی، همان `_Resp`/`_FakeOpen`ِ آشنایِ `tests/test_exporter_client.py`). هیچ عددی
در این فایل «از سرِ خود» نیست: هر ۴ عددِ نردبان (۰ / ۳۰۰ / ۱۸۰۰ / ۲۱۶۰۰) از برنامهٔ گام H.2
آمده و با `test_ladder_matches_the_plan` قفل شده است.
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
    """ساعتِ قابلِ کنترل (unix ثانیه، همان چیزی که `DeviceState.next_retry_at` می‌شمارد)."""

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Resp:
    def __init__(self, body: bytes = b"", status: int = 200):
        self._body, self.status = body, status

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpen:
    """transport جعلی: پاسخ‌ها را به ترتیب می‌دهد و بعد همان آخری را تکرار می‌کند."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if not self.responses:
            raise ec.urllib.error.URLError("connection refused")
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def n(self) -> int:
        return len(self.calls)


GOOD = (FIXTURES / "exporter_response_sample.txt").read_text(encoding="utf-8").encode("utf-8")


def _refused(times: int = 1):
    """`times` خطایِ شبکه (بعد از آن آخرین پاسخ تکرار می‌شود — یعنی همیشه شکست)."""
    return [ec.urllib.error.URLError("connection refused") for _ in range(times)]


def _down_then_up(failures: int):
    """فیکِ ترتیبی: `failures` بار شکست، بعد همیشه پاسخِ سالم."""
    return _FakeOpen(*_refused(failures), _Resp(GOOD))


def _client(*responses, clock=None):
    fake = _FakeOpen(*responses) if responses else _FakeOpen()
    return ec.ExporterClient("http://127.0.0.1:9116", open_fn=fake, clock=clock or _Clock()), fake


def _fail_n(client, fake, clock, n, ip="10.0.0.1", module="printer_hp"):
    """n شکستِ *واقعی* (با فاصله‌ای به‌اندازهٔ ۶ ساعت، تا هیچ‌کدام در backoff نیفتد)."""
    for _ in range(n):
        clock.advance(ec.CIRCUIT_COOLDOWN + 1)
        with pytest.raises(ec.ExporterError):
            client.query(ip, module)
    return fake.n


# ── ۱) خودِ نردبان ──────────────────────────────────────────────────────────
def test_ladder_matches_the_plan():
    """۱–۲ فوری · ۳–۵ ← ۵ دقیقه · ۶–۱۰ ← ۳۰ دقیقه · >۱۰ ← ۶ ساعت."""
    assert [ec.delay_for_failures(i) for i in range(0, 13)] == [
        0.0, 0.0, 0.0,            # صفر/یک/دو شکست ⇒ بی‌درنگ
        300.0, 300.0, 300.0,     # سه تا پنج
        1800.0, 1800.0, 1800.0, 1800.0, 1800.0,   # شش تا ده
        21600.0, 21600.0,       # یازده به بعد = مدار باز
    ]
    assert ec.CIRCUIT_OPEN_AFTER == 10 and ec.CIRCUIT_COOLDOWN == 6 * 60 * 60.0
    assert ec.BACKOFF_STEPS == ((2, 0.0), (5, 300.0), (10, 1800.0))


# ── ۲) رفتارِ کلاینت ────────────────────────────────────────────────────────
def test_first_two_failures_do_not_delay_anything():
    clock = _Clock()
    client, fake = _client(clock=clock)
    assert _fail_n(client, fake, clock, 2) == 2
    assert client.retry_in("10.0.0.1", "printer_hp") == 0.0, "دو شکست اول پنجره ندارد"
    with pytest.raises(ec.ExporterError):
        client.query("10.0.0.1", "printer_hp")    # سومین درخواست هم بی‌درنگ می‌رود
    assert fake.n == 3 and client.consecutive_failures("10.0.0.1", "printer_hp") == 3


def test_third_failure_opens_a_five_minute_window_and_skips_the_sixth_request():
    """معیارِ برنامه: «سه شکست ⇒ درخواستِ ششم رد می‌شود» — و رد شدن یعنی *هیچ* سوکتی."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3)
    assert fake.n == 3 and client.retry_in("10.0.0.1", "printer_hp") == 300.0

    for _ in range(3):                      # تلاش‌های ۴، ۵ و ۶ در همان پنجره
        with pytest.raises(ec.ExporterCircuitOpen) as exc:
            client.query("10.0.0.1", "printer_hp")
        assert exc.value.retry_in > 0
    assert fake.n == 3, "در زمانِ backoff نباید درخواستی برود"
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 3, "شمارنده جلو نمی‌رود"


def test_url_is_called_again_after_the_wait():
    """بعد ازِ پنجره، دقیقاً یک درخواستِ تازه — پس دستگاهِ برگشته بی‌دستکاریِ دستی می‌آید."""
    clock = _Clock()
    fake = _down_then_up(3)                      # سه شکست، و از آن به بعد سالم
    client = ec.ExporterClient("http://127.0.0.1:9116", open_fn=fake, clock=clock)
    _fail_n(client, fake, clock, 3)
    before = fake.n

    clock.advance(299.0)
    with pytest.raises(ec.ExporterCircuitOpen):
        client.query("10.0.0.1", "printer_hp")
    assert fake.n == before, "یک ثانیه زودتر ⇒ نه"

    clock.advance(2.0)                        # ۳۰۱ از آخرین شکست
    metrics = client.query("10.0.0.1", "printer_hp")
    assert fake.n == before + 1 and metrics, "درخواست رفت و داده آمد"
    assert client.device_states() == {}, "موفقیت ⇒ رکورد حذف"


def test_success_resets_the_counter():
    clock = _Clock()
    fake = _FakeOpen(*(_refused(2) + [_Resp(GOOD)] + _refused(2)))   # شکست،شکست،سالم،شکست،شکست
    client = ec.ExporterClient("http://127.0.0.1:9116", open_fn=fake, clock=clock)
    _fail_n(client, fake, clock, 2)
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 2
    clock.advance(ec.CIRCUIT_COOLDOWN)
    assert client.query("10.0.0.1", "printer_hp"), "پاسخِ سالم"
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 0
    _fail_n(client, fake, clock, 2)                                  # دو شکستِ تازه
    assert client.retry_in("10.0.0.1", "printer_hp") == 0.0, "دوباره از اولِ نردبان"
    assert fake.n == 5 and len(fake.calls) == 5


def test_circuit_opens_after_eleven_and_probes_once_every_six_hours():
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
        client.query("10.0.0.1", "printer_hp")     # یک تلاشِ آزمونی، و باز هم شکست
    assert fake.n == 12, "دقیقاً یک پروب، نه هر poll"
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 12


def test_backoff_is_per_ip_and_per_module():
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="10.0.0.1", module="printer_hp")
    assert client.retry_in("10.0.0.1", "printer_hp") == 300.0
    assert client.retry_in("10.0.0.2", "printer_hp") == 0.0, "پرینترِ دیگر بی‌خبر"
    assert client.retry_in("10.0.0.1", "printer_std") == 0.0, "ماژولِ دیگر بی‌خبر"


def test_query_brand_refuses_the_whole_brand_when_one_module_is_open():
    """نیمه‌خواندن نداریم: اگر یکی از ماژول‌ها در backoff است، کل برند رد می‌شود."""
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="10.0.0.1", module="printer_std")
    before = fake.n
    with pytest.raises(ec.ExporterCircuitOpen) as exc:
        client.query_brand("10.0.0.1", "hp")
    assert "printer_std" in str(exc.value)
    assert fake.n == before, "نباید حتی printer_hp هم خوانده شود"


def test_circuit_open_is_an_exporter_error_so_old_handlers_still_work():
    """سازگاریِ عقب‌رو: `except ExporterError` (مثلِ `collect_via_exporter`) پوشش می‌دهد."""
    assert issubclass(ec.ExporterCircuitOpen, ec.ExporterError)
    err = ec.ExporterCircuitOpen("x", retry_in=42)
    assert err.retry_in == 42.0


def test_healthy_devices_leave_no_state_behind():
    """معیارِ «state در حافظه»: فقط دستگاهِ شکست‌خورده رکورد دارد (حافظه با poll نمی‌رشد)."""
    clock = _Clock()
    client, fake = _client(_Resp(GOOD), clock=clock)
    for i in range(500):
        client.query(f"10.0.{i % 7}.{i % 23}", "printer_hp")
    assert fake.n == 500 and client.device_states() == {}


def test_parse_error_counts_as_a_failure():
    """exporter زنده ولی پاسخِ خراب ⇒ همان رفتارِ شبکه؛ وگرنه هر ۶۰ ثانیه یک exception می‌دهیم."""
    clock = _Clock()
    client, fake = _client(_Resp(b"this line is not prometheus\n"), clock=clock)
    with pytest.raises(ec.ExporterError):
        client.query("10.0.0.1", "printer_hp")
    assert client.consecutive_failures("10.0.0.1", "printer_hp") == 1


def test_circuit_open_warning_is_emitted_once(caplog):
    clock = _Clock()
    client, fake = _client(clock=clock)
    with caplog.at_level(logging.WARNING, logger="PrinterMonitor"):
        _fail_n(client, fake, clock, ec.CIRCUIT_OPEN_AFTER + 1)
        for _ in range(20):                     # بیست چرخهٔ ردشده
            with pytest.raises(ec.ExporterCircuitOpen):
                client.query("10.0.0.1", "printer_hp")
    # فقط لاگگرِ خودِ برنامه: `caplog.records` همۀ root را می‌گیرد و وگرنه به ترتیبِ اجرا
    # حساس می‌شود (یک رکوردِ بی‌ربط از جای دیگر، این assert را «شکستِ کاذب» می‌کرد).
    warns = [r.getMessage() for r in caplog.records
             if r.name == "PrinterMonitor" and r.levelno >= logging.WARNING]
    assert len(warns) == 1 and "مدارِ 10.0.0.1/printer_hp باز شد" in warns[0]


def test_reset_backoff_by_ip_and_globally():
    clock = _Clock()
    client, fake = _client(clock=clock)
    _fail_n(client, fake, clock, 3, ip="10.0.0.1")
    _fail_n(client, fake, clock, 3, ip="10.0.0.2")
    assert len(client.device_states()) == 2
    assert client.reset_backoff("10.0.0.1") == 1
    assert list(client.device_states()) == ["10.0.0.2|printer_hp"]
    assert client.retry_in("10.0.0.1", "printer_hp") == 0.0
    assert client.reset_backoff() == 1 and client.device_states() == {}


# ── ۳) آینه: مسیرِ collector باید «رد شدن» را بی‌سروصدا بخواند ────────────────
def test_adapter_turns_circuit_open_into_a_quiet_none(caplog):
    """هیچ snapshotِ نصفه‌ای، هیچ هشدارِ تکراری: فقط debug و `None` تا poller legacy برود."""
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
    """کلاینتِ جعلی که فقط `ExporterCircuitOpen` می‌دهد (بدونِ state واقعی)."""

    def __init__(self):
        self.calls = []

    def query_brand(self, ip, brand, auth):
        self.calls.append((ip, brand, auth))
        raise ec.ExporterCircuitOpen("backoff", retry_in=60.0)


def test_adapter_does_not_query_the_web_when_the_brand_is_skipped(monkeypatch):
    """ترتیبِ درست: اولِ exporter بپرس؛ اگر مدار باز بود، وب‌اسکرپ هم نباید اجرا شود."""
    hits = []
    monkeypatch.setattr(ea, "_scrape_hp_toners", lambda *a, **k: hits.append("hp") or None)
    client = _BoomClient()
    assert ea.collect_via_exporter(client, "172.16.0.5", "x", "hp") is None
    assert client.calls and hits == []


# ── ۴) هم‌زمانی (poller هر پرینتر را در thread می‌زند — core/poller.py:384) ─────
def test_concurrent_failures_lose_nothing():
    """state زیرِ lock است؛ این تست *آسیب* را می‌گیرد، نه «اثباتِ ضرورتِ lock».

    صادقانه: در CPython رقابتِ `x += 1` روی dict نادر است، پس این تست ممکن است بدونِ lock هم
    سبز بماند — چیزی که می‌سنجد این است که زیرِ بارِ هم‌زمانی نه شمارش می‌پرد و نه رکورد
    می‌پوسد (و اگر روزی lock حذف شود، حداقل تستِ بالادست `retry_in` هنوز معتبر است).
    """
    clock = _Clock()
    client, fake = _client(clock=clock)
    errors = []

    def worker(idx):
        try:
            for _ in range(10):
                # هر بار بیرونِ پنجره، تا «شکستِ واقعی» شمارش شود (تلاشِ ردشده شمارنده را
                # جلو نمی‌برد — همان چیزی که تستِ «شمارنده در backoff نمی‌پرد» می‌بندد)
                clock.advance(ec.CIRCUIT_COOLDOWN + 1)
                with pytest.raises(ec.ExporterError):
                    client.query(f"10.9.9.{idx}", "printer_hp")
        except Exception as exc:                        # pragma: no cover - فقط خرابیِ تست
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert fake.n == 160, "هر شکست یک درخواست، بدونِ دوباره‌شماری/گم‌شدن"
    assert all(client.consecutive_failures(f"10.9.9.{i}", "printer_hp") == 10 for i in range(16))
