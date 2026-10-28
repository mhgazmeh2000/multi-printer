# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/golden_harness.py
# @brief:        Shared infrastructure for recording and replaying golden fixtures.
# @details:      Responsibilities: Freeze time, seed the database and snapshot results for
#                comparison.
#                Main API: _FrozenDatetime, _ClockState, FakeHttp
#                Side effects: database access, network I/O, file I/O
# @dependencies: internal: config.settings, core, core.collectors, core.database, core.db,
#                core.enhanced_collector, core.yield_engine, tests.fake_snmp,
#                tests.golden_profiles, tests.golden_scenarios
# @notes:        Reads environment variables: PG_FROZEN_TZ.
# =============================================================================

"""
Golden harness — shared infrastructure for "recording" and "playback" of golden fixtures.

This module is used by two consumers:
  • ``tools/record_golden.py'' → record fixtures (hermetic mode or --with-simulator)
  • ``tests/test_golden.py'' → hermetic replay and zero-difference comparison

Determinism rules:
  • Time locks (``time.time'', ``time.monotonic'' and **class ``datetime''** in
    all domain modules) → no value depends on actual clock.
    (E.0.3 work: class gets patched, not just ``now''; to ``utcnow''/``today''/
    ``fromisoformat'' also pass through the same locked path.)
  • Database: a temporary SQLite per run (because ``DB_PATH="logs.db"'' is relative,
    goes to temporary folder with ``os.chdir'') + ``init_db()'' + yield tables.
  • SNMP: Only ``core.snmp.protocol.snmp_get'' / ``snmp_getnext'' are patched.
  • HTTP: transport layer only (``requests.get'' and ``urllib.request.urlopen'').

Output normalization rules (according to phase C decision; completed in E.0.3):
  • Time fields → ``"<TS_LOCAL>"`` (naive, without offset) or ``"<TS_UTC>"``
    (After phase E.3 the authors write UTC/Z ⇒ all tags become ``<TS_UTC>'')
    (with ``Z''/offset). The **value** of the clock is removed, but its **kind** (naive/aware)
    Deliberately maintained: Phase E wants to make authors UTC-aware and that's it
    Dissociation makes that change explicitly break golden (and not be silently missed).
  • Automatic ID (``id``/``rowid``) → delete
  • File path → remove/replace with ``"<PATH>"``
  • DB rows → sorted by meaningful key, maximum last 10 rows of each table
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core.db import UndefinedColumn, UndefinedTable  # Phase E.4.4: Common type of error

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"

# ─── Locked Time (Work E.0.3) ─────────────────────────────────────────────
# Fixed moment of all fixtures: 2026-10-03 at 12:00:00 **UTC** (aware).
# This moment is a real instant, not a local time; So:
# • now(tz) / now(timezone.utc) → the same moment covering the requested time zone
# • now() (without tz) → display *local* at the same moment (naive) — like real Python
# • utcnow() → Display naive-UTC instantly — like real Python
# Phase E wants to take writers to aware-UTC; With this lock, it is proven that
# Changing the datum (local ⇒ UTC) does not change the behavior of the domain, only the *display* changes.
FROZEN_INSTANT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_EPOCH = FROZEN_INSTANT.timestamp()
FROZEN_ISO = FROZEN_INSTANT.isoformat()               # «2026-10-03T12:00:00+00:00»

# "Local" in this harness means the **project contract** time zone (Asia/Tehran - Section 2 of the document
# time contract), not the TZ of the machine on which the test is run. why
# • The existing data of the naive program are local and the meaning of "local" is already there for them
# Asia/Tehran is; Locking the same meaning ties the harness to the contract.
# • Result: recording fixtures on any machine (CI with TZ=UTC or Tehran server) bit by bit
# It becomes the same and "drift between CI and server" is not possible.
# The local time zone for testing can be changed with the ``PG_FROZEN_TZ'' environment variable.
def _local_tz():
    """Helper: local tz."""
    name = os.environ.get("PG_FROZEN_TZ", "Asia/Tehran")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                       # no tzdata/zoneinfo (bare windows)
        if name == "Asia/Tehran":
            from datetime import timedelta
            return timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")
        return timezone.utc


FROZEN_TZ = _local_tz()
FROZEN_NOW_LOCAL = (FROZEN_INSTANT.astimezone(FROZEN_TZ).replace(tzinfo=None))
# Phase E.3: "Now" standard output (project format: 6 microsecond digits + Z).
FROZEN_UTC_Z = FROZEN_INSTANT.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _frozen_utc_now():
    """Substitute for ``core.time_utils.utc_now_iso'' in time locked."""
    return FROZEN_UTC_Z

DB_TABLES = ["logs", "printer_counters", "cartridge_state",
             "yield_samples", "toner_snapshots_v2"]

# The meaningful key to sort each table (according to phase C decision)
ORDER_BY = {
    "logs": "rowid",
    "printer_counters": "rowid",
    "cartridge_state": "ip, color",
    "yield_samples": "ip, color",
    "toner_snapshots_v2": "ip, color",
}

# Time tags (E.0.3): The type of time is preserved, not its value.
TS_LOCAL = "<TS_LOCAL>"      # naive local (without Z/offset)
TS_UTC = "<TS_UTC>"          # aware (Z or offset)
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$")
_ISO_IN_TEXT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?")


def _ts_tag(match) -> str:
    """Appropriate label for a timing match: aware (Z/offset) or naive."""
    return TS_UTC if match.group("tz") else TS_LOCAL
# TD-003 (Phase E.3): Pattern should not be activated **in the middle of a word**. Without this lookbehind,
# The value "N/A" (sensor for "unknown") would be converted to "N<PATH>" and another fixture
# It did not show the actual output. actual path ('/tmp/x', 'C:\\x') or with
# A non-alphabetic character comes before itself (space, quote, `=`, `:`) or is at the beginning of the string.
_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(/[^\s'\"]+|[A-Za-z]:\\\\[^\s'\"]+)")


class _FrozenDatetime(datetime):
    """Locked ``datetime'' class — behaves like a real class, only fixed ``now''.

    Why class and not just ``now''? Because the product code may be ``utcnow'', ``today'',
    Also call ``fromtimestamp'' or ``fromisoformat''; Patching the assurance class
    makes none of them reach the real time:

      • ``now(tz)'' → same as ``FROZEN_INSTANT'' with same time zone
      • ``now()'' → display the local naive at that moment (like real Python)
      • ``utcnow()'' → naive-UTC right now (like real Python; deprecated)
      • ``today()'' → the same as ``now()''
      • ``fromtimestamp'' → from the given moment (UTC), then the actual translation
      • ``fromisoformat'' → inherits from ``datetime'' (the actual parser)
    """

    @classmethod
    def now(cls, tz=None):
        """Helper: now."""
        if tz is None:
            return FROZEN_NOW_LOCAL
        return FROZEN_INSTANT.astimezone(tz)

    @classmethod
    def utcnow(cls):
        """Helper: utcnow."""
        return FROZEN_INSTANT.replace(tzinfo=None)

    @classmethod
    def today(cls):
        """Helper: today."""
        return cls.now()

    @classmethod
    def fromtimestamp(cls, t, tz=None):
        """Helper: fromtimestamp."""
        base = datetime.fromtimestamp(t, timezone.utc)
        if tz is not None:
            return base.astimezone(tz)
        return base.astimezone(FROZEN_TZ).replace(tzinfo=None)


class _ClockState:
    """Deterministic counter for time.time()/monotonic()."""

    def __init__(self):
        """Initialize the instance."""
        self.n = 0

    def tick(self):
        """Helper: tick."""
        self.n += 1
        return self.n


_CLOCK = _ClockState()


def _fake_time():
    """Helper: fake time."""
    return FROZEN_EPOCH + _CLOCK.tick() * 0.5


def _fake_monotonic():
    """Helper: fake monotonic."""
    return 1000.0 + _CLOCK.tick() * 0.5


@contextlib.contextmanager
def frozen_time():
    """The time.time/time.monotonic and datetime modules lock the domain."""
    import config.settings as settings
    from core import database, store
    from core import yield_engine, cartridge_change_engine
    from core import time_utils
    from core.collectors import base as base_mod

    mods = [base_mod, database, store, yield_engine, cartridge_change_engine, time_utils]
    try:
        from core.collectors import (hp, canon, brother, toshiba, sensor,
                                    cartridge_id, cartridge_discovery)
        mods += [hp, canon, brother, toshiba, sensor, cartridge_id, cartridge_discovery]
    except Exception:  # pragma: no cover - optional module
        pass
    try:
        import core.enhanced_collector as ec
        mods.append(ec)
    except Exception:  # pragma: no cover
        pass
    mods.append(settings)

    import sqlite3 as _sqlite3

    _CLOCK.n = 0
    patches = [mock.patch("time.time", side_effect=_fake_time),
               mock.patch("time.monotonic", side_effect=_fake_monotonic)]
    for m in mods:
        # ⚠ Intentional exception: ``datetime'' class inside ``core.time_utils'' patch itself
        # can't ``to_utc_z'' works with ``isinstance'' and substituting the class with
        # The subclass rejects actual datetime instances
        # ("to_utc_z only accepts datetime, not datetime" — seen in recording E.3.6).
        # The "now" of this module is locked by the ``utc_now_iso'' patch (below).
        if m is time_utils:
            continue
        if getattr(m, "datetime", None) is datetime:
            patches.append(mock.patch.object(m, "datetime", _FrozenDatetime))
        # Phase E.3: Authors' "now" comes from ``utc_now_iso'' and each module uses it with
        # imported its own name (``from core.time_utils import utc_now_iso''); so
        # The patch should sit on the same consumer module, not just core.time_utils.
        # Without this, the actual clock would be entered into the fixture and the golden "fixed" to the recording moment
        # It gets tied.
        if getattr(m, "utc_now_iso", None) is time_utils.utc_now_iso:
            patches.append(mock.patch.object(m, "utc_now_iso", _frozen_utc_now))
    # The data layer should not see the real time either (sqlite3 is made from datetime
    # and may be used for "now"; patch only if the module has imported the class).
    if hasattr(_sqlite3, "datetime") and _sqlite3.datetime is datetime:
        patches.append(mock.patch.object(_sqlite3, "datetime", _FrozenDatetime))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


@contextlib.contextmanager
def isolated_db(tmpdir: str):
    """Temporary DB + in-memory cache resets + complete tables (init_db + yield)."""
    import core.database as database
    from core import store
    from core.yield_engine import ensure_yield_tables

    old_cwd = os.getcwd()
    os.chdir(tmpdir)
    # ── Safety guard: temp db should be in tmpdir, never logs.db of repository itself ──
    db_path = os.path.abspath(getattr(database, "DB_PATH", "logs.db"))
    root = os.path.abspath(tmpdir)
    if os.path.commonpath([db_path, root]) != root:
        os.chdir(old_cwd)
        raise RuntimeError(
            f"isolated_db refused to run: DB_PATH={db_path} is outside temp dir {root}")
    try:
        database.init_db()
        ensure_yield_tables()
        store._prev._cache.clear()
        with store.printers_lock:
            store.PRINTERS[:] = []
        # Another global state that persists between runs and changes the result
        with store.data_lock:
            store.printer_data.clear()
        store.poll_stats.update({"count": 0, "last": None, "errors": 0, "is_polling": False})
        clear_module_caches()
        yield
    finally:
        store._prev._cache.clear()
        os.chdir(old_cwd)


# ─── Database: seed / snapshot / normalize
def _rows(conn, sql, params=()):
    """Helper: rows."""
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def capture_seed(tables=None):
    """*Raw* status of tables (with id) for DB reconstruction in stand-alone replay."""
    import core.database as database

    tables = tables or DB_TABLES
    seed = {}
    with database.db_connection() as conn:
        for t in tables:
            try:
                seed[t] = _rows(conn, f"SELECT * FROM {t}")
            except (UndefinedTable, UndefinedColumn):   # Phase E.4.4: Common type
                seed[t] = []
    return seed


def seed_db(seed):
    """Fills the new DB with raw rows (including id)."""
    import core.database as database

    # Lazy columns (real production path: added when load/save)
    try:
        database.ensure_printer_counters_columns()
    except Exception:  # pragma: no cover
        pass

    with database.db_connection(commit=True) as conn:
        for t, rows in (seed or {}).items():
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ",".join("?" for _ in cols)
            collist = ",".join(cols)
            for r in rows:
                conn.execute(
                    f"INSERT OR REPLACE INTO {t} ({collist}) VALUES ({placeholders})",
                    [r.get(c) for c in cols])


def snapshot_db(tables=None, limit=10):
    """The *normalized* state of the tables for the ``db_state_after'' section."""
    import core.database as database

    tables = tables or DB_TABLES
    out = {}
    with database.db_connection() as conn:
        for t in tables:
            order = ORDER_BY.get(t, "rowid")
            try:
                rows = _rows(conn, f"SELECT * FROM {t} ORDER BY {order} DESC LIMIT {limit}")
            except (UndefinedTable, UndefinedColumn):   # Phase E.4.4: Common type
                out[t] = []
                continue
            rows.reverse()                      # Ascending significant order
            cleaned = []
            for r in rows:
                r.pop("id", None)
                r.pop("rowid", None)
                cleaned.append({k: normalize_value(v) for k, v in r.items()})
            out[t] = cleaned
    return out


# - Normalization - Normalization
def normalize_value(v):
    """Helper: normalize value."""
    if isinstance(v, str):
        m = _TS_RE.match(v)
        if m:
            return _ts_tag(m)
        text = v
        # TEXT columns that store JSON (cartridge_id_state, toner_overrides, details):
        # The order of the keys is different between "build new" and "load→dump again", but normalization
        # makes it definite (same semantic content ⇒ same string).
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                text = json.dumps(json.loads(stripped), ensure_ascii=False, sort_keys=True)
            except Exception:
                text = v
        # Note: The template must also swallow the offset/`Z' itself, otherwise artifacts like
        # "<TS_LOCAL>+00:00" is generated (bug discovered in test E.0.3).
        text = _ISO_IN_TEXT_RE.sub(_ts_tag, text)
        text = _PATH_RE.sub("<PATH>", text)
        return text
    if isinstance(v, dict):
        return {k: normalize_value(x) for k, x in v.items() if k not in ("id", "rowid")}
    if isinstance(v, (list, tuple)):
        return [normalize_value(x) for x in v]
    return v


def normalize_tree(obj):
    """Recursive normalization + removal of indeterminate keys."""
    if isinstance(obj, dict):
        return {k: normalize_tree(v) for k, v in sorted(obj.items())
                if k not in ("id", "rowid")}
    if isinstance(obj, list):
        return [normalize_tree(x) for x in obj]
    return normalize_value(obj)


# Fake HTTP (Transport Layer)
class FakeHttp:
    """Deterministic HTTP responses based on URL (regex → content)."""

    def __init__(self, responses=(), default_status=404):
        """Initialize the instance."""
        self.responses = list(responses)
        self.default_status = default_status
        self.calls = []

    def _match(self, url):
        """Helper: match."""
        for pattern, body in self.responses:
            if re.search(pattern, url or ""):
                return body
        return None

    class _Resp:
        """Helper class: resp."""
        def __init__(self, status, text):
            """Initialize the instance."""
            self.status_code = status
            self.text = text
            self.content = (text or "").encode("utf-8")
            self.ok = 200 <= status < 300

        def raise_for_status(self):
            """Helper: raise for status."""
            if not self.ok:
                raise RuntimeError(f"HTTP {self.status_code}")

    def get(self, url, *a, **kw):
        """Helper: get."""
        self.calls.append(url)
        body = self._match(url)
        if body is None:
            return self._Resp(self.default_status, "")
        return self._Resp(200, body)

    @contextlib.contextmanager
    def installed(self):
        """Helper: installed."""
        import urllib.request as _ur

        def _urlopen(req, *a, **kw):
            """Helper: urlopen."""
            url = getattr(req, "full_url", req)
            self.calls.append(url)
            body = self._match(url) or ""
            return self._Resp(200 if body else self.default_status, body)

        with mock.patch("requests.get", side_effect=self.get), \
             mock.patch("requests.Session.get", side_effect=self.get), \
             mock.patch.object(_ur, "urlopen", side_effect=_urlopen):
            yield self


# ─── recording/playback of a fixture
@contextlib.contextmanager
def event_recorder():
    """It logs every ``add_event'' and *then* calls the actual version.

    Note: This patch is in the storage layer (not domain logic); ``_counters_event''
    And the domain engines run intact and the actual rows are written to the DB.
    """
    import core.database as database

    orig = database.add_event
    recorded = []

    def wrapper(ip, etype, details=None):
        """Helper: wrapper."""
        recorded.append({
            "ip": ip,
            "type": etype,
            "details": normalize_tree(dict(details or {})),
        })
        return orig(ip, etype, details)

    targets = []
    for name in ("core.database", "core.collectors.base", "core.collectors.toshiba",
                 "core.collectors.canon", "core.collectors.hp", "core.collectors.brother",
                 "core.collectors.sensor", "core.enhanced_collector",
                 "core.yield_engine", "core.cartridge_change_engine", "core.sensor_alert"):
        try:
            mod = __import__(name, fromlist=["add_event"])
        except Exception:  # pragma: no cover
            continue
        if getattr(mod, "add_event", None) is not None:
            targets.append(mod)

    with contextlib.ExitStack() as stack:
        for m in targets:
            stack.enter_context(mock.patch.object(m, "add_event", wrapper))
        yield recorded


def replay_counters(fixture):
    """Hermetic playback of a fixture of type counters_event (independent of other steps)."""
    from tests.golden_scenarios import build_counters_call

    ip = fixture["input"]["ip"]
    with event_recorder() as rec:
        ret = build_counters_call(dict(fixture["input"]["args"]))()
    return {
        "return_value": normalize_tree(ret),
        "expected_events": normalize_tree(rec),
        "new_prev": normalize_tree(store_get_prev(ip)),
        "db_state_after": snapshot_db(),
        "snmp_call_sequence": [],
    }


def store_get_prev(ip):
    """Helper: store get prev."""
    from core import store
    return store._prev.get(ip)


def replay_enhanced(fixture):
    """Hermetic playback of a collect_enhanced fixture (all phases, one DB)."""
    import copy

    import core.enhanced_collector as ec
    from tests.golden_profiles import build_transport

    transport = build_transport(fixture)
    http = FakeHttp([tuple(x) for x in fixture["input"]["http_responses"]])
    steps = []
    with frozen_time(), transport.installed() as t, http.installed() as h:
        for ph, _exp in zip(fixture["input"]["phases"], fixture["expected"]["steps"]):
            t.devices = copy.deepcopy(ph.get("devices") or {})
            clear_module_caches()
            t.calls.clear()
            h.calls.clear()
            ret = ec.collect_enhanced(dict(fixture["input"]["printer"]), save_to_db=True)
            steps.append({
                "phase": ph["name"],
                "return_value": json_safe(normalize_tree(ret)),
                "db_state_after": snapshot_db(),
                "snmp_call_sequence": t.call_sequence(),
                "http_calls": list(h.calls),
            })
    return json_safe({"steps": steps})


def first_diff(a, b, path="$"):
    """The first difference is the two JSON-like structures (to accurately report mismatches)."""
    if type(a) is not type(b):
        return f"{path}: type {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                return f"{path}.{k}: missing in observed"
            if k not in b:
                return f"{path}.{k}: extra in observed"
            d = first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


def clear_module_caches():
    """Clears in-memory caches of modules so that each scenario/replay starts clean."""
    try:
        from core.collectors import cartridge_discovery
        cartridge_discovery._stability.clear()
        cartridge_discovery._last_attempt.clear()
        # Cartridge discovery queue/worker: background thread during recording, additional SNMP walks and more
        # produces *inconclusive* (5 extra calls in the actual measurement of this session).
        # With the flag enabled, the thread will never start ⇒ recording/playback will stop.
        cartridge_discovery._queue.clear()
        cartridge_discovery._queue_set.clear()
        cartridge_discovery._worker_started = True
    except Exception:  # pragma: no cover
        pass
    try:
        from core.collectors import cartridge_id
        cartridge_id._cache.clear()
        cartridge_id._config_cache.update({"ts": 0.0, "data": None})
    except Exception:  # pragma: no cover
        pass
    try:
        from core.collectors import toshiba
        toshiba._toner_scrape_cache.clear()
    except Exception:  # pragma: no cover
        pass
    try:
        from tests.fake_snmp import clear_version_cache
        clear_version_cache()
    except Exception:  # pragma: no cover
        pass


def json_safe(obj):
    """Convert each object to a definitive JSON-acceptable form (for zero-difference comparisons)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str))


def write_fixture(name, payload):
    """Helper: write fixture."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path
