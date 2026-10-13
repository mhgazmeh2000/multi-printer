# -*- coding: utf-8 -*-
"""
core/db/schema.py — تأیید schema روی PostgreSQL (E.4.5).

قاعده‌ی E.4.5
-------------
روی **SQLite** هر ماژول جدول‌های خودش را با ``CREATE TABLE IF NOT EXISTS`` می‌سازد
(شکل تاریخی پروژه و لازم است، چون فایل دیتابیس می‌تواند از نسخه‌ی قدیمی بیاید).

روی **PostgreSQL** این کار ممنوع است: ``Alembic`` تنها منبع DDL است
(``alembic/versions/``). نگه‌داشتن دو نسخه از یک DDL (یکی در Python، یکی در
migration) دیر یا زود واگرا می‌شود و دلیل اصلی شکست‌ها را از چشم می‌اندازد؛
``INTEGER PRIMARY KEY AUTOINCREMENT`` هم اصلاً نحو PostgreSQL نیست.

پس مسیر PG توابع bootstrap فقط **تأیید** می‌کنند: اگر جدولی که باید باشد نباشد،
خطای روشن با راه‌حل می‌دهند (شکست بی‌صدا ممنوع — همان درسی که
``init_security_audit`` می‌داد: خطا فقط log می‌شد و برنامه جلو می‌رفت).

نکته: این ماژول به ``connection`` وابسته است، نه به ``core/db/__init__`` — تا
import چرخه‌ای نسازد.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .connection import BACKEND_POSTGRESQL, quote_ident, resolve_backend

#: جدول‌هایی که migration ``0001`` می‌سازد (۱۱ مورد). روی PG این‌ها باید موجود
#: باشند؛ روی SQLite فقط مبنای مقایسه‌اند (تست‌ها).
APP_TABLES = (
    "logs",
    "printer_counters",
    "toner_history",
    "sensor_readings",
    "users",
    "security_events",
    "yield_samples",
    "toner_snapshots_v2",
    "cartridge_state",
    "cartridge_yield_profiles",
    "meta",
)

#: جدول‌هایی که ``core.yield_engine.ensure_yield_tables`` روی SQLite می‌سازد.
YIELD_TABLES = (
    "cartridge_state",
    "yield_samples",
    "toner_snapshots_v2",
    "cartridge_yield_profiles",
)

#: جدول‌هایی که ``core.security_audit.init_security_audit`` روی SQLite می‌سازد.
SECURITY_TABLES = ("security_events",)

#: ستون‌هایی که روی PostgreSQL نوع ``BOOLEAN`` دارند (migration ``0002``).
#: روی SQLite نوع BOOLEAN وجود ندارد و ``0/1`` شکل کانونیک است، پس این فهرست
#: **فقط** برای درج مقادیر از یک فایل SQLite به PostgreSQL معنا دارد
#: (مصرف‌کننده‌ی امروز: ``web/routes/import_db.py`` در E.4.8).
#: هم‌خوانی این فهرست با migration در تست قفل شده است.
BOOLEAN_COLUMNS = (
    ("users", "is_verified"),
    ("users", "email_verified"),
    ("users", "is_active"),
    ("yield_samples", "accepted"),
    ("security_events", "success"),
)


def boolean_columns_of(table: str) -> tuple:
    """نام ستون‌های بولی یک جدول (زیرمجموعه‌ی ``BOOLEAN_COLUMNS``)."""
    return tuple(col for tbl, col in BOOLEAN_COLUMNS if tbl == table)


#: جدولی که خودِ Alembic می‌سازد (نشانه‌ی اجرای migration).
ALEMBIC_TABLE = "alembic_version"


def is_postgres() -> bool:
    """آیا backend فعال ``postgresql`` است؟ (بدون نیاز به اتصال، از ``DB_BACKEND``)"""
    return resolve_backend() == BACKEND_POSTGRESQL


def schema_columns(conn, table: str) -> list:
    """
    نام ستون‌های یک جدول، مستقل از backend.

    چرا لازم است: ``PRAGMA table_info`` فقط نحو SQLite است و روی PostgreSQL با
    ``syntax error at or near "PRAGMA"`` می‌شکند (سنجیده‌شده، E.4.8). هر کدی که
    فهرست ستون‌ها را برای ساختن یک جمله‌ی SQL لازم دارد باید از این‌جا بگیرد.

    Args:
        conn: اتصال adapter (روی هر دو backend).
        table: نام جدول.

    Returns:
        فهرست نام ستون‌ها **به ترتیب اعلام** در جدول؛ جدول ناموجود ⇒ ``[]``
        (روی هر دو backend یکسان: نه PRAGMA و نه ``information_schema`` برای
        جدول ناموجود خطا نمی‌دهند).
    """
    if conn.backend == BACKEND_POSTGRESQL:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return [r[0] for r in rows]
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [r[1] for r in rows]


def present_tables(conn) -> set:
    """نام جدول‌های پایه در schema جاری (روی هر دو backend؛ روی SQLite از sqlite_master)."""
    if conn.backend == BACKEND_POSTGRESQL:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        ).fetchall()
        return {r[0] for r in rows}
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


def missing_tables(conn, tables: Sequence[str]) -> list:
    """زیرمجموعه‌ی ``tables`` که در دیتابیس موجود نیست."""
    present = present_tables(conn)
    return [t for t in tables if t not in present]


def verify_postgres_tables(conn, tables: Iterable[str], *, context: str) -> None:
    """
    تأیید حضور جدول‌ها روی PostgreSQL (Alembic تنها منبع DDL است).

    Args:
        conn: اتصال adapter (باید backend آن ``postgresql`` باشد).
        tables: جدول‌هایی که باید موجود باشند.
        context: نام فراخوان، برای پیام خطای روشن.

    Raises:
        RuntimeError: اگر جدولی نباشد — با فهرست دقیق و راه‌حل (``alembic upgrade head``).
    """
    missing = missing_tables(conn, sorted(set(tables)))
    if missing:
        raise RuntimeError(
            f"{context}: این جدول‌ها در PostgreSQL نیستند: {', '.join(missing)} — "
            "روی PostgreSQL اسکیما را Alembic می‌سازد (نه کد برنامه): "
            "«python -m alembic upgrade head» را با همان ALEMBIC_DB_URL اجرا کنید "
            "(E.4.5 — docs/postgresql-strictness.md بند ۱۲)."
        )


__all__ = [
    "ALEMBIC_TABLE",
    "APP_TABLES",
    "BOOLEAN_COLUMNS",
    "SECURITY_TABLES",
    "YIELD_TABLES",
    "boolean_columns_of",
    "is_postgres",
    "missing_tables",
    "present_tables",
    "schema_columns",
    "verify_postgres_tables",
]
