"""کاربرِ `snmp_exporter` — فاز G، گام G.5.2.

یک لایهٔ نازک برای خواندنِ پاسخِ exporter و نرمال‌کردنِ آن:

    client = ExporterClient()                       # http://localhost:9116
    metrics = client.query("172.16.0.5", "printer_hp", auth="public_v2")
    # → {"hp_model": [({"hp_model": "HP …"}, 1.0)], "bro_total": [({}, 12345.0)], …}

فرض‌های ساختاری که در همین session از **منبعِ exporter** (نسخهٔ v0.26.0، فایل‌های
`collector/collector.go` و `main.go`) خوانده شدند و در fixtureهای
`tests/fixtures/exporter_response_*.txt` هم ثابت شده‌اند:

* نامِ متریک دقیقاً فیلدِ `name:` در `monitoring/snmp.yml` است؛ هیچ پیشوندِ ماژولی
  اضافه نمی‌شود.
* متریک‌های متنی (`DisplayString`) مقدارِ `1` می‌دهند و متن را در **لیبلی هم‌نامِ خودِ
  متریک** می‌گذارند. `label_value()` همان را برمی‌دارد.
* self-metricsِ `snmp_scrape_*` لیبلِ ثابتِ `module="<module>"` دارند.
* خودِ exporter لیبلِ `instance` اضافه نمی‌کند (جست‌وجوی `instance` در collector.go و
  main.go: صفر نتیجه)؛ اگر Prometheus یا نسخه‌ای لیبل اضافه کند، این پارسر آن را
  نادیده می‌گیرد چون فقط لیبل‌های لازم را می‌خواند.

**چه چیزی تأیید نشده:** هیچ تماسِ واقعی با exporter در این sandbox انجام نشد (دانلودنِ
باینری و اجرای SNMP در فاز G ممنوع بود)؛ parse/normalize در مقابلِ fixtureهای
دست‌نویس تست شده‌اند و رفتارِ شبکه در مقابلِ transport جعلی. سنجشِ واقعی در سرور مالک
و در گام G.6 است.

این فایل به‌تنهایی هیچ اثری روی رفتارِ برنامه ندارد: شاخهٔ مصرف‌کننده در
`core/poller.py` تنها با `USE_SNMP_EXPORTER=1` فعال می‌شود (`exporter_enabled()`).

**گام H.2 — backoff و مدارِ باز:** شمارشِ «چند بار پیاپی نشد؟» در همین کلاینت نگه داشته
می‌شود (`DeviceState` به‌ازای `(ip, module)`)، چون poller هر ۶۰ ثانیه می‌چرخد و پرینترِ
خاموش نباید هر بار سه درخواستِ بی‌پاسخ بگیرد. نردبان، `ExporterCircuitOpen` و قاعدهٔ
«موفقیت ⇒ صفر» پایینِ همین فایل‌اند؛ شرحِ تصمیم و جایگزین‌هایِ ردشده:
`docs/phase-h-decisions.md` §H.2 (و بدهیِ «state در حافظه» در `docs/tech-debt.md`).
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("PrinterMonitor")

#: نامِ متغیرِ محیطیِ روشن‌کنندهٔ مسیرِ exporter. پیش‌فرض **خاموش** است؛ تا گام G.6 که
#: مقایسه با مسیرِ SNMPِ مستقیم انجام نشده، روشن‌کردنش توصیه نمی‌شود.
ENV_FLAG = "USE_SNMP_EXPORTER"
#: آدرسِ exporter. فقط برای این خوانده می‌شود که نصبِ exporter روی پورت/ماشینِ دیگری
#: بدونِ تغییرِ کد ممکن باشد؛ پیش‌فرض localhost است چون مصرف‌کننده روی همان ماشین است.
ENV_URL = "SNMP_EXPORTER_URL"

DEFAULT_BASE_URL = "http://localhost:9116"
DEFAULT_TIMEOUT = 5.0
#: timeoutِ کوتاهِ «آیا exporter هست؟» — این پرس‌وجو در هر poll صدا زده می‌شود.
AVAILABILITY_TIMEOUT = 2.0
DEFAULT_AUTH = "public_v2"

#: ماژول‌هایی که برای هر برند باید پرسیده شوند تا شکلِ collectorِ همان برند پر شود.
#: دلیلِ وجودِ `printer_std`: ماژول‌های vendor در spec ما تونر/سینی/در ندارند (برای
#: مثال `printer_hp` فقط ۱۴ متریک دارد و هیچکدام toner نیست) و آن داده‌ها در MIBِ
#: استاندارد است.
MODULES_BY_BRAND = {
    "hp": ("printer_hp", "printer_std"),
    "canon": ("printer_canon", "printer_std"),
}


def exporter_enabled() -> bool:
    """فقط `USE_SNMP_EXPORTER=1` مسیرِ exporter را روشن می‌کند (پیش‌فرض ۰).

    عمداً هنگامِ import خوانده نمی‌شود تا تست بتواند با تغییرِ محیط آن را عوض کند.
    """
    return os.environ.get(ENV_FLAG, "0").strip() == "1"


class ExporterError(RuntimeError):
    """خطای خواندن/پارسِ پاسخِ exporter — مصرف‌کننده باید به مسیرِ مستقیم برگردد."""



class ExporterCircuitOpen(ExporterError):
    """دستگاه در بازهٔ backoff است — این «خطا» نیست، «نوبتِ ما نیست».

    زیرکلاسِ `ExporterError` است تا مصرف‌کننده‌هایی که با یک class رفتار می‌کنند
    (`collect_via_exporter` دقیقاً همین) همان مسیرِ مستقیم را انتخاب کنند؛ ولی نوعِ جدا
    دارد، چون لاگِ آن مسیر warning است و با پرچمِ روشن یک پرینترِ خاموش هر ۶۰ ثانیه یک
    هشدارِ تازه می‌ساخت. `retry_in` هم دارد تا عیب‌یابی/تست عددِ باقی‌مانده را ببیند.
    """

    def __init__(self, message: str, retry_in: float = 0.0):
        super().__init__(message)
        self.retry_in = float(retry_in)


#: ── نردبانِ backoff (گام H.2) ────────────────────────────────────────────────
#: به‌ازای *شمارِ شکست‌هایِ پیاپی* روی همان `(ip, module)`:
#:   ۱–۲  ⇒ بی‌درنگ دوباره تلاش کن (خوابِ دستگاه و شلوغیِ لحظه‌ای شبکه زیاد می‌افتد)
#:   ۳–۵  ⇒ ۵ دقیقه
#:   ۶–۱۰ ⇒ ۳۰ دقیقه
#:   >۱۰  ⇒ مدار باز: ۶ ساعت — بعدش دقیقاً **یک** تلاشِ آزمونی، و موفقیت همه‌چیز را صفر می‌کند
#:
#: تأخیر از *لحظهٔ آخرین شکست* شمرده می‌شود، نه از آخرین تلاشِ ردشده؛ و در زمانِ backoff
#: هیچ درخواستی نمی‌رود، پس شمارنده هم جلو نمی‌رود — وگرنه سه روزِ قطعیِ یک پرینتر یعنی
#: ۴۳۲۰ «شکست» و بیرون‌رفتن از هر جدولی.
BACKOFF_STEPS: tuple[tuple[int, float], ...] = ((2, 0.0), (5, 300.0), (10, 1800.0))
CIRCUIT_OPEN_AFTER = 10
CIRCUIT_COOLDOWN = 6 * 60 * 60.0


def delay_for_failures(failures: int) -> float:
    """چند ثانیه باید صبر کرد؟ (تابعِ خالصِ نردبان؛ جدولش در `tests/test_exporter_backoff.py`)."""
    if failures <= 0:
        return 0.0
    for upper, delay in BACKOFF_STEPS:
        if failures <= upper:
            return delay
    return CIRCUIT_COOLDOWN


@dataclass
class DeviceState:
    """وضعیتِ یک `(ip, module)`.

    state در *کلاینت* است نه poller: poller هر چرخه `_exporter_client()` را صدا می‌زند و
    هیچ تضمینی برایِ عمرِ آن شیء نیست؛ کلاینت تنها جایی است که «پیاپی‌بودن» معنا دارد.
    فقط دستگاه‌هایِ شکست‌خورده رکورد دارند (موفقیت ⇒ حذفِ رکورد)، پس حافظه با تعدادِ
    پرینترها رشد نمی‌کند. `next_retry_at` در حافظهٔ زنده است و با reboot می‌پرد — عمدی:
    بیدار‌شدنِ سرویس نباید یک پرینترِ مرده را ۶ ساعت بلوکه کند.
    """

    consecutive_failures: int = 0
    next_retry_at: float = 0.0   # unix ثانیه؛ ۰ = «همین حالا می‌شود تلاش کرد»


def parse_prometheus_text(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """متنِ exposition را به `{name: [(labels, value), …]}` تبدیل می‌کند.

    قوانین (همان چیزی که در `tests/test_exporter_client.py` تست شده):
      * خطوطِ `#` (HELP/TYPE/…) و خط‌های خالی رد می‌شوند؛
      * `{name} {value}` و `{name}{labels…} {value}` هر دو پذیرفته می‌شوند؛
      * مقدار می‌تواند int/float/نمای علمی، `NaN`، `+Inf`/`-Inf` باشد؛
      * escapingِ داخلِ لیبل (`\\"`, `\\\\`, `\\n`) باز می‌شود؛
      * سری‌های تکراریِ هم‌نام کنارِ هم می‌مانند (Advisory: ترتیبِ پاسخِ exporter
        تضمین‌شده نیست؛ برای متریک‌های scalarِ ما آخرین سری خوانده می‌شود —
        `latest_series()`).
    خطای پارس (سطرِ بی‌شکل یا مقدارِ غیرعددی) `ExporterError` می‌دهد، نه skipِ خاموش:
    دادهٔ نصفه‌نیمه نباید خودش را به‌جای دادهٔ دستگاه جا بزند.
    """
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, labels_part = _split_name(line, lineno)
        body = line[len(name):]
        labels: dict[str, str] = {}
        if body.startswith("{"):
            close = body.rfind("}")
            if close < 0:
                raise ExporterError(f"سطر {lineno}: آکولادِ لیبل بسته نشده")
            labels = _parse_labels(body[1:close], lineno)
            rest = body[close + 1:]
        else:
            rest = body
        value = _parse_value(rest.strip(), lineno, name)
        out.setdefault(name, []).append((labels, value))
    return out


def _split_name(line: str, lineno: int) -> tuple[str, str]:
    for i, ch in enumerate(line):
        if ch in "{ \t":
            name = line[:i]
            if not name or not (name[0].isalpha() or name[0] == "_"):
                raise ExporterError(f"سطر {lineno}: نامِ متریک نامعتبر است: {line[:40]!r}")
            return name, line[i:]
    raise ExporterError(f"سطر {lineno}: مقدارِ متریک پیدا نشد: {line[:40]!r}")


def _parse_value(token: str, lineno: int, name: str) -> float:
    # خطِ زمانِ Prometheus این سه کلمه را آزاد می‌گذارد؛ ما هم می‌پذیریم و بعداً
    # مقدارِ غیرمحدود را «دادهٔ نامعتبر» می‌دانیم (finite_value).
    if token in ("NaN", "nan"):
        return float("nan")
    if token in ("+Inf", "Inf", "inf"):
        return float("inf")
    if token in ("-Inf", "-inf"):
        return float("-inf")
    try:
        return float(token)
    except ValueError:
        raise ExporterError(f"سطر {lineno}: مقدارِ عددی نیست ({name}={token!r})") from None


_ESC_REPL = {"n": "\n", '"': '"', "\\": "\\"}


def _parse_labels(blob: str, lineno: int) -> dict[str, str]:
    """لیبل‌ها را با جداسازیِ «ویرگولِ بیرونِ کوتیشن» می‌شکند."""
    labels: dict[str, str] = {}
    parts: list[str] = []
    buf, in_str, esc = "", False, False
    for ch in blob:
        if esc:
            buf += _ESC_REPL.get(ch, "\\" + ch)
            esc = False
            continue
        if in_str and ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            buf += ch
            continue
        if ch == "," and not in_str:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        parts.append(buf)
    if in_str:
        raise ExporterError(f"سطر {lineno}: کوتیشنِ لیبل بسته نشد")
    for part in parts:
        key, sep, val = part.partition("=")
        if not sep:
            raise ExporterError(f"سطر {lineno}: لیبل بدونِ «=»: {part.strip()!r}")
        labels[key.strip()] = val.strip().strip('"')
    return labels


def latest_series(metrics: dict, name: str) -> tuple[dict[str, str], float] | None:
    series = metrics.get(name)
    return series[-1] if series else None


def metric_value(metrics: dict, name: str, default=None):
    """مقدارِ scalarِ یک متریک، یا `default`. `NaN`/`Inf` «دادهٔ نامعتبر» است."""
    series = latest_series(metrics, name)
    if series is None:
        return default
    value = series[1]
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def label_value(metrics: dict, name: str, default=None) -> str | None:
    """متنِ متریک‌های `DisplayString`: مقدارِ لیبلِ هم‌نامِ متریک (collector.go).

    fallback: اگر نسخه‌ای متن را در لیبلِ دیگری گذاشت، اولین لیبلِ غیراز‌`instance`/
    `module` خوانده می‌شود — و اگر هیچ لیبلی نبود `default`.
    """
    series = latest_series(metrics, name)
    if series is None:
        return default
    labels = series[0]
    if name in labels:
        return labels[name]
    for key, val in labels.items():
        if key not in ("instance", "module"):
            return val
    return default


class ExporterClient:
    """کوئریِ ماژول‌ها از exporter. هیچ وابستگیِ دیگری به کدِ برنامه ندارد."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT,
                 open_fn=None, clock=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        # باز‌شدنِ hook برای تست (بدونِ monkeypatchِ global):
        self._open = open_fn or urllib.request.urlopen
        #: ساعتِ inject-پذیر: نردبان با «ثانیه» کار می‌کند و تست نباید ۶ ساعت بخوابد.
        self._clock = clock or time.time
        #: `(ip, module) → DeviceState` — فقط برایِ دستگاه‌هایی که *حداقل یک* شکست دارند.
        self._states: dict[tuple[str, str], DeviceState] = {}
        # poller هر پرینتر را در thread خودش می‌زند (core/poller.py:384) ⇒ این dict مشترک است.
        self._states_lock = threading.Lock()

    # ── URL ────────────────────────────────────────────────────────────────
    def url_for(self, ip: str, module: str, auth: str = DEFAULT_AUTH) -> str:
        query = urllib.parse.urlencode({"target": ip, "module": module, "auth": auth})
        return f"{self.base_url}/snmp?{query}"

    # ── خواندن ─────────────────────────────────────────────────────────────
    def query(self, ip: str, module: str = "printer_std",
              auth: str = DEFAULT_AUTH) -> dict:
        """یک ماژول را می‌خواند و dictِ `{name: [(labels, value), …]}` برمی‌گرداند.

        هر خطای شبکه/HTTP یا پارس → `ExporterError` (مصرف‌کننده باید به مسیرِ
        مستقیم برگردد؛ دادهٔ نصفه‌نیمه تحویل نمی‌دهیم).

        **گام H.2:** اگر `(ip, module)` در بازهٔ backoff باشد *هیچ درخواستی* نمی‌رود و
        `ExporterCircuitOpen` بالا می‌آید. هر `ExporterError` (شبکه، HTTP، یا پارس) شمارنده
        را یکی جلو می‌برد و پاسخِ سالم رکورد را کلاً حذف می‌کند.
        """
        wait = self.retry_in(ip, module)
        if wait > 0:
            raise ExporterCircuitOpen(
                f"{ip}/{module} در backoff است ({int(wait)}s دیگر تلاشِ بعدی؛ "
                f"شکست‌هایِ پیاپی: {self.consecutive_failures(ip, module)})", retry_in=wait)
        url = self.url_for(ip, module, auth)
        log.debug("exporter: GET %s", url)
        try:
            try:
                with self._open(url, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", None) or resp.getcode()
                    if int(status) != 200:
                        raise ExporterError(f"پاسخِ {status} از {url}")
                    body = resp.read()
            except ExporterError:
                raise
            except (urllib.error.URLError, OSError, ValueError) as exc:
                reason = getattr(exc, "reason", exc)
                raise ExporterError(f"خواندنِ {url} ممکن نشد: {reason}") from None
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            metrics = parse_prometheus_text(body)
        except ExporterError as exc:
            self._note_failure(ip, module, exc)
            raise
        self._note_success(ip, module)
        return metrics

    def query_brand(self, ip: str, brand: str, auth: str = DEFAULT_AUTH) -> dict | None:
        """ماژول‌های آن برند را خوانده و یکی می‌کند. برندِ ناشناخته → None.

        اگر همهٔ ماژول‌ها خالی برگشتند، dictِ خالی برمی‌گردد (نه None)؛ تشخیصِ
        «دادهٔ قابل‌استفاده هست یا نه» با `exporter_adapter.exporter_to_collector_shape`
        است که در آنجا روی None شدن تصمیم گرفته می‌شود.

        **گام H.2:** اگر *هر یک* از ماژول‌ها در backoff باشند، `ExporterCircuitOpen` (پیش ازِ
        هیچ درخواستی).
        """
        modules = MODULES_BY_BRAND.get((brand or "").strip().lower())
        if not modules:
            return None
        # H.2: یک ماژولِ در backoff ⇒ کلِ برند رد می‌شود. دلیل: شکلِ collector با نیمی از
        # ماژول‌ها کامل نمی‌شود (HP = printer_hp + printer_std) و snapshotِ نصفه از نبودِ
        # snapshot بدتر است — آنگاه poller مسیرِ مستقیم را می‌نویسد که کامل است.
        for module in modules:
            wait = self.retry_in(ip, module)
            if wait > 0:
                raise ExporterCircuitOpen(
                    f"{ip}: ماژولِ {module} در backoff است ({int(wait)}s دیگر تلاشِ بعدی)",
                    retry_in=wait)
        merged: dict = {}
        for module in modules:
            merged.update(self.query(ip, module, auth))
        return merged

    # ── backoff / circuit breaker (گام H.2) ────────────────────────────────
    def retry_in(self, ip: str, module: str) -> float:
        """چند ثانیه تا تلاشِ بعدی؟ ۰ یعنی «همین حالا»."""
        with self._states_lock:
            st = self._states.get((ip, module))
            if st is None or st.next_retry_at <= 0:
                return 0.0
            return max(0.0, st.next_retry_at - self._clock())

    def consecutive_failures(self, ip: str, module: str) -> int:
        """شمارشِ شکست‌هایِ پیاپی (۰ اگر رکوردی نیست)."""
        with self._states_lock:
            st = self._states.get((ip, module))
            return st.consecutive_failures if st else 0

    def device_states(self) -> dict:
        """نمایِ خواندنیِ only-ِ‌دستگاه‌هایِ در backoff — برایِ عیب‌یابی/تست (و وبِ آینده).

        کلیدِ `"ip|module"` رشته‌ای است نه tuple، تا در JSON هم قابلِ نمایش بماند.
        """
        now = self._clock()
        with self._states_lock:
            items = list(self._states.items())
        return {f"{ip}|{module}": {
                    "consecutive_failures": st.consecutive_failures,
                    "retry_in": round(max(0.0, st.next_retry_at - now), 1),
                    "circuit_open": st.consecutive_failures > CIRCUIT_OPEN_AFTER,
                }
                for (ip, module), st in items}

    def reset_backoff(self, ip: str | None = None) -> int:
        """پاک کردنِ state (دستی/تست). `ip=None` یعنی همه. تعدادِ پاک‌شده را برمی‌گرداند."""
        with self._states_lock:
            keys = [k for k in self._states if ip is None or k[0] == ip]
            for k in keys:
                del self._states[k]
        return len(keys)

    def _note_failure(self, ip: str, module: str, exc: Exception | None = None) -> DeviceState:
        """یک شکست: شمارنده +۱ و `next_retry_at` از نردبان. snapshot برمی‌گرداند."""
        key = (ip, module)
        with self._states_lock:
            st = self._states.setdefault(key, DeviceState())
            st.consecutive_failures += 1
            delay = delay_for_failures(st.consecutive_failures)
            st.next_retry_at = (self._clock() + delay) if delay > 0 else 0.0
            snap = DeviceState(st.consecutive_failures, st.next_retry_at)
        if snap.consecutive_failures == CIRCUIT_OPEN_AFTER + 1:
            # دقیقاً یک بار، در لحظهٔ باز شدنِ مدار؛ بعد از آن سکوت (لاگِ poll نباید سیلِ
            # هشدار شود) و بستنِ مدار هم بی‌صداست — دادهٔ تازه در مسیرِ اصلی لاگ دارد.
            log.warning("exporter: مدارِ %s/%s باز شد — تا %.1f ساعت هیچ کوئری‌ای نمی‌رود و "
                        "مسیرِ مستقیم می‌نویسد (شکستِ آخر: %s)",
                        ip, module, CIRCUIT_COOLDOWN / 3600.0, exc)
        return snap

    def _note_success(self, ip: str, module: str) -> None:
        """موفقیت ⇒ صفر؛ رکورد حذف می‌شود تا `_states` بزرگ نشود."""
        with self._states_lock:
            self._states.pop((ip, module), None)

    # ── در دسترس بودن ──────────────────────────────────────────────────────
    def is_available(self) -> bool:
        """`GET /` با timeoutِ کوتاه. هر خطایی → False (هیچ استثنایی بالا نمی‌آید).

        عمداً پاسخِ HTTP را چک نمی‌کند که «exporter سالم است» را ادعا کند؛ فقط می‌گوید
        جایی که انتظارش را داریم چیزی گوش می‌دهد.
        """
        url = f"{self.base_url}/"
        try:
            with self._open(url, timeout=AVAILABILITY_TIMEOUT) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                return int(status) == 200
        except Exception as exc:  # timeout/DNS/URLError — هر چیزی
            log.debug("exporter unavailable at %s: %s", url, exc)
            return False


def client_from_env() -> ExporterClient:
    """کلاینتِ ساخته‌شده از محیط؛ هیچ ترافیکی در زمانِ ساخت انجام نمی‌شود."""
    return ExporterClient(os.environ.get(ENV_URL, DEFAULT_BASE_URL))
