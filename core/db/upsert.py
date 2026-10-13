# -*- coding: utf-8 -*-
"""
core.db.upsert — جملهٔ «درج یا جایگزینی» مستقل از backend (فاز E.5).

چرا این هِلپر لازم شد
---------------------
``INSERT OR REPLACE`` **نحو SQLite** است. روی PostgreSQL می‌شکند:
``syntax error at or near "OR"`` (SQLSTATE 42601 — سنجیده در آغاز E.5،
۲۰ شکست فقط از همین یک الگو). جایگزین PostgreSQL عبارت
``INSERT … ON CONFLICT (کلید) DO UPDATE SET`` است.

معنای دو عبارت **وقتی همهٔ ستون‌ها نوشته می‌شوند** یکسان است:
  • SQLite: ردیف کهنه حذف و ردیف تازه درج می‌شود ⇒ ستونِ نوشته‌نشده به
    پیش‌فرض برمی‌گردد؛
  • PG: ردیف درجا به‌روزرسانی می‌شود ⇒ ستونِ نوشته‌نشده **ارزش قبلی** را نگه
    می‌دارد.
پس این هِلپر عمداً فقط برای جمله‌هایی ساخته شده که **همهٔ ستون‌های جدول** را
می‌نویسند (`docs/insert-or-replace-inventory.md` §۳.۱ — همان سه محلی که در E.5
منتقل شدند). برای جمله‌های با ستون ناقص، ترجمه بی‌دقت یعنی تغییر معنای داده
(بخش ۳.۲ همان سند، کد مرده، آگاهانه دست‌نخورده).

قاعده‌ی E.5: کد برنامه (``core/`` و ``web/``) هیچ ``INSERT OR REPLACE`` دستی
نمی‌نویسد؛ از این هِلپر استفاده می‌کند (قفل: ``tests/test_upsert_cross_backend.py``).
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .connection import BACKEND_POSTGRESQL, quote_ident, resolve_backend

#: جای‌نمای ستونِ درج‌شده در بند ``DO UPDATE SET`` پستگرس.
_EXCLUDED = "EXCLUDED"


def _ident_list(names: Iterable[str]) -> str:
    return ", ".join(quote_ident(n) for n in names)


def upsert_sql(
    table: str,
    columns: Sequence[str],
    conflict_columns: Sequence[str],
    *,
    values_sql: Optional[str] = None,
    backend: Optional[str] = None,
) -> str:
    """
    جملهٔ «درج یا جایگزینی» را برای backend فعال می‌سازد.

    Args:
        table: نام جدول (نقل‌قول می‌شود).
        columns: ستون‌های درج‌شده، **به همان ترتیبی که پارامترها داده می‌شوند**.
        conflict_columns: کلید یکتایی که تعارض روی آن سنجیده می‌شود
            (روی PG به‌عنوان هدف ``ON CONFLICT``؛ روی SQLite صرفاً اعتبارسنجی می‌شود).
        values_sql: بدنهٔ ``VALUES`` — اگر داده نشود، یک ``?`` به‌ازای هر ستون
            ساخته می‌شود. برای جمله‌هایی که مقدار ثابت در فهرست دارند
            (مثلاً ``?, ?, 'high', ?``) باید صریح داده شود؛ در این حالت طول
            فهرست ستون‌ها و تعداد جای‌نماها عمداً مقایسه نمی‌شود.
        backend: پوشش ``DB_BACKEND`` (عمدتاً برای تست).

    Returns:
        متن SQL با جای‌نمای ``?`` (ترجمهٔ placeholder کار adapter است).

    Raises:
        ValueError: اگر ``columns`` یا ``conflict_columns`` خالی باشد، یا
            ``conflict_columns`` زیرمجموعهٔ ``columns`` نباشد.
    """
    if not columns:
        raise ValueError("upsert_sql: فهرست ستون‌ها نمی‌تواند خالی باشد")
    if not conflict_columns:
        raise ValueError(
            "upsert_sql: کلید تعارض لازم است (روی PG هدف ON CONFLICT باید صریح باشد)")
    missing = [c for c in conflict_columns if c not in columns]
    if missing:
        raise ValueError(
            f"upsert_sql: ستون‌های تعارض در فهرست درج نیستند: {', '.join(missing)}")

    active = backend if backend is not None else resolve_backend()
    cols_sql = _ident_list(columns)
    values = values_sql if values_sql is not None else ", ".join(["?"] * len(columns))

    if active != BACKEND_POSTGRESQL:
        # مسیر SQLite: بایت‌به‌بایت همان عبارت تاریخی (REPLACE = حذف + درج).
        return f"INSERT OR REPLACE INTO {quote_ident(table)} ({cols_sql}) VALUES ({values})"

    update_cols = [c for c in columns if c not in conflict_columns]
    if not update_cols:
        # «تعارض ⇒ هیچ کاری نکن» معادل DO NOTHING است؛ DO UPDATE SET خالی مجاز نیست.
        return (
            f"INSERT INTO {quote_ident(table)} ({cols_sql}) VALUES ({values}) "
            f"ON CONFLICT ({_ident_list(conflict_columns)}) DO NOTHING"
        )
    assignments = ", ".join(
        f"{quote_ident(c)} = {_EXCLUDED}.{quote_ident(c)}" for c in update_cols
    )
    return (
        f"INSERT INTO {quote_ident(table)} ({cols_sql}) VALUES ({values}) "
        f"ON CONFLICT ({_ident_list(conflict_columns)}) DO UPDATE SET {assignments}"
    )


__all__ = ["upsert_sql"]
