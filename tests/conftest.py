# -*- coding: utf-8 -*-
"""
tests/conftest.py — ایزولاسیون PostgreSQL برای سوئیت تست (فاز E.5).

چرا این فایل هست
----------------
پیش از این، هر تستی که با ``PG_DSN`` به پستگرس وصل می‌شد روی **یک اسکیمای مشترک**
(``public``) کار می‌کرد. سه پیامد عملی داشت (سنجش آغازین E.5، ``DB_BACKEND=postgresql``):

  1. ``relation "t" already exists`` — چند تست جدول موقت هم‌نام در ``public``
     می‌ساختند؛ اجرای دوم به اجرای اول برخورد می‌کرد.
  2. دادهٔ باقیمانده از اجراهای قبیـل نتیجه‌ها را عوض می‌کرد (باید دستی
     ``TRUNCATE`` می‌شد تا وضعیت تمیز شود).
  3. دو اجرای موازی (مثلاً ``pytest -n`` یا دو چک‌اوت در یک ماشین) یکدیگر را
     خراب می‌کردند.

راه‌حل: یک **اسکیمای اختصاصی برای هر اجرای pytest**. با ``CREATE SCHEMA`` (نه
``CREATE DATABASE`` — ساخت دیتابیس به حق ``CREATEDB`` نیاز دارد و آهسته‌تر است)
و قفل ``search_path`` روی همان اسکیمای خالی، سه مشکل بالا توأمان رفع می‌شوند.

قاعدهٔ این فایل: «هیچ تستی نباید ``public`` را hardcode کند.» چون همه‌چیز از راه
``PG_DSN`` (که در ادامهٔ همین فایل بازنویسی می‌شود) وصل می‌شوند، کدهای موجود بدون
تغییر به اسکیمای ایزوله می‌روند. تست‌های ماژول‌سطحی که ``PG_DSN`` را در زمان import
می‌خوانند هم درست کار می‌کنند، چون این بازنویسی **در سطح ماژول conftest** انجام
می‌شود و conftest پیش از هر ماژول تستی import می‌گردد.

منطق skip (بی‌صدا، طبق تصمیم مالک)
----------------------------------
  • ``PG_DSN`` ست نشده ⇒ هیچ کاری؛ تست‌های SQLite دست‌نخورده اجرا می‌شوند.
  • سرور پستگرس در دسترس نیست ⇒ هیچ کاری (تست‌های PG خودشان skip می‌کنند؛ ما
    خطای ساختگی نمی‌سازیم).
  • نقش، حق ``CREATE`` روی دیتابیس ندارد (نقش read-only) ⇒ skip با هشدار.
  • ``PG_DSN`` هست و اسکیم ساخته شد ولی ``alembic upgrade head`` شکست ⇒
    **خطای صریح**؛ هیچ بازگشت بی‌صدایی به ``public`` انجام نمی‌شود (قاعدهٔ
    «fallback بی‌صدا وجود ندارد» در ``alembic/env.py``).

خاموش‌کردن موقت (برای عیب‌یابی): ``PRINTGUARD_PG_ISOLATE=0``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
import warnings
from pathlib import Path

import pytest

ENV_PG_DSN = "PG_DSN"
ENV_DB_BACKEND = "DB_BACKEND"
ENV_SWITCH = "PRINTGUARD_PG_ISOLATE"
#: پیشوند نام اسکیم — برای اینکه با چشم در ``pg_stat_activity`` پیدا باشد.
SCHEMA_PREFIX = "printguard_test"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: وضعیت این اجرا (dsn اصلی + اسکیم ساخته‌شده).
_state = {"dsn": None, "schema": None}

#: اگر بازنشانی داده یک‌بار بشکند، پیامش اینجا می‌ماند و از آن پس خاموش می‌شود.
_reset_broken = None


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    """
    ``options=-csearch_path=<schema>`` به DSN اضافه می‌کند (بدون فاصله).
    پشتیبانی کامل از هر دو ساختار: URI (`postgresql://...`) و conninfo (`host=...`).
    """
    try:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        params = conninfo_to_dict(dsn)
        existing = params.get("options", "")
        opts = f"-csearch_path={schema}"
        if existing:
            opts = f"{existing} {opts}"
        params["options"] = opts
        return make_conninfo(**params)
    except Exception:
        from core.db.connection import _conninfo_value

        existing = _conninfo_value(dsn, "options")
        opts = f"-csearch_path={schema}"
        if existing:
            opts = f"{existing} {opts}"
        tokens = [t for t in dsn.split() if not t.lower().startswith("options=")]
        tokens.append(f"options={opts}")
        return " ".join(tokens)


def _maintenance_connect(dsn: str):
    """اتصال autocommit برای کارهای مدیریتی (CREATE/DROP SCHEMA)."""
    import psycopg

    return psycopg.connect(dsn, autocommit=True, connect_timeout=5)


def _isolate() -> None:
    """اسکیمای ایزوله می‌سازد، DSN را بازنویسی می‌کند و migration را رویش می‌زند."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path=env_file, override=False)
        except ImportError:
            pass

    dsn = (os.environ.get(ENV_PG_DSN, "") or "").strip()
    if not dsn or os.environ.get(ENV_SWITCH, "1").strip() == "0":
        return                                   # حالت عادی: فقط SQLite
    from importlib.util import find_spec
    if find_spec("psycopg") is None:
        return                                   # بدون درایور ⇒ تست‌های PG خودشان skip می‌کنند

    schema = f"{SCHEMA_PREFIX}_{uuid.uuid4().hex[:12]}"
    _state["dsn"] = dsn
    try:
        with _maintenance_connect(dsn) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
    except Exception as exc:                       # noqa: BLE001 — هر دلیلی (از جمله حق ناکافی)
        sqlstate = getattr(getattr(exc, "sqlstate", None), "value", None) or getattr(
            exc, "sqlstate", None)
        _state["dsn"] = None
        if sqlstate in ("3D000", "08001", "08006", "28P01"):
            return                                 # سرور/احراز هویت ⇒ تست‌ها خودشان skip می‌کنند
        warnings.warn(
            f"ایزولاسیون PG خاموش ماند: CREATE SCHEMA مجاز نبود "
            f"({type(exc).__name__}: {str(exc).strip().splitlines()[0][:160]}). "
            f"برای ایزوله‌شدن، به نقش اجرا «GRANT CREATE ON DATABASE …» بدهید.",
            stacklevel=2)
        return

    _state["schema"] = schema
    os.environ[ENV_PG_DSN] = _dsn_with_search_path(dsn, schema)
    _run_alembic_upgrade(dsn, schema)


def _run_alembic_upgrade(dsn: str, schema: str) -> None:
    """
    ``alembic upgrade head`` را **روی اسکیمای ایزوله** اجرا می‌کند.

    چرا subprocess: alembic در سطح ماژول logging و sys.path دست می‌برد و اجرای
    in-process آن در میانهٔ یک جلسهٔ pytest می‌تواند state جهانی را عوض کند.
    هزینه ~۱ ثانیه است و یک‌بار در هر اجرا پرداخت می‌شود.
    """
    env = os.environ.copy()
    env[ENV_DB_BACKEND] = "postgresql"
    env[ENV_PG_DSN] = _dsn_with_search_path(dsn, schema)
    env.pop("ALEMBIC_DB_URL", None)                # override نخوانده‌گرفته نگذاریم
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        # بی‌صدا نمی‌بلعیم: نیمی از اسکیم ساخته‌شده خالی بماند یعنی صدها شکست
        # بی‌ربط در تست‌ها.
        raise RuntimeError(
            "ایزولاسیون PG: alembic upgrade head روی اسکیمای تازه شکست.\n"
            f"schema = {schema}\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}\n"
            f"--- stderr ---\n{proc.stderr[-2000:]}")


def _cleanup() -> None:
    schema, dsn = _state.get("schema"), _state.get("dsn")
    if not schema or not dsn:
        return
    try:
        with _maintenance_connect(dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    except Exception as exc:                       # noqa: BLE001 — teardown نباید تست را بشکند
        warnings.warn(f"پاک‌سازی اسکیمای تست نشد ({schema}): {type(exc).__name__}: {exc}",
                      stacklevel=2)
    finally:
        _state["schema"] = None


_isolate()


#: جدول‌های برنامه که پیش از هر تست خالی می‌شوند (``core.db.schema.APP_TABLES``).
_APP_TABLES = ("logs", "printer_counters", "toner_history", "sensor_readings", "users",
               "security_events", "yield_samples", "toner_snapshots_v2", "cartridge_state",
               "cartridge_yield_profiles", "meta")


def _app_backend_is_postgres() -> bool:
    """
    آیا خودِ برنامه روی PG اجرا می‌شود؟

    بازنشانی داده فقط در آن حالت لازم است؛ اگر ``DB_BACKEND`` خالی/sqlite باشد،
    تست‌ها فایل‌های موقت خودشان را دارند و دست‌زدن به PGِ مشترک هم بی‌فایده‌ست و
    هم اجرای SQLite را کند می‌کند (یک TRUNCATE به‌ازای هر تست).
    """
    from core.db import BACKEND_POSTGRESQL, resolve_backend

    try:
        return resolve_backend() == BACKEND_POSTGRESQL
    except Exception:                            # noqa: BLE001 - تنظیمات ناشناخته ⇒ دست نزن
        return False


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _reset_state() -> None:
    """
    اسکیمای ایزوله را به حالت «تازه» برمی‌گرداند: جدول‌های برنامه خالی،
    sequenceها ریست، و جدول‌های دست‌سازِ تست‌ها حذف.

    چرا لازم است: «هر تست یک فایل SQLite موقت» فرضِ پیش‌فرضِ صدها تست است. روی PG که
    همهٔ تست‌ها یک اسکیمای مشترکِ جلسه‌ای را می‌بینند، این فرض می‌شکند؛ نمونهٔ
    سنجیده‌شده: ``duplicate key value violates unique constraint
    "users_username_key"`` (admin1 که فایل قبلی ساخته) ⇒ ``'viewer' != 'admin'``،
    و ``'t' already exists`` (جدول موقت هم‌نام). بازنشانی داده در **پیش از هر تست**
    همان «فایل موقت تازه» را بازتولید می‌کند، بدون آنکه یک خط در فایل‌های تست
    عوض شود و بدون آنکه migration دوباره اجرا شود.
    """
    from core.db import connect_postgres

    conn = connect_postgres()
    try:
        try:
            conn.execute("SET LOCAL lock_timeout = '5s'")
        except Exception:                       # noqa: BLE001 — بیرون از تراکنش بی‌معناست
            pass
        found = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()}
        present_app = [t for t in _APP_TABLES if t in found]
        foreign = sorted(found - set(_APP_TABLES) - {"alembic_version"})
        if present_app:
            conn.execute("TRUNCATE TABLE " + ", ".join(_quote(t) for t in present_app)
                         + " RESTART IDENTITY CASCADE")
        for name in foreign:                     # میز (temp table) یک تست نباید به تست بعد برسد
            conn.execute(f"DROP TABLE IF EXISTS {_quote(name)} CASCADE")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@pytest.fixture(scope="session", autouse=True)
def pg_isolated_schema():
    """
    fixture سراسریِ جلسه‌ای: فقط teardown را وصل می‌کند.

    ساخت اسکیم در سطح ماژول انجام شد (پیش از import ماژول‌های تست) تا تست‌هایی که
    ``PG_DSN`` را در زمان collection می‌خوانند هم از ایزولاسیون بهره‌مند شوند.
    """
    yield {"schema": _state.get("schema"), "dsn": _state.get("dsn")}
    _cleanup()


@pytest.fixture(autouse=True)
def pg_clean_db_per_test(pg_isolated_schema):
    """
    ایزولاسیون **داده** در مقیاس تک‌تک تست (صرفِ هزینهٔ migration یک‌بار در جلسه).

    اگر ایزولاسیون PG فعال نباشد (``PG_DSN`` خالی / نقش read-only) بی‌عمل است.
    خطای بازنشانی یک‌بار هشدار می‌دهد و از آن پس خاموش می‌شود، تا صدها خطای
    تکراری منشأ اصلی را نپوشاند.
    """
    global _reset_broken
    if _app_backend_is_postgres() and pg_isolated_schema.get("schema") and not _reset_broken:
        try:
            _reset_state()
        except Exception as exc:                 # noqa: BLE001
            _reset_broken = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:140]}"
            warnings.warn(f"بازنشانی دادهٔ PG انجام نشد ⇒ از این پس بدون بازنشانی: "
                          f"{_reset_broken}", stacklevel=2)
    yield


#: شمارش skipهای sqlite_only برای گزارش پایانی.
_sqlite_only_skipped = 0

SKIP_SQLITE_ONLY_REASON = (
    "sqlite_only: این تست SQL/اسکیمای مختص SQLite می‌نویسد (CREATE TABLE … "
    "AUTOINCREMENT، INSERT OR REPLACE، json_extract، یا فرض «DB خالیِ هر chdir»)؛ "
    "زیر DB_BACKEND=postgresql معنا ندارد ⇒ docs/phase-e-plan.md بند ۵.۳")


def pytest_collection_modifyitems(config, items):
    """
    auto-skip تست‌های `sqlite_only` — **فقط** وقتی برنامه روی PG است.

    چرا hook و نه `-m "not sqlite_only"`: خواسته این بود که اجرای `pytest tests/`
    بدون آرگومان خاص، روی هر دو backend سبز باشد. skip «صراحی‌شده» است: هم در
    `-rs` دیده می‌شود، هم در خلاصهٔ پایانی شمارش می‌شود، و روی مسیر SQLite هیچ
    اثری ندارد (پس قاعدهٔ «۰ skipped» آن مسیر پابرجاست).
    """
    global _sqlite_only_skipped
    if not _app_backend_is_postgres():
        return
    marker = pytest.mark.skip(reason=SKIP_SQLITE_ONLY_REASON)
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(marker)
            _sqlite_only_skipped += 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """یک خط شفاف‌سازی: چند تست sqlite_only رد شد (تا skip بی‌صدا نماند)."""
    if _sqlite_only_skipped:
        terminalreporter.write_line(
            f"printguard: {_sqlite_only_skipped} تست با marker sqlite_only رد شد "
            f"(اجرای PG) — فهرست و دلیل: docs/phase-e-plan.md بند ۵.۳")


def pytest_report_header(config):
    """یک خط شفاف‌سازی: تست‌ها روی کدام اسکیم می‌دوند."""
    schema = _state.get("schema")
    if schema:
        return f"printguard: PG ایزوله روی اسکیمای {schema} (هر اجرا یکی، teardown با CASCADE)"
    if not (os.environ.get(ENV_PG_DSN, "") or "").strip():
        return None
    return "printguard: PG در دسترس/ایزوله نیست ⇒ تست‌های PG روی همان تنظیمات قبلی"
