# -*- coding: utf-8 -*-
"""
Golden harness — زیرساخت مشترک «ضبط» و «بازپخش» fixtureهای طلایی.

این ماژول توسط دو مصرف‌کننده استفاده می‌شود:
  • ``tools/record_golden.py``  → ضبط fixtureها (حالت hermetic یا --with-simulator)
  • ``tests/test_golden.py``    → بازپخش hermetic و مقایسه‌ی تفاضل-صفر

قواعد قطعی‌بودن (determinism):
  • زمان قفل می‌شود (``time.time``، ``time.monotonic`` و **کلاس ``datetime``** در
    همه‌ی ماژول‌های دامنه) → هیچ مقداری به ساعت واقعی وابسته نیست.
    (کار E.0.3: کلاس وصله می‌شود، نه فقط ``now``؛ تا ``utcnow``/``today``/
    ``fromisoformat`` هم از همان مسیر قفل‌شده بگذرند.)
  • دیتابیس: یک SQLite موقت در هر اجرا (چون ``DB_PATH="logs.db"`` نسبی است،
    با ``os.chdir`` به پوشه‌ی موقت می‌رود) + ``init_db()`` + جداول yield.
  • SNMP: فقط ``core.snmp.protocol.snmp_get`` / ``snmp_getnext`` وصله می‌شوند.
  • HTTP: فقط لایه‌ی انتقال (``requests.get`` و ``urllib.request.urlopen``).

قواعد نرمال‌سازی خروجی (طبق تصمیم فاز C؛ تکمیل در E.0.3):
  • فیلدهای زمان → ``"<TS_LOCAL>"`` (naive، بدون offset) یا ``"<TS_UTC>"``
    (پس از فاز E.3 نویسنده‌ها UTC/Z می‌نویسند ⇒ همهٔ برچسب‌ها ``<TS_UTC>`` می‌شوند)
    (دارای ``Z``/offset). **مقدار** ساعت حذف می‌شود ولی **جنسِ** آن (naive/aware)
    عمداً حفظ می‌شود: فاز E می‌خواهد نویسنده‌ها را به aware-UTC ببرد و همین
    تفکیک باعث می‌شود آن تغییر، golden را صریح بشکند (و بی‌صدا از قلم نیفتد).
  • شناسه‌ی خودکار (``id``/``rowid``) → حذف
  • مسیر فایل → حذف/جایگزینی با ``"<PATH>"``
  • ردیف‌های DB → مرتب‌شده بر اساس کلید معنادار، حداکثر ۱۰ ردیف آخر هر جدول
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core.db import UndefinedColumn, UndefinedTable  # فاز E.4.4: نوع مشترک خطا

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"

# ─── زمان قفل‌شده (کار E.0.3) ──────────────────────────────────────────────
# لحظه‌ی ثابتِ همه‌ی fixtureها: ۲۰۲۶-۱۰-۰۳ ساعت ۱۲:۰۰:۰۰ **UTC** (aware).
# این لحظه یک «instant» واقعی است، نه یک ساعت محلی؛ پس:
#   • now(tz) / now(timezone.utc) → همان لحظه با پوشش منطقه‌زمانی خواسته‌شده
#   • now()  (بدون tz)            → نمایش *محلیِ* همان لحظه (naive) — مثل پایتون واقعی
#   • utcnow()                    → نمایش naive-UTC همان لحظه — مثل پایتون واقعی
# فاز E می‌خواهد writers را به aware-UTC ببرد؛ با این قفل، ثابت می‌شود که
# تغییر مبنا (محلی ⇒ UTC) رفتار دامنه را عوض نمی‌کند و فقط *نمایش* عوض می‌شود.
FROZEN_INSTANT = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_EPOCH = FROZEN_INSTANT.timestamp()
FROZEN_ISO = FROZEN_INSTANT.isoformat()               # «2026-10-03T12:00:00+00:00»

# «محلی» در این هارنس یعنی منطقه‌زمانیِ **قرارداد پروژه** (Asia/Tehran — بخش ۲ سند
# قرارداد زمان)، نه TZ ماشینی که تست روی آن اجرا می‌شود. چرا؟
#   • داده‌های موجود برنامه naive local‌اند و معنی «local» برایشان از پیش
#     Asia/Tehran است؛ قفل‌کردنِ همین معنا، هارنس را به قرارداد گره می‌زند.
#   • نتیجه: ضبط fixtureها روی هر ماشینی (CI با TZ=UTC یا سرور تهران) بیت‌به‌بیت
#     یکسان می‌شود و «drift بین CI و سرور» ممکن نیست.
# با متغیر محیطی ``PG_FROZEN_TZ`` می‌توان منطقه‌زمانیِ محلی را برای آزمایش عوض کرد.
def _local_tz():
    name = os.environ.get("PG_FROZEN_TZ", "Asia/Tehran")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                       # بدون tzdata/zoneinfo (ویندوز لخت)
        if name == "Asia/Tehran":
            from datetime import timedelta
            return timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")
        return timezone.utc


FROZEN_TZ = _local_tz()
FROZEN_NOW_LOCAL = (FROZEN_INSTANT.astimezone(FROZEN_TZ).replace(tzinfo=None))
FROZEN_NOW = FROZEN_INSTANT.replace(tzinfo=None)      # naive-UTC (سازگاری عقب‌رو)
# فاز E.3: خروجی استانداردِ «الآن» (فرمت پروژه: ۶ رقم میکروثانیه + Z).
FROZEN_UTC_Z = FROZEN_INSTANT.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _frozen_utc_now():
    """جانشین ``core.time_utils.utc_now_iso`` در زمان قفل‌شده."""
    return FROZEN_UTC_Z

DB_TABLES = ["logs", "printer_counters", "cartridge_state",
             "yield_samples", "toner_snapshots_v2"]

# کلید معنادار مرتب‌سازی هر جدول (طبق تصمیم فاز C)
ORDER_BY = {
    "logs": "rowid",
    "printer_counters": "rowid",
    "cartridge_state": "ip, color",
    "yield_samples": "ip, color",
    "toner_snapshots_v2": "ip, color",
}

# برچسب‌های زمان (E.0.3): جنس زمان حفظ می‌شود، مقدارش نه.
TS_LOCAL = "<TS_LOCAL>"      # naive local (بدون Z/offset)
TS_UTC = "<TS_UTC>"          # aware (Z یا offset)
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$")
_ISO_IN_TEXT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?")


def _ts_tag(match) -> str:
    """برچسب مناسب برای یک تطبیق زمانی: aware (Z/offset) یا naive."""
    return TS_UTC if match.group("tz") else TS_LOCAL
# TD-003 (فاز E.3): الگو نباید **وسط یک واژه** فعال شود. بدون این lookbehind،
# مقدار «N/A» (سنسور برای «نامعلوم») به «N<PATH>» تبدیل می‌شد و fixture دیگر
# عیناً خروجی واقعی را نشان نمی‌داد. مسیر واقعی («/tmp/x»، «C:\\x») یا با
# کاراکتر غیرالفبایی پیش از خودش می‌آید (فاصله، کوتیشن، `=`، `:`) یا در ابتدای رشته است.
_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(/[^\s'\"]+|[A-Za-z]:\\\\[^\s'\"]+)")


class _FrozenDatetime(datetime):
    """کلاس ``datetime`` قفل‌شده — رفتارش مثل کلاس واقعی است، فقط «حالا» ثابت.

    چرا کلاس و نه فقط ``now``؟ چون کد product ممکن است ``utcnow``، ``today``،
    ``fromtimestamp`` یا ``fromisoformat`` را هم صدا بزند؛ وصله‌کردن کلاس تضمین
    می‌کند هیچ‌کدام به ساعت واقعی نرسند:

      • ``now(tz)``        → همان ``FROZEN_INSTANT`` با همان منطقه‌زمانی
      • ``now()``          → نمایش محلیِ naive همان لحظه (مثل پایتون واقعی)
      • ``utcnow()``       → naive-UTC همان لحظه (مثل پایتون واقعی؛ deprecated)
      • ``today()``        → همان ``now()``
      • ``fromtimestamp``  → از لحظه‌ی داده‌شده (UTC)، سپس مثل واقعی ترجمه
      • ``fromisoformat``  → به ارث می‌رسد از ``datetime`` (تجزیه‌ی واقعی)
    """

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_NOW_LOCAL
        return FROZEN_INSTANT.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return FROZEN_INSTANT.replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def fromtimestamp(cls, t, tz=None):
        base = datetime.fromtimestamp(t, timezone.utc)
        if tz is not None:
            return base.astimezone(tz)
        return base.astimezone(FROZEN_TZ).replace(tzinfo=None)


class _ClockState:
    """شمارنده‌ی قطعی برای time.time()/monotonic()."""

    def __init__(self):
        self.n = 0

    def tick(self):
        self.n += 1
        return self.n


_CLOCK = _ClockState()


def _fake_time():
    return FROZEN_EPOCH + _CLOCK.tick() * 0.5


def _fake_monotonic():
    return 1000.0 + _CLOCK.tick() * 0.5


@contextlib.contextmanager
def frozen_time():
    """time.time/time.monotonic و datetime ماژول‌های دامنه را قفل می‌کند."""
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
    except Exception:  # pragma: no cover - ماژول اختیاری
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
        # ⚠ استثنای عمدی: کلاس ``datetime`` داخل خودِ ``core.time_utils`` وصله
        # نمی‌شود. ``to_utc_z`` با ``isinstance`` کار می‌کند و جانشین‌کردن کلاس با
        # زیرکلاس، نمونه‌های واقعیِ datetime را رد می‌کرد
        # («to_utc_z فقط datetime می‌پذیرد، نه datetime» — در ضبط E.3.6 دیده شد).
        # «الآن»ی این ماژول از راه وصلهٔ ``utc_now_iso`` قفل می‌شود (پایین).
        if m is time_utils:
            continue
        if getattr(m, "datetime", None) is datetime:
            patches.append(mock.patch.object(m, "datetime", _FrozenDatetime))
        # فاز E.3: «الآن» نویسنده‌ها از ``utc_now_iso`` می‌آید و هر ماژول آن را با
        # نام خودش import کرده (``from core.time_utils import utc_now_iso``)؛ پس
        # وصله باید روی همان ماژولِ مصرف‌کننده بنشیند، نه فقط روی core.time_utils.
        # بدون این، ساعتِ واقعی وارد fixture می‌شد و «ثابتِ» golden به لحظه‌ی ضبط
        # گره می‌خورد.
        if getattr(m, "utc_now_iso", None) is time_utils.utc_now_iso:
            patches.append(mock.patch.object(m, "utc_now_iso", _frozen_utc_now))
    # لایه‌ی داده هم نباید ساعت واقعی ببیند (sqlite3 از datetime ساخته می‌شود
    # و ممکن است برای «حالا» استفاده شود؛ وصله فقط اگر ماژول کلاس را import کرده باشد).
    if hasattr(_sqlite3, "datetime") and _sqlite3.datetime is datetime:
        patches.append(mock.patch.object(_sqlite3, "datetime", _FrozenDatetime))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


@contextlib.contextmanager
def isolated_db(tmpdir: str):
    """DB موقت + ریست کش‌های in-memory + جداول کامل (init_db + yield)."""
    import core.database as database
    from core import store
    from core.yield_engine import ensure_yield_tables

    old_cwd = os.getcwd()
    os.chdir(tmpdir)
    # ── گارد ایمنی: DB موقت باید داخل tmpdir باشد، هرگز logs.db خودِ مخزن ──
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
        # حالت جهانی دیگری که بین اجراها باقی می‌ماند و نتیجه را تغییر می‌دهد
        with store.data_lock:
            store.printer_data.clear()
        store.poll_stats.update({"count": 0, "last": None, "errors": 0, "is_polling": False})
        clear_module_caches()
        yield
    finally:
        store._prev._cache.clear()
        os.chdir(old_cwd)


# ─── دیتابیس: seed / snapshot / normalize ──────────────────────────────────
def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def capture_seed(tables=None):
    """وضعیت *خام* جدول‌ها (با id) برای بازسازی DB در بازپخش مستقل."""
    import core.database as database

    tables = tables or DB_TABLES
    seed = {}
    with database.db_connection() as conn:
        for t in tables:
            try:
                seed[t] = _rows(conn, f"SELECT * FROM {t}")
            except (UndefinedTable, UndefinedColumn):   # فاز E.4.4: نوع مشترک
                seed[t] = []
    return seed


def seed_db(seed):
    """DB تازه را با ردیف‌های خام (شامل id) پر می‌کند."""
    import core.database as database

    # ستون‌های lazy (مسیر واقعی production: هنگام load/save اضافه می‌شوند)
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
    """وضعیت *نرمال‌شده*ی جدول‌ها برای بخش ``db_state_after``."""
    import core.database as database

    tables = tables or DB_TABLES
    out = {}
    with database.db_connection() as conn:
        for t in tables:
            order = ORDER_BY.get(t, "rowid")
            try:
                rows = _rows(conn, f"SELECT * FROM {t} ORDER BY {order} DESC LIMIT {limit}")
            except (UndefinedTable, UndefinedColumn):   # فاز E.4.4: نوع مشترک
                out[t] = []
                continue
            rows.reverse()                      # مرتب‌سازی معنادار صعودی
            cleaned = []
            for r in rows:
                r.pop("id", None)
                r.pop("rowid", None)
                cleaned.append({k: normalize_value(v) for k, v in r.items()})
            out[t] = cleaned
    return out


# ─── نرمال‌سازی ────────────────────────────────────────────────────────────
def normalize_value(v):
    if isinstance(v, str):
        m = _TS_RE.match(v)
        if m:
            return _ts_tag(m)
        text = v
        # ستون‌های TEXT که JSON ذخیره می‌کنند (cartridge_id_state، toner_overrides، details):
        # ترتیب کلیدها بین «ساخت تازه» و «load→dump مجدد» فرق می‌کند ولی نرمال‌سازی
        # آن را قطعی می‌کند (محتوای معنایی یکسان ⇒ رشته‌ی یکسان).
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                text = json.dumps(json.loads(stripped), ensure_ascii=False, sort_keys=True)
            except Exception:
                text = v
        # نکته: الگو باید خودِ offset/`Z` را هم ببلعد، وگرنه آرتیفکت‌هایی مثل
        # «<TS_LOCAL>+00:00» تولید می‌شود (باگ کشف‌شده در آزمایش E.0.3).
        text = _ISO_IN_TEXT_RE.sub(_ts_tag, text)
        text = _PATH_RE.sub("<PATH>", text)
        return text
    if isinstance(v, dict):
        return {k: normalize_value(x) for k, x in v.items() if k not in ("id", "rowid")}
    if isinstance(v, (list, tuple)):
        return [normalize_value(x) for x in v]
    return v


def normalize_tree(obj):
    """نرمال‌سازی بازگشتی + حذف کلیدهای غیرقطعی."""
    if isinstance(obj, dict):
        return {k: normalize_tree(v) for k, v in sorted(obj.items())
                if k not in ("id", "rowid")}
    if isinstance(obj, list):
        return [normalize_tree(x) for x in obj]
    return normalize_value(obj)


# ─── HTTP جعلی (لایه‌ی انتقال) ──────────────────────────────────────────────
class FakeHttp:
    """پاسخ‌های HTTP قطعی بر اساس URL (regex → محتوا)."""

    def __init__(self, responses=(), default_status=404):
        self.responses = list(responses)
        self.default_status = default_status
        self.calls = []

    def _match(self, url):
        for pattern, body in self.responses:
            if re.search(pattern, url or ""):
                return body
        return None

    class _Resp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text
            self.content = (text or "").encode("utf-8")
            self.ok = 200 <= status < 300

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"HTTP {self.status_code}")

    def get(self, url, *a, **kw):
        self.calls.append(url)
        body = self._match(url)
        if body is None:
            return self._Resp(self.default_status, "")
        return self._Resp(200, body)

    @contextlib.contextmanager
    def installed(self):
        import urllib.request as _ur

        def _urlopen(req, *a, **kw):
            url = getattr(req, "full_url", req)
            self.calls.append(url)
            body = self._match(url) or ""
            return self._Resp(200 if body else self.default_status, body)

        with mock.patch("requests.get", side_effect=self.get), \
             mock.patch("requests.Session.get", side_effect=self.get), \
             mock.patch.object(_ur, "urlopen", side_effect=_urlopen):
            yield self


# ─── ضبط/بازپخش یک fixture ────────────────────────────────────────────────
def execute_steps(steps, transport=None, http=None):
    """گام‌ها را روی محیط ایزوله اجرا می‌کند و برای هر گام خروجی مشاهده‌شده می‌دهد.

    ``steps``: [{"call": callable} ...] — هر گام یک تابع بدون آرگومان که خروجی تابع
    تحت آزمون را برمی‌گرداند (یا None).
    """
    observed = []
    for st in steps:
        before = capture_seed() if st.get("capture_before") else None
        if before:
            seed_db(before)
        if transport is not None:
            transport.calls.clear()
        ret = st["call"]()
        observed.append({
            "return_value": normalize_tree(ret),
            "expected_events": normalize_tree(st["events_fn"]()),
            "new_prev": normalize_tree(st["prev_fn"]()),
            "db_state_after": snapshot_db(),
            "snmp_call_sequence": (transport.call_sequence() if transport else []),
        })
    return observed


@contextlib.contextmanager
def event_recorder():
    """هر ``add_event`` را ثبت می‌کند و *سپس* نسخه‌ی واقعی را صدا می‌زند.

    نکته: این وصله در لایه‌ی ذخیره‌سازی است (نه منطق دامنه)؛ ``_counters_event``
    و موتورهای دامنه دست‌نخورده اجرا می‌شوند و ردیف‌های واقعی در DB نوشته می‌شود.
    """
    import core.database as database

    orig = database.add_event
    recorded = []

    def wrapper(ip, etype, details=None):
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
    """بازپخش hermetic یک fixture از نوع counters_event (مستقل از سایر گام‌ها)."""
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
    from core import store
    return store._prev.get(ip)


def replay_enhanced(fixture):
    """بازپخش hermetic یک fixture از نوع collect_enhanced (همه‌ی فازها، یک DB)."""
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
    """اولین تفاوت دو ساختار JSON-مانند (برای گزارش دقیق عدم‌تطابق)."""
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
    """کش‌های in-memory ماژول‌ها را پاک می‌کند تا هر سناریو/بازپخش از حالت تمیز شروع شود."""
    try:
        from core.collectors import cartridge_discovery
        cartridge_discovery._stability.clear()
        cartridge_discovery._last_attempt.clear()
        # صف/worker کشف کارتریج: thread پس‌زمینه در حین ضبط، walkهای SNMP اضافه و
        # *غیرقطعی* تولید می‌کند (۵ فراخوانی اضافه در اندازه‌گیری واقعی همین نشست).
        # با فعال‌گذاشتن پرچم، thread هرگز شروع نمی‌شود ⇒ ضبط/بازپخش قطعی می‌ماند.
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
    """تبدیل هر شیء به شکل JSON-پذیر قطعی (برای مقایسه‌ی تفاضل-صفر)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str))


def write_fixture(name, payload):
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def load_fixture(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
