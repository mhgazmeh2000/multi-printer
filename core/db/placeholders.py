# -*- coding: utf-8 -*-
"""
ترجمه‌ی placeholder پارامتر کوئری (فاز D ساخته شد، فاز E.2 فعال شد).

همه‌ی کوئری‌های مخزن با سبک SQLite نوشته شده‌اند (``?``). روی PostgreSQL همان
کوئری‌ها باید با ``%s`` اجرا شوند؛ این ماژول همان ترجمه را انجام می‌دهد و از
E.2 در ``ConnectionAdapter._sql`` روی مسیر postgresql صدا زده می‌شود (روی
sqlite هیچ تغییری نمی‌دهد و همان رشته‌ی ورودی برگردانده می‌شود).

قاعده: ``?`` فقط وقتی ترجمه می‌شود که **خارج از** رشته‌ی نقل‌قول، شناسه‌ی
دابل‌کوت، کامنت خطی (``--``) و کامنت بلوکی (``/* */``) باشد.

escape درصد (فاز E.2)
---------------------
psycopg متن کوئری را در سطح **کاراکتری** برای placeholderها اسکن می‌کند؛ پس هر
``%`` تنها (حتی داخل رشته‌ی نقل‌قول یا کامنت) باید ``%%`` باشد وگرنه با
``ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders`` می‌شکند
(اندازه‌گیری‌شده روی psycopg 3.3.6). ترتیب کار مهم است: **اول** escape درصد،
**بعد** ترجمه‌ی ``?`` — تا ``%s``های تازه‌ساخته‌شده دوباره escape نشوند.

توجه: escape روی backend ``sqlite`` انجام **نمی‌شود** (رفتار sqlite3 هیچ مفهومی
از ``%`` ندارد و تغییرش متن کوئری را خراب می‌کرد).
"""

from __future__ import annotations

from typing import Optional

from .exceptions import PlaceholderTranslationError

#: سبک placeholder هر backend. کلیدهای این دیکشنری باید با ثابت‌های
#: ``core.db.connection.BACKEND_*`` یکسان بمانند (تست نگهبان دارد).
PLACEHOLDER_STYLE = {
    "sqlite": "?",
    "postgresql": "%s",
}


def _replace_question_marks(sql: str, replacement: str) -> str:
    """جایگزینی ``?``های خارج از رشته/شناسه/کامنت."""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # رشته‌ی تک‌کوت‌دار: '' = کوت فرار
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        # شناسه‌ی دابل‌کوت‌دار: "" = دابل‌کوت فرار
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue

        # کامنت خطی تا پایان خط
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j])
            i = j
            continue

        # کامنت بلوکی
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(sql[i:j])
            i = j
            continue

        if ch == "?":
            out.append(replacement)
            i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def escape_percent_signs(sql: str) -> str:
    """
    هر ``%`` را به ``%%`` تبدیل می‌کند (قرارداد psycopg برای متن کوئری).

    عمداً **متنی** است و نه سازه‌شناسانه: psycopg هم متن را کاراکتری اسکن می‌کند،
    پس ``%`` داخل رشته/کامنت هم باید escape شود. این تابع idempotent **نیست**
    (اجرای دوباره ``%%%%`` می‌سازد) چون کاربردش فقط یک‌بار در مسیر ترجمه است.
    """
    return sql.replace("%", "%%")


def translate_placeholders(sql: str, backend: str,
                           *, escape_percent: Optional[bool] = None) -> str:
    """
    کوئری نوشته‌شده با سبک ``?`` را به سبک backend مقصد ترجمه می‌کند.

    Args:
        sql: متن کوئری.
        backend: ``"sqlite"`` (بازگشت بدون تغییر) یا ``"postgresql"``.
        escape_percent: ``None`` ⇒ پیش‌فرضِ backend (postgresql: ``True``،
            sqlite: ``False``). دادن ``True`` همراه ``sqlite`` خطای صریح است تا
            کسی اشتباهی متن SQLite را خراب نکند.

    Raises:
        PlaceholderTranslationError: backend ناشناخته، sql غیررشته، یا
            ``escape_percent=True`` همراه backendی که این مفهوم را ندارد.
    """
    if not isinstance(sql, str):
        raise PlaceholderTranslationError(
            f"sql باید str باشد، نه {type(sql).__name__}", backend=backend)

    style = PLACEHOLDER_STYLE.get(backend)
    if style is None:
        raise PlaceholderTranslationError(
            f"سبک placeholder برای backend «{backend}» تعریف نشده است "
            f"(شناخته‌شده: {', '.join(sorted(PLACEHOLDER_STYLE))})",
            backend=backend)

    if style == "?":
        if escape_percent:
            raise PlaceholderTranslationError(
                f"escape درصد برای backend «{backend}» معنا ندارد "
                f"(فقط برای درایورهای سبک DB-API با placeholder درصدی).",
                backend=backend)
        return sql

    if escape_percent is None:
        escape_percent = True  # پیش‌فرض مسیر postgresql
    prepared = escape_percent_signs(sql) if escape_percent else sql
    return _replace_question_marks(prepared, style)


__all__ = ["PLACEHOLDER_STYLE", "escape_percent_signs", "translate_placeholders"]
