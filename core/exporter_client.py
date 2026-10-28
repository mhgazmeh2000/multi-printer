# =============================================================================
# @file:         core/exporter_client.py
# @brief:        HTTP client for snmp_exporter with a circuit breaker and backoff.
# @details:      Responsibilities: Fetch exporter metrics, parse the exposition text and track
#                failures per device.
#                Main API: exporter_enabled(), ExporterError, ExporterCircuitOpen,
#                delay_for_failures(), DeviceState, parse_prometheus_text(), latest_series(),
#                metric_value() (+3 more)
#                Side effects: network I/O, starts threads
# @dependencies: stdlib only
# @notes:        None.
# =============================================================================

"""User `snmp_exporter` — Phase G, Step G.5.2.

A thin layer to read the exporter's response and normalize it:

    client = ExporterClient()                       # http://localhost:9116
    metrics = client.query("172.16.0.5", "printer_hp", auth="public_v2")
    # → {"hp_model": [({"hp_model": "HP …"}, 1.0)], "bro_total": [({}, 12345.0)], …}

structural assumptions that in this session from **source exporter** (version v0.26.0, files
`collector/collector.go` and `main.go`) were read and in fixtures
`tests/fixtures/exporter_response_*.txt` are also fixed:

* The name of the metric is exactly the `name:` field in `monitoring/snmp.yml`; No module prefix
  It is not added.
* Text metrics (`DisplayString`) give the value `1` and put the text in a label of the same name.
  They put metric**. `label_value()` returns the same.
* self-metrics of `snmp_scrape_*` have fixed label `module="<module>"`.
* The exporter itself does not add `instance` label (search for `instance` in collector.go and
  main.go: nil result); If Prometheus or a version adds a label, this parser will
  It ignores because it only reads necessary labels.

**What's not verified:** No actual calls to the exporter were made in this sandbox (downloading the
binaries and SNMP execution were prohibited in phase G); parse/normalize against fixtures
Manuscripts have been tested and the behavior of the network in front of fake transport. Actual measurement on the owner's server
And it is in step G.6.

This file alone has no effect on the program's behavior: the consumer branch in the
`core/poller.py` is only enabled with `USE_SNMP_EXPORTER=1` (`exporter_enabled()`).

**Step H.2 — backoff and open circuit:** Count "How many consecutive failures?" kept in the same client
(`DeviceState` for `(ip, module)`), because the poller rotates every 60 seconds and the printer
Shutdown should not get three unanswered requests every time. ladder, `ExporterCircuitOpen` and rule
"Success ⇒ zero" are at the bottom of this file; Description of the decision and rejected alternatives:
`docs/phase-h-decisions.md` §H.2 (and ``state in memory'' debt in `docs/tech-debt.md`).
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

#: the name of the environment variable clarifying the path of the exporter. The default is **off**; until step G.6 that
#: Comparison with direct SNMP route is not done, it is not recommended to turn it on.
ENV_FLAG = "USE_SNMP_EXPORTER"
#: address of the exporter. It is read only to install the exporter on another port/machine
#: It is possible without changing the code; The default is localhost because the user is on the same machine.
ENV_URL = "SNMP_EXPORTER_URL"

DEFAULT_BASE_URL = "http://localhost:9116"
DEFAULT_TIMEOUT = 5.0
#: the short timeout of "is there an exporter?" - This query is called in every poll.
AVAILABILITY_TIMEOUT = 2.0
DEFAULT_AUTH = "public_v2"

#: Modules that must be asked for each brand to fill the collector form of the same brand.
#: The reason for `printer_std`: vendor modules in our spec do not have toner/tray/door (for
#: Example `printer_hp` has only 14 metrics and none of them are toner) and those data in MIB
#: It is standard.
MODULES_BY_BRAND = {
    "hp": ("printer_hp", "printer_std"),
    "canon": ("printer_canon", "printer_std"),
}


def exporter_enabled() -> bool:
    """``USE_SNMP_EXPORTER=1'' only turns on the exporter path (default 0).

    It is intentionally not read during import so that the test can change it by changing the environment.
    """
    return os.environ.get(ENV_FLAG, "0").strip() == "1"


class ExporterError(RuntimeError):
    """Error reading/parsing the exporter's response — the consumer must return to the direct path."""



class ExporterCircuitOpen(ExporterError):
    """The device is in the backoff interval — it's not "error", it's "not our turn".

    A subclass of ``ExporterError'' is a consumer that behaves with a class
    (`collect_via_exporter' is exactly that) choose the same direct path; But a different kind
    Yes, because the log of that path is a warning, and with the flag on, a printer is turned off every 60 seconds
    He made a new warning. It also has `retry_in' to debug/test the remaining number.
    """

    def __init__(self, message: str, retry_in: float = 0.0):
        """Create the error with a message and an optional retry delay in seconds."""
        super().__init__(message)
        self.retry_in = float(retry_in)


#: ── Backoff ladder (step H.2) ─────────────────────────────────────────────────
#: for *number of consecutive failures* on the same `(ip, module)`:
#: 1-2 ⇒ Try again immediately (the device sleeps and the network is temporarily busy)
#: 3-5 ⇒ 5 minutes
#: 6-10 ⇒ 30 minutes
#: >10 ⇒ open circuit: 6 hours — exactly **one** test attempt later, and success resets everything to zero
#:
#: The delay is counted from *the moment of the last failure*, not from the last failed attempt; And during backoff
#: No request goes, so the counter doesn't advance either - otherwise three days of downtime for a printer means
#: 4320 "Failure" and exit from any table.
BACKOFF_STEPS: tuple[tuple[int, float], ...] = ((2, 0.0), (5, 300.0), (10, 1800.0))
CIRCUIT_OPEN_AFTER = 10
CIRCUIT_COOLDOWN = 6 * 60 * 60.0


def delay_for_failures(failures: int) -> float:
    """How many seconds to wait? (pure ladder function; table in `tests/test_exporter_backoff.py`)."""
    if failures <= 0:
        return 0.0
    for upper, delay in BACKOFF_STEPS:
        if failures <= upper:
            return delay
    return CIRCUIT_COOLDOWN


@dataclass
class DeviceState:
    """The status of a `(ip, module)`.

    state is in *client*, not poller: poller calls `_exporter_client()` each cycle and
    There is no guarantee for the life of that object; The client is the only place where "consistency" makes sense.
    Only failed devices have a record (success ⇒ delete record), so memory with no
    Printers do not grow. `next_retry_at` is in live memory and jumps on reboot — intentional:
    Waking up the service should not block a dead printer for 6 hours.
    """

    consecutive_failures: int = 0
    next_retry_at: float = 0.0   # unix seconds; 0 = "You can try right now"


def parse_prometheus_text(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Converts exposition text to `{name: [(labels, value), …]}`.

    Rules (same as tested in `tests/test_exporter_client.py`):
      * `#` lines (HELP/TYPE/…) and empty lines are ignored;
      * `{name} {value}` and `{name}{labels…} {value}` are both accepted;
      * value can be int/float/scientific representation, `NaN`, `+Inf`/`-Inf`;
      * escaping inside the label (`\\"`, `\\\\`, `\\n`) is opened;
      * Repetitive series with the same name stay together (Advisory: the order of the exporter's response
        Not guaranteed; For our scalar metrics the last series is called —
        `latest_series()`).
    Parse error (shapeless string or non-numeric value) gives `ExporterError', not silent skip:
    Halfway data should not replace device data.
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
    """Split a metric line into its name part and its value part, honouring label braces."""
    for i, ch in enumerate(line):
        if ch in "{ \t":
            name = line[:i]
            if not name or not (name[0].isalpha() or name[0] == "_"):
                raise ExporterError(f"سطر {lineno}: نامِ متریک نامعتبر است: {line[:40]!r}")
            return name, line[i:]
    raise ExporterError(f"سطر {lineno}: مقدارِ متریک پیدا نشد: {line[:40]!r}")


def _parse_value(token: str, lineno: int, name: str) -> float:
    # The timeline of Prometheus leaves these three words free; We also accept and later
    # We consider an unlimited value as "invalid data" (finite_value).
    """Parse a metric value token into a float, accepting NaN and Inf forms."""
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
    """Breaks labels by separating them with "comma outside the quotation"."""
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
    """Return the labels and value of the most recent series for a metric name, or None."""

    series = metrics.get(name)
    return series[-1] if series else None


def metric_value(metrics: dict, name: str, default=None):
    """The scalar value of a metric, or `default'. ``NaN''/`Inf' is ``invalid data''."""
    series = latest_series(metrics, name)
    if series is None:
        return default
    value = series[1]
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def label_value(metrics: dict, name: str, default=None) -> str | None:
    """Metrics text `DisplayString`: the label value of the same metric name (collector.go).

    fallback: if a copy of the text is placed in another label, the first label other than `instance`/
    `module` is called — and `default` if there is no label.
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
    """Module query from exporter. It has no other dependencies on the program code."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT,
                 open_fn=None, clock=None):
        """Create the exporter client with its base URL, timeout and retry settings."""
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        # Opening the hook for testing (without global monkeypatch):
        self._open = open_fn or urllib.request.urlopen
        #: Injectable clock: The ladder works with "seconds" and the test should not sleep for 6 hours.
        self._clock = clock or time.time
        #: `(ip, module) → DeviceState` — only for devices with *at least one* failure.
        self._states: dict[tuple[str, str], DeviceState] = {}
        # poller polls each printer in its own thread (core/poller.py:402) ⇒ this dict is shared.
        self._states_lock = threading.Lock()

    # ── URL ────────────────────────────────────────────────────────────────
    def url_for(self, ip: str, module: str, auth: str = DEFAULT_AUTH) -> str:
        """Build the snmp_exporter scrape URL for one printer and SNMP module."""

        query = urllib.parse.urlencode({"target": ip, "module": module, "auth": auth})
        return f"{self.base_url}/snmp?{query}"

    # Reading
    def query(self, ip: str, module: str = "printer_std",
              auth: str = DEFAULT_AUTH) -> dict:
        """Reads a module and returns a dict of `{name: [(labels, value), …]}`.

        Any network/HTTP error or pars → `ExporterError` (the user must go to the path of
        return straight; We do not deliver half data).

        **Step H.2:** If `(ip, module)` is in the backoff interval, *no request* will be sent and
        `ExporterCircuitOpen` comes up. Any `ExporterError` (network, HTTP, or Pars) counters
        advances by one and deletes the record's healthy response altogether.
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
        """It reads and integrates the modules of that brand. Unknown brand → None.

        If all modules return empty, an empty dict is returned (not None); diagnosis
        "Is the data usable or not" with `exporter_adapter.exporter_to_collector_shape`
        is where it is decided to become None.

        **Step H.2:** If *any* of the modules are in backoff, `ExporterCircuitOpen` (before
        no request).
        """
        modules = MODULES_BY_BRAND.get((brand or "").strip().lower())
        if not modules:
            return None
        # H.2: A module in backoff ⇒ the whole brand is rejected. Reason: the form of collector with half of
        # The modules are not complete (HP = printer_hp + printer_std) and the snapshot is half missing
        # snapshot is worse — then the poller writes the direct path, which is complete.
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

    # ── backoff / circuit breaker (step H.2)
    def retry_in(self, ip: str, module: str) -> float:
        """How many seconds until the next attempt? 0 means "right now"."""
        with self._states_lock:
            st = self._states.get((ip, module))
            if st is None or st.next_retry_at <= 0:
                return 0.0
            return max(0.0, st.next_retry_at - self._clock())

    def consecutive_failures(self, ip: str, module: str) -> int:
        """Count of consecutive failures (0 if no record)."""
        with self._states_lock:
            st = self._states.get((ip, module))
            return st.consecutive_failures if st else 0

    def device_states(self) -> dict:
        """Read-only view of devices in backoff — for troubleshooting/testing (and future web).

        The key ``ip|module'' is a string, not a tuple, so that it can be displayed in JSON.
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
        """Clear state (manual/test). `ip=None` means all. Returns the cleared number."""
        with self._states_lock:
            keys = [k for k in self._states if ip is None or k[0] == ip]
            for k in keys:
                del self._states[k]
        return len(keys)

    def _note_failure(self, ip: str, module: str, exc: Exception | None = None) -> DeviceState:
        """A failure: +1 counter and `next_retry_at' from the ladder. Returns a snapshot."""
        key = (ip, module)
        with self._states_lock:
            st = self._states.setdefault(key, DeviceState())
            st.consecutive_failures += 1
            delay = delay_for_failures(st.consecutive_failures)
            st.next_retry_at = (self._clock() + delay) if delay > 0 else 0.0
            snap = DeviceState(st.consecutive_failures, st.next_retry_at)
        if snap.consecutive_failures == CIRCUIT_OPEN_AFTER + 1:
            # exactly once, at the moment of opening the circuit; After that silence (the poll log should not flood
            # be warned) and closing the circuit is also silent - it has new data in the main log path.
            log.warning("exporter: مدارِ %s/%s باز شد — تا %.1f ساعت هیچ کوئری‌ای نمی‌رود و "
                        "مسیرِ مستقیم می‌نویسد (شکستِ آخر: %s)",
                        ip, module, CIRCUIT_COOLDOWN / 3600.0, exc)
        return snap

    def _note_success(self, ip: str, module: str) -> None:
        """success ⇒ zero; The record is deleted so that `_states` is not enlarged."""
        with self._states_lock:
            self._states.pop((ip, module), None)

    # Availability
    def is_available(self) -> bool:
        """`GET /` with short timeout. Any error → False (no exception is raised).

        It intentionally does not check the HTTP response that claims "exporter is healthy"; He just says
        Something is listening where we expect it.
        """
        url = f"{self.base_url}/"
        try:
            with self._open(url, timeout=AVAILABILITY_TIMEOUT) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                return int(status) == 200
        except Exception as exc:  # timeout/DNS/URLError — anything
            log.debug("exporter unavailable at %s: %s", url, exc)
            return False


def client_from_env() -> ExporterClient:
    """Client built from the environment; No traffic is done at build time."""
    return ExporterClient(os.environ.get(ENV_URL, DEFAULT_BASE_URL))
