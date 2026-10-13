# -*- coding: utf-8 -*-
"""
alembic/env.py — پیکربندی Alembic برای پروژه‌ی PrintGuard (فاز E.1).

دو backend، یک مجموعه migration
-------------------------------
Migrationها **دست‌نویس** هستند (بدون ORM؛ `target_metadata = None`) و باید روی هر دو
دیتابیس اجرا شوند:

  • `DB_BACKEND=sqlite` (پیش‌فرض، همان وضعیت امروزِ برنامه) ⇒ مسیر از `config.settings.DB_PATH`
  • `DB_BACKEND=postgresql` ⇒ نشانی از `PG_DSN`

قاعده‌ی طلایی: **fallback بی‌صدا وجود ندارد.** اگر backend روی postgresql باشد و `PG_DSN`
خالی باشد، اجرا با خطای صریح متوقف می‌شود؛ چون اجرای migration روی دیتابیس اشتباه
خطرناک‌تر از بالا نیامدن است (همان قاعده‌ای که `core/db/connection.resolve_backend` دارد).

نکته‌ی مسیر SQLite
-------------------
مسیر نسبیِ `DB_PATH` (پیش‌فرض `logs.db`) نسبت به **ریشه‌ی پروژه** مطلق می‌شود، نه نسبت به
پوشه‌ی جاری؛ تا اجرای alembic از هر جایی، همان فایل موردنظر برنامه را ببیند و فایل
خالیِ ناخواسته ساخته نشود.

قلاب override (فقط برای آزمون/سناریوهای ویژه)
---------------------------------------------
`ALEMBIC_DB_URL` در محیط، کل تشخیص را دور می‌زند (مثلاً برای اجرای migration روی یک کپی
موقت: `ALEMBIC_DB_URL=sqlite:////tmp/copy.db`). هنگام استفاده، هشدار چاپ می‌شود.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, event, pool

from alembic import context

# ─────────────────────────────────────────────────────────────
# مسیرها و import پروژه
# ─────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: بدون ORM: هیچ MetaDataای برای autogenerate وجود ندارد؛ migrationها دست‌نویس‌اند.
target_metadata = None

BACKEND_SQLITE = "sqlite"
BACKEND_POSTGRESQL = "postgresql"
#: همان نام‌های جایگزینی که لایه‌ی داده‌ی برنامه می‌شناسد (`core/db/connection.py`).
BACKEND_ALIASES = {"postgres": BACKEND_POSTGRESQL, "pg": BACKEND_POSTGRESQL}
ENV_DB_BACKEND = "DB_BACKEND"
ENV_PG_DSN = "PG_DSN"
ENV_OVERRIDE_URL = "ALEMBIC_DB_URL"


class ConfigurationError(RuntimeError):
    """پیکربندی محیط برای migration ناقص یا متناقض است."""


def mask_dsn(dsn: str) -> str:
    """رمز را از DSN حذف می‌کند (هیچ‌وقت رمز چاپ نشود)."""
    out = []
    for token in str(dsn).split():
        out.append("password=***" if token.lower().startswith("password=") else token)
    return " ".join(out)


def resolve_backend(value: str | None = None) -> str:
    """backend را از پارامتر/`DB_BACKEND` تعیین می‌کند (خالی ⇒ sqlite)."""
    raw = (value if value is not None else os.getenv(ENV_DB_BACKEND, "")) or ""
    backend = raw.strip().lower()
    backend = BACKEND_ALIASES.get(backend, backend)
    if not backend:
        return BACKEND_SQLITE
    if backend in (BACKEND_SQLITE, BACKEND_POSTGRESQL):
        return backend
    raise ConfigurationError(
        f"مقدار ناشناخته برای {ENV_DB_BACKEND}: «{raw}» "
        f"(مجاز: {BACKEND_SQLITE}، {BACKEND_POSTGRESQL})")


def sqlite_url() -> str:
    """URL کامل SQLite از `config.settings.DB_PATH` (نسبی ⇒ نسبت به ریشه‌ی پروژه)."""
    from config.settings import DB_PATH  # noqa: PLC0415 — مطابق قرارداد پروژه

    path = Path(DB_PATH)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return "sqlite:///" + path.as_posix()


def get_url() -> str:
    """نشانی دیتابیس مقصد برای migration (بدون fallback بی‌صدا)."""
    override = os.getenv(ENV_OVERRIDE_URL, "").strip()
    if override:
        # تضمین استفاده از psycopg v3 (postgresql+psycopg://) به جای psycopg2
        if "://" not in override:
            override = "postgresql+psycopg://" + _dsn_to_url(override)
        elif override.startswith("postgresql://"):
            override = "postgresql+psycopg://" + override[len("postgresql://"):]
        elif override.startswith("postgres://"):
            override = "postgresql+psycopg://" + override[len("postgres://"):]
        print(f"[alembic] ⚠ override فعال است ({ENV_OVERRIDE_URL}); URL = {mask_dsn(override)}",
              file=sys.stderr)
        return override

    backend = resolve_backend()
    if backend == BACKEND_POSTGRESQL:
        dsn = os.getenv(ENV_PG_DSN, "").strip()
        if not dsn:
            raise ConfigurationError(
                f"{ENV_DB_BACKEND}=postgresql است ولی {ENV_PG_DSN} خالی است. "
                f"یا {ENV_PG_DSN} را ست کنید یا {ENV_DB_BACKEND} را به sqlite برگردانید "
                f"(هیچ fallback خودکاری انجام نمی‌شود).")
        print(f"[alembic] backend = postgresql | DSN = {mask_dsn(dsn)}", file=sys.stderr)
        return "postgresql+psycopg://" + _dsn_to_url(dsn)

    url = sqlite_url()
    print(f"[alembic] backend = sqlite | URL = {url}", file=sys.stderr)
    return url


def _dsn_to_url(dsn: str) -> str:
    """DSN به سبک libpq (`host=... dbname=...`) را به بخشِ بعد از `://` تبدیل می‌کند.

    پشتیبانی: کلیدهای رایج libpq + فرم URI (`postgresql://...`) که دست‌نخورده رد می‌شود.
    """
    if "://" in dsn:
        # از قبل URI است (مثل postgresql://user:pass@host/db)
        return dsn.split("://", 1)[1]

    from core.db.connection import _tokenize_conninfo

    parts = {}
    for token in _tokenize_conninfo(dsn):
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        parts[key.strip().lower()] = value.strip().strip("'\"")

    host = parts.get("host") or parts.get("hostaddr") or "localhost"
    port = parts.get("port") or "5432"
    user = parts.get("user") or ""
    password = parts.get("password") or ""
    dbname = parts.get("dbname") or parts.get("database") or ""

    auth = ""
    if user:
        from urllib.parse import quote  # noqa: PLC0415 — فقط برای escape رمز/کاربر
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    elif password:
        from urllib.parse import quote  # noqa: PLC0415
        auth = ":" + quote(password, safe="") + "@"

    tail = "/" + (dbname or "")
    extra = {k: v for k, v in parts.items()
             if k not in ("host", "hostaddr", "port", "user", "password", "dbname", "database")}
    if extra:
        from urllib.parse import urlencode  # noqa: PLC0415
        tail += "?" + urlencode(extra)
    return f"{auth}{host}:{port}{tail}"


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _install_sqlite_pragmas(engine) -> None:
    """`PRAGMA foreign_keys=ON` را روی هر اتصال تازه اعمال می‌کند.

    چرا event و نه اجرای دستور در بدنه؟ اجرای PRAGMA روی `Connection` باعث
    autobegin (باز شدن تراکنش سطح SQLAlchemy) می‌شود و همان چیزی است که در E.1.4
    باعث شد سطر `alembic_version` گم شود؛ اینجا PRAGMA در لایه‌ی DBAPI و پیش از هر
    تراکنشی اجرا می‌شود.
    """
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover - رویداد
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def _configure(connection=None, url: str | None = None) -> None:
    """پیکربندی مشترک online/offline (با تنظیمات مناسب هر dialect)."""
    opts = {
        "target_metadata": target_metadata,
        # مقایسه‌ی نوع ستون‌ها در autogenerate (فازهای بعدی که migration تولید می‌کنند).
        "compare_type": True,
        # روی SQLite هر ALTER باید به‌صورت batch (بازسازی جدول) اجرا شود.
        "render_as_batch": True,
        "include_schemas": False,
    }
    if connection is not None:
        context.configure(connection=connection, **opts)
    else:
        context.configure(url=url, literal_binds=True,
                          dialect_opts={"paramstyle": "named"}, **opts)


def run_migrations_offline() -> None:
    """حالت offline: فقط SQL تولید می‌شود (بدون اتصال)."""
    _configure(url=get_url())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """حالت online: اتصال واقعی و اجرای migrationها."""
    url = get_url()
    # مقداردهی از کد (نه از alembic.ini): `%` در رمز/URL باید escape شود.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)

    if _is_sqlite(url):
        # روی SQLite، foreign key enforcement پیش‌فرض خاموش است؛ برای migration
        # آیندهٔ این پروژه (که FK ندارد ولی قابل افزودن است) روشنش می‌کنیم.
        _install_sqlite_pragmas(connectable)

    with connectable.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()

        # ⚠ هشدار اندازه‌گیری‌شده (E.1.4): روی SQLite، alembic `transactional_ddl=False`
        # می‌بیند و برای «تراکنش بیرونی» فقط nullcontext برمی‌گرداند؛ در نتیجه تراکنش
        # سطح SQLAlchemy در پایان باز می‌ماند و `Connection.close()` آن را **rollback**
        # می‌کند. DDL به‌خاطر رفتار implicit-commit درایور sqlite3 باقی می‌ماند، ولی سطر
        # `alembic_version` (که DML است) **گم می‌شد**: جدول‌ها ساخته می‌شدند و نسخه
        # ثبت نمی‌شد (اثبات: alembic_version خالی و `alembic current` بی‌خروجی).
        # این commit صریح مشکل را می‌بندد و روی PostgreSQL (که transactional_ddl دارد)
        # بی‌اثر است، چون آنجا تراکنش پیش از این بسته شده است.
        if connection.in_transaction():
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
