# -*- coding: utf-8 -*-
"""
core/db/scripts.py — شکستن اسکریپت SQL به statementها (E.4.7).

چرا لازم است؟
-------------
``sqlite3.Connection.executescript`` چند دستور را پشت‌سرهم اجرا می‌کند. معادل
دقیقش در psycopg وجود ندارد و رفتارش هم بسته به مسیر کوئری عوض می‌شود:

  • ``conn.execute("A; B", ())`` روی psycopg ۳ کار می‌کند (پروتکل ساده)؛
  • ولی ``conn.execute("A; B", (1,))`` می‌شکند:
    ``SyntaxError: cannot insert multiple commands into a prepared statement``
    (اندازه‌گیری‌شده) — چون با پارامتر غیرخالی، درایور به prepared statement
    می‌رود.

پس اجرای «تک‌تک» statementها امن‌ترین و قابل‌انتقال‌ترین راه است و مزیت دوم هم
دارد: خطا به **همان statement** نسبت داده می‌شود، نه به کل اسکریپت.

این پیمایشگر عمداً کوته‌نظر نیست: ``;`` داخل رشته/شناسه‌ی نقل‌قولی، کامنت خطی
(``-- …``) و کامنت بلوکی (``/* … */``) جداکننده نیست. رشته‌های dollar-quoted
(``$$ … $$``) پشتیبانی **نمی‌شوند** و به‌جای شکستن اشتباه، خطای روشن می‌دهند.
"""

from __future__ import annotations

from typing import List

#: پیام خطای dollar-quoting — جدا تا تست بتواند به آن تکیه کند.
DOLLAR_QUOTE_ERROR = (
    "اسکریپت شامل رشته‌ی dollar-quoted است (مثل $$ … $$ یا $tag$ … $tag$) و "
    "split_sql_statements آن را پشتیبانی نمی‌کند؛ چنین اسکریپتی را دستی به "
    "statementها بشکنید (E.4.7 — core/db/scripts.py)."
)


def split_sql_statements(script: str) -> List[str]:
    """
    یک اسکریپت SQL را به فهرست statementها می‌شکند (بدون ``;`` پایانی و بدون
    statementهای خالی/فقط‌کامنت).

    Args:
        script: متن اسکریپت.

    Returns:
        فهرست statementها به ترتیب ظاهر شدن؛ متن هر statement دست‌نخورده
        (فقط فاصله‌های ابتدا/انتها حذف شده) تا SQL ارسالی خوانا بماند.

    Raises:
        ValueError: اگر رشته‌ی نقل‌قولی بسته نشده باشد، یا dollar-quoting دیده شود.
    """
    if not isinstance(script, str):
        raise ValueError(f"script باید str باشد، نه {type(script).__name__}")

    statements: List[str] = []
    buf: List[str] = []
    i, n = 0, len(script)

    while i < n:
        ch = script[i]

        # ── کامنت خطی: تا پایان خط ──
        if ch == "-" and script.startswith("--", i):
            end = script.find("\n", i)
            end = n if end == -1 else end
            buf.append(script[i:end])
            i = end
            continue

        # ── کامنت بلوکی: تا */ ──
        if ch == "/" and script.startswith("/*", i):
            end = script.find("*/", i + 2)
            if end == -1:
                raise ValueError("کامنت بلوکی در اسکریپت بسته نشده است (/* بدون */).")
            buf.append(script[i:end + 2])
            i = end + 2
            continue

        # ── رشته‌ی تک‌نقل‌قولی (با فرار '' ) ──
        if ch == "'":
            j = i + 1
            while j < n:
                if script[j] == "'":
                    if j + 1 < n and script[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValueError("رشته‌ی تک‌نقل‌قولی در اسکریپت بسته نشده است.")
            buf.append(script[i:j + 1])
            i = j + 1
            continue

        # ── شناسه‌ی دو‌نقل‌قولی (با فرار "" ) ──
        if ch == '"':
            j = i + 1
            while j < n:
                if script[j] == '"':
                    if j + 1 < n and script[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValueError("شناسه‌ی دو‌نقل‌قولی در اسکریپت بسته نشده است.")
            buf.append(script[i:j + 1])
            i = j + 1
            continue

        # ── dollar-quoting: پشتیبانی نمی‌شود ⇒ خطای روشن به‌جای شکستن اشتباه ──
        if ch == "$":
            raise ValueError(DOLLAR_QUOTE_ERROR)

        # ── جداکننده ──
        if ch == ";":
            statement = "".join(buf).strip()
            if statement and not _is_comment_only(statement):
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail and not _is_comment_only(tail):
        statements.append(tail)
    return statements


def _is_comment_only(text: str) -> bool:
    """آیا متن فقط کامنت است؟ (تا کامنت‌های میان statementها اجرا نشوند)"""
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                return False
            i = end + 2
            continue
        return False
    return True


__all__ = ["DOLLAR_QUOTE_ERROR", "split_sql_statements"]
