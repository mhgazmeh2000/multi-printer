# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/scripts.py
# @brief:        Splits multi-statement SQL scripts into individual statements.
# @details:      Responsibilities: Parse SQL text while respecting quoted strings and comments.
#                Main API: split_sql_statements()
#                Side effects: none at import time
# @dependencies: stdlib only
# @notes:        None.
# =============================================================================

"""
core/db/scripts.py — Break SQL script into statements (E.4.7).

Why is it necessary?
-------------
``sqlite3.Connection.executescript'' executes several commands in sequence. Equivalent
It is not detailed in psycopg and its behavior changes depending on the query path:

  • ``conn.execute("A; B", ())`` works on psycopg 3 (simple protocol);
  • But ``conn.execute("A; B", (1,))'' breaks:
    ``SyntaxError: cannot insert multiple commands into a prepared statement``
    (measured) — because with a non-empty parameter, the driver to the prepared statement
    goes

So, executing "one by one" statements is the safest and most transferable way, and the second advantage
Yes: The error is attributed to the **same statement**, not the entire script.

This browser is not intentionally parsimonious: ``;`` inside quoted string/id, inline comment
(``--...``) and block comment (``/*... */``) are not delimiters. dollar-quoted strings
(``$$ … $$``) are **not** supported and give a clear error instead of breaking.
"""

from __future__ import annotations

from typing import List

#: dollar-quoting error message — isolated so testing can rely on it.
DOLLAR_QUOTE_ERROR = (
    "اسکریپت شامل رشته‌ی dollar-quoted است (مثل $$ … $$ یا $tag$ … $tag$) و "
    "split_sql_statements آن را پشتیبانی نمی‌کند؛ چنین اسکریپتی را دستی به "
    "statementها بشکنید (E.4.7 — core/db/scripts.py)."
)


def split_sql_statements(script: str) -> List[str]:
    """
    Breaks an SQL script into a list of statements (no trailing ``;'' and no
    empty statements/comment only).

    Args:
        script: script text.

    Returns:
        The list of statements in order of appearance; The text of each statement is intact
        (only start/end spaces are removed) to keep the sent SQL readable.

    Raises:
        ValueError: if the quoted string is not closed, or dollar-quoting is seen.
    """
    if not isinstance(script, str):
        raise ValueError(f"script باید str باشد، نه {type(script).__name__}")

    statements: List[str] = []
    buf: List[str] = []
    i, n = 0, len(script)

    while i < n:
        ch = script[i]

        # ── Line comment: until the end of the line ──
        if ch == "-" and script.startswith("--", i):
            end = script.find("\n", i)
            end = n if end == -1 else end
            buf.append(script[i:end])
            i = end
            continue

        # ── block comment: to */ ──
        if ch == "/" and script.startswith("/*", i):
            end = script.find("*/", i + 2)
            if end == -1:
                raise ValueError("کامنت بلوکی در اسکریپت بسته نشده است (/* بدون */).")
            buf.append(script[i:end + 2])
            i = end + 2
            continue

        # ── single-quoted string (with escaping '') ──
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

        # ── double-quoted identifier (with escaping "") ──
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

        # ── dollar-quoting: not supported ⇒ clear error instead of breaking error ──
        if ch == "$":
            raise ValueError(DOLLAR_QUOTE_ERROR)

        # ── Separator ──
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
    """Is the text just a comment? (so that comments between statements are not executed)"""
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
