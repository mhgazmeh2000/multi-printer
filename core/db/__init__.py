# -*- coding: utf-8 -*-
"""
core.db — لایه‌ی داده‌ی PrintGuard (فاز D: adapter نازک؛ فاز E.2: دو backend).

معنای این پکیج
--------------
یک **دورگیر** نازک است: از بیرون (web/core/tools) هیچ رفتاری تغییر نمی‌کند.
نقشه‌ی فازها:
  • فاز D: فقط ``sqlite`` (گارد ``UnsupportedBackendError`` برای هر مقدار دیگر).
  • فاز E.2 (همین حالا): ``postgresql`` هم در ``SUPPORTED_BACKENDS`` است و
    :func:`connect_postgres` با درایور ``psycopg`` ۳ وصل می‌شود (TimeZone=UTC قفل‌شده).
    ترجمه‌ی placeholder در همین فاز فعال می‌شود.
  • فاز E.3: فعال‌سازی ``DB_BACKEND=postgresql`` در production + writers آگاه-UTC.
    تا آن لحظه پیش‌فرضِ محیط توسعه همچنان ``sqlite`` است.

مسیر سازگاری
------------
``core.database.db_connection`` و ``core.security_audit._db_conn`` هر دو به
همین ماژول delegate می‌کنند، پس:
  • امضای توابع و محل import در کد موجود **تغییر نکرده**؛
  • ``monkeypatch``های احتمالی تست‌ها روی ``core.database.db_connection``
    هم‌چنان کار می‌کنند؛
  • backend از ``DB_BACKEND`` فقط در زمان فراخوانی خوانده می‌شود (نه در زمان
    import)، پس تست‌ها می‌توانند آن را عوض کنند و بلافاصله اثر بگیرند.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from .connection import (  # noqa: F401  (بازصادرسازی برای مصرف‌کنندگان فاز E)
    ALL_BACKENDS,
    BACKEND_ALIASES,
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    ENV_DB_BACKEND,
    ENV_PG_DSN,
    TZ_OPTIONS,
    SUPPORTED_BACKENDS,
    ConnectionAdapter,
    connect,
    connect_postgres,
    connect_sqlite,
    default_db_path,
    dsn_from_env,
    quote_ident,
    resolve_backend,
    resolve_dsn,
    timezone_options,
)
from .exceptions import (  # noqa: F401
    ColumnAlreadyExists,
    ConfigurationError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseOperationalError,
    PlaceholderTranslationError,
    UndefinedColumn,
    UndefinedTable,
    UniqueViolation,
    UnsupportedBackendError,
    UnsupportedFeatureError,
)
from .placeholders import (  # noqa: F401
    PLACEHOLDER_STYLE,
    escape_percent_signs,
    translate_placeholders,
)
from .scripts import split_sql_statements  # noqa: F401  (E.4.7: شکستن اسکریپت)
from .upsert import upsert_sql  # noqa: F401  (E.5: درج یا جایگزینی، مستقل از backend)
from .schema import (  # noqa: F401  (E.4.5: تأیید schema روی PG)
    APP_TABLES,
    BOOLEAN_COLUMNS,
    SECURITY_TABLES,
    YIELD_TABLES,
    boolean_columns_of,
    is_postgres,
    missing_tables,
    present_tables,
    schema_columns,
    verify_postgres_tables,
)

log = logging.getLogger("PrinterMonitor")

#: هشدار «backend تصمیم‌گیری‌شده» فقط یک بار در طول عمر پروسه چاپ می‌شود.
_BACKEND_LOGGED: Optional[str] = None


def _log_backend_once(backend: str) -> None:
    global _BACKEND_LOGGED
    if _BACKEND_LOGGED == backend:
        return
    _BACKEND_LOGGED = backend
    log.info("core.db: backend دیتابیس = %s (DB_PATH=%s)", backend, default_db_path())


def get_connection(timeout: float = 30.0, commit: bool = False) -> ConnectionAdapter:
    """
    یک اتصال تازه به دیتابیسِ پیش‌فرض (``config.settings.DB_PATH``) می‌سازد.

    Args:
        timeout: مهلت قفل‌شدن (ثانیه). همان معنای ``sqlite3.connect(timeout=...)``.
        commit: در این فاز **بدون اثر** است؛ فقط برای سازگاری امضا نگه داشته
            شده و اتصال را *commit نمی‌کند*. مدیریت تراکنش در
            :func:`db_connection` انجام می‌شود.

    Raises:
        UnsupportedBackendError: اگر ``DB_BACKEND`` مقدار ناشناخته داشته باشد.
        ConfigurationError: اگر ``DB_BACKEND=postgresql`` باشد و ``PG_DSN`` نباشد.
        DatabaseConnectionError: اگر ساخت اتصال شکست بخورد.
    """
    return _open(path=None, timeout=timeout)


def connect_to(path: str, timeout: float = 30.0) -> ConnectionAdapter:
    """
    اتصال به یک **فایل مشخص** (نه مسیر پیش‌فرض) — برای دیتابیس‌های موقت/آپلودی
    مثل مسیر ``temp_import_*.db`` در ``web/routes/import_db.py``.

    توجه: پارامتر ``timeout`` را فراخوان‌ها صریح می‌فرستند تا رفتار قبلی
    (``sqlite3.connect(temp_path)`` با پیش‌فرض ۵ ثانیه) عوض نشود.

    **قفل backend (E.6.1):** این هِلپر «یک فایل» را باز می‌کند و فایلِ ``.db`` یعنی
    SQLite — پس backend صریحاً ``sqlite`` است و ``DB_BACKEND`` برنامه را نمی‌خواند.
    پیش از این، زیر ``DB_BACKEND=postgresql`` خودِ ``connect()`` با
    ``ConfigurationError`` («path همراه postgresql») رد می‌شد و
    ``/api/import/analyze`` با ۵۰۰ می‌سوخت، در حالی که گارد E.4.8 صریحاً analyze را
    باز گذاشته بود («فایل مبدأ همیشه SQLite است»). این قفل همان نیت را پیاده می‌کند
    و هیچ مسیر نوشتن برنامه را تغییر نمی‌دهد.
    """
    return _open(path=path, timeout=timeout, backend=BACKEND_SQLITE)


def _open(path: Optional[str], timeout: float,
          backend: Optional[str] = None) -> ConnectionAdapter:
    """ساخت اتصال + یک‌بار لاگ‌کردن backend انتخاب‌شده."""
    conn = connect(path, timeout=timeout, backend=backend)
    _log_backend_once(conn.backend)
    return conn


@contextmanager
def db_connection(timeout: float = 30.0, commit: bool = False) -> Iterator[ConnectionAdapter]:
    """
    Context manager برای اتصال امن به دیتابیس (نسخه‌ی adapter).

    سازگاری با ``core.database.db_connection`` قبلی:
      • برنگشتن خطا ⇒ ``commit`` فقط اگر ``commit=True`` باشد؛
      • بروز خطا ⇒ ``rollback`` (فقط در حالت ``commit=True``، مثل قبل) و
        سپس **بازتاب همان استثنا**؛
      • در همه‌ی حالت‌ها ``close()``.

    یک تفاوت فنی و بی‌اثر از بیرون: اتصال در لحظه‌ی ورود به ``with`` ساخته
    می‌شود (قبلاً با ``sqlite3.connect`` در خط اول بدنه). خطاهای ساخت اتصال
    اکنون در همان نقطه با نوع ``DatabaseConnectionError`` بالا می‌آیند.
    """
    conn = get_connection(timeout=timeout, commit=commit)
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = [
    # سازگاری
    "get_connection",
    "connect_to",
    "connect",
    "connect_sqlite",
    "connect_postgres",
    "dsn_from_env",
    "timezone_options",
    "TZ_OPTIONS",
    "default_db_path",
    "quote_ident",
    "db_connection",
    # کلاس‌ها و خطاها
    "ConnectionAdapter",
    "DatabaseError",
    "DatabaseOperationalError",
    "ColumnAlreadyExists",
    "UndefinedTable",
    "UndefinedColumn",
    "UniqueViolation",
    "DatabaseConnectionError",
    "ConfigurationError",
    "UnsupportedBackendError",
    "UnsupportedFeatureError",
    "PlaceholderTranslationError",
    # backend
    "resolve_backend",
    "resolve_dsn",
    "BACKEND_SQLITE",
    "BACKEND_POSTGRESQL",
    "SUPPORTED_BACKENDS",
    "ALL_BACKENDS",
    "BACKEND_ALIASES",
    "ENV_DB_BACKEND",
    "ENV_PG_DSN",
    # placeholder
    "PLACEHOLDER_STYLE",
    "escape_percent_signs",
    "translate_placeholders",
    # اسکریپت (E.4.7)
    "split_sql_statements",
    # upsert (E.5)
    "upsert_sql",
    # schema (E.4.5)
    "APP_TABLES",
    "BOOLEAN_COLUMNS",
    "YIELD_TABLES",
    "boolean_columns_of",
    "SECURITY_TABLES",
    "is_postgres",
    "present_tables",
    "missing_tables",
    "schema_columns",
    "verify_postgres_tables",
]
