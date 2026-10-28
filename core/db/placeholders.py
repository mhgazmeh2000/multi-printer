# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/db/placeholders.py
# @brief:        Converts query parameter placeholders between SQLite and PostgreSQL styles.
# @details:      Responsibilities: Translate '?' placeholders to '%s' (and escape percent signs)
#                per backend.
#                Main API: escape_percent_signs(), translate_placeholders()
#                Side effects: none at import time
# @dependencies: internal: .exceptions
# @notes:        None.
# =============================================================================

"""
Query parameter placeholder translation (phase D was created, phase E.2 was activated).

All repository queries are written in SQLite style (``?``). On the same PostgreSQL
Queries must be executed with ``%s''; This module performs the same translation and from
E.2 is called in ``ConnectionAdapter._sql'' on the postgresql path (on
sqlite does not change anything and the same input string is returned).

Rule: ``?'' is translated only when **outside** the quoted string, id
double quote, linear comment (``--``) and block comment (``/* */``).

escape percentage (phase E.2)
---------------------
psycopg scans the query text at the **character** level for placeholders; So every
Only ``%'' (even inside the quote or comment string) should be ``%%'' otherwise ba
``ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders'' breaks
(measured on psycopg 3.3.6). The order of work is important: **first** escape percent,
**Next** translation of ``?'' — so that the newly created ``%s'' are not escaped again.

Note: escape **doesn't** work on backend ``sqlite'' (behavior of sqlite3 makes no sense
does not have ``%'' and changing it would destroy the query text).
"""

from __future__ import annotations

from typing import Optional

from .exceptions import PlaceholderTranslationError

#: Placeholder style of each backend. The keys of this dictionary must be with constants
#: ``core.db.connection.BACKEND_*'' remain the same (it has a guard test).
PLACEHOLDER_STYLE = {
    "sqlite": "?",
    "postgresql": "%s",
}


def _replace_question_marks(sql: str, replacement: str) -> str:
    """Replace ``?''s outside the string/id/comment."""
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]

        # Single-quoted string: '' = escape quote
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

        # Double-quoted identifier: "" = escaped double-quote
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

        # Line comment until the end of the line
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(sql[i:j])
            i = j
            continue

        # Block comment
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
    Converts any ``%'' to ``%%'' (psycopg's convention for query text).

    It is deliberately **textual** and not structural: psycopg also scans text character by character.
    So ``%'' inside the string/comment must be escaped. This function is **not** idempotent.
    (Executing it again makes ``%%%%'') because it is used only once in the translation path.
    """
    return sql.replace("%", "%%")


def translate_placeholders(sql: str, backend: str,
                           *, escape_percent: Optional[bool] = None) -> str:
    """
    Translates a query written with the ``?'' style to the style of the destination backend.

    Args:
        sql: query text.
        backend: ``"sqlite"`` (return unchanged) or ``"postgresql"``.
        escape_percent: ``None'' ⇒ backend default (postgresql: ``True'',
            sqlite: ``False''). Giving ``True'' with ``sqlite'' is an explicit error
            Don't mess up the SQLite text by mistake.

    Raises:
        PlaceholderTranslationError: unknown backend, non-string sql, or
            ``escape_percent=True'' comes with a backend that does not have this concept.
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
        escape_percent = True  # Default postgresql path
    prepared = escape_percent_signs(sql) if escape_percent else sql
    return _replace_question_marks(prepared, style)


__all__ = ["PLACEHOLDER_STYLE", "escape_percent_signs", "translate_placeholders"]
