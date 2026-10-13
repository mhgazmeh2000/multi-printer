# -*- coding: utf-8 -*-
"""
خطاهای مشترک لایه‌ی داده (فاز D — adapter نازک روی SQLite).

هدف: کد بالادستی بتواند خطاهای دیتابیس را **مستقل از backend** بگیرد. از E.4.4
``ConnectionAdapter`` خطای درایور را پیش از رسیدن به مصرف‌کننده ترجمه می‌کند
(``core/db/connection.py::_translate_error``) و کد برنامه (``core/`` و ``web/``)
دیگر هیچ ``except sqlite3.*`` ندارد.

نکته‌ی نام‌گذاری: ``DatabaseConnectionError`` عمداً به‌جای ``ConnectionError``
انتخاب شده تا با built-in پایتون اشتباه گرفته نشود.
"""

from __future__ import annotations

from typing import Optional


class DatabaseError(Exception):
    """
    ریشه‌ی همه‌ی خطاهای لایه‌ی داده.

    Attributes:
        backend: backendی که خطا از آن آمده (``sqlite``/``postgresql``/``None``).
        sqlstate: کد SQLSTATE درایور مبدأ، اگر داشت (فقط PG؛ در SQLite ``None``).
        original: خودِ استثنای درایور (برای تشخیص دقیق/بازخوانی پیام اصلی).
    """

    def __init__(self, message: str = "", *, backend: Optional[str] = None,
                 sqlstate: Optional[str] = None, original: Optional[BaseException] = None):
        super().__init__(message)
        self.backend = backend
        self.sqlstate = sqlstate
        self.original = original


class UnsupportedBackendError(DatabaseError):
    """backend درخواستی ناشناخته است یا در این فاز پشتیبانی نمی‌شود."""


class ConfigurationError(DatabaseError):
    """
    پیکربندی ناقص/متناقض است (مثلاً ``DB_BACKEND=postgresql`` بدون ``PG_DSN``).

    جدا از ``UnsupportedBackendError`` است: آن‌جا backend **نامعتبر** است، این‌جا
    backend معتبر است ولی **اطلاعات لازم برای اتصال نیست**. عمداً هیچ fallback
    بی‌صدایی انجام نمی‌شود (کار روی دیتابیس اشتباه از بالا نیامدن خطرناک‌تر است).
    """


class DatabaseConnectionError(DatabaseError):
    """برقراری یا نگه‌داری اتصال به دیتابیس ممکن نشد."""


class DatabaseOperationalError(DatabaseError):
    """
    خطای اجرایی دیتابیس: قفل‌بودن، پر بودن دیسک، قطع اتصال، مهلت تمام‌شده.

    معادل مشترک ``sqlite3.OperationalError`` و ``psycopg.OperationalError``.
    مصرف‌کننده‌ها باید همین را بگیرند، نه نوع درایور را. برای تصمیم‌گیری دقیق‌تر
    (مثلاً «قفل موقت» در برابر «دیسک پر») هم ``sqlstate`` و هم پیام درایور
    (دست‌نخورده) در دسترس است — توجه: پیام، متنِ خودِ درایور است و بررسی رشته‌ای
    روی آن همچنان شکننده است (یادداشت E.4.4؛ کاندید فاز بعد).
    """


class ColumnAlreadyExists(DatabaseError):
    """
    ستون از قبل وجود دارد — مسیر idempotent ``ALTER TABLE … ADD COLUMN``.

    معادل مشترک ``sqlite3.OperationalError: duplicate column name`` و
    ``psycopg.errors.DuplicateColumn`` (SQLSTATE 42701).
    """


class UndefinedTable(DatabaseError):
    """جدول وجود ندارد (SQLSTATE 42P01 / ``sqlite3``: «no such table»)."""


class UndefinedColumn(DatabaseError):
    """ستون وجود ندارد (SQLSTATE 42703 / ``sqlite3``: «no such column»)."""


class UniqueViolation(DatabaseError):
    """
    تعارض یکتایی/موجودیت — **کل خانوادهٔ integrity**.

    تصمیم E.4.4 (مالک): در SQLite هر ``sqlite3.IntegrityError`` (UNIQUE، NOT NULL،
    FOREIGN KEY، CHECK) و در PG هر SQLSTATE با پیشوند ``23`` به همین کلاس نگاشت
    می‌شود تا رفتار امروزِ ``web/routes/import_db.py`` (شمارش «skipped» و ادامهٔ
    حلقه) روی هر دو backend یکی بماند؛ هیچ شکست رفتاری رخ نمی‌دهد.

    ⚠ بدهی فنی شناخته‌شده (ثبت در ``docs/tech-debt.md``): این نام برای کل خانواده
    دقیق نیست. در فازی که تصمیم محصولی گرفته شود، باید یک پایهٔ ``IntegrityError``
    با زیرکلاس‌های ``NotNullViolation``/``ForeignKeyViolation``/``UniqueViolation``
    افزوده شود و ``import_db`` صریح انتخاب کند کدام خانواده «skip» شود.
    """


class PlaceholderTranslationError(DatabaseError):
    """ترجمه‌ی placeholder‌های کوئری برای backend مقصد ممکن نبود."""


class UnsupportedFeatureError(DatabaseError):
    """
    قابلیتی که کد صدا می‌زند روی این backend پیاده نشده است.

    فرقش با ``UnsupportedBackendError`` روشن است: آن‌جا *backend* نامعتبر است؛
    این‌جا backend معتبر و سالم است، ولی یک قابلیت مشخص روی آن ترجمه نشده
    (مثال زنده: حالت ``replace`` در import روی PostgreSQL — E.4.8 آن را با گارد
    بست و تصمیم E.6.1 **بازنشستگی** گرفت، نه ترجمه؛ دلیل و جایگزین‌های ردشده در
    ``docs/phase-e-decisions.md`` ردیف ۱.)

    عمداً در همین سلسله‌مراتب است تا مصرف‌کننده‌ها یک ریشه بگیرند، و عمداً
    **خطای روشن و زودهنگام** است تا خطای نحوی مبهم درایور
    (``42601 syntax error at or near "OR"``) جای آن را نگیرد.
    """


__all__ = [
    "DatabaseError",
    "DatabaseOperationalError",
    "ColumnAlreadyExists",
    "UndefinedTable",
    "UndefinedColumn",
    "UniqueViolation",
    "UnsupportedBackendError",
    "UnsupportedFeatureError",
    "ConfigurationError",
    "DatabaseConnectionError",
    "PlaceholderTranslationError",
]
