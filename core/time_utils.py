# -*- coding: utf-8 -*-
# =============================================================================
# @file:         core/time_utils.py
# @brief:        Central time helper: the only place that produces the standard UTC 'Z' timestamp
#                format.
# @details:      Responsibilities: Create, parse and convert UTC timestamps and local-time strings.
#                Main API: utc_now_iso(), to_utc_z(), parse_utc(), to_local_str(), local_tz(),
#                reset_display_tz_cache(), as_utc_datetime(), to_filter_bound() (+2 more)
#                Side effects: none at import time
# @dependencies: stdlib only
# @notes:        Stored timestamps must come from here: UTC, Z suffix, microsecond precision.
# =============================================================================

"""
Central Time Helper — **the only source of UTC standard format production** in the project (Phase E.3).

Standard format (definitive decision of phase E.3)
-----------------------------------
    2026-10-03T12:00:00.123456Z
    └──────── 26 characters + Z ────────┘ ⇒ fixed length 27

Rules:
  • Always UTC (not the server's local time);
  • Always suffix ``Z'' (not ``+00:00'');
  • always 6 microsecond digits (so even when microsecond is zero, ``.000000'' comes);
  • Fixed length ⇒ **String comparison is safe in SQL and Python** (collation
    word for word is the same with chronological order). This is what the columns are
    Today's ``TEXT'' and ``TIMESTAMPTZ'' in phase E rely on it.

Why ``Z'' and not ``+00:00''? ``datetime.isoformat()'' on UTC-aware datetime
produces ``+00:00'' and ``strftime("%...Z")'' (previously in
``cartridge_change_engine'' was used) gives a ``Z'' suffix. Equalize on ``Z''
It has two advantages: fixed length of 27 (the ``+00:00'' format is also 27, but with ``+'' which is compared to
A string messes up the order if mixed with other formats) and the readability of the convention.

Consumption contract
------------
  • **Authors** only use:func:`utc_now_iso` (never ``datetime.now()``
    raw, never ``utcnow()'').
  • **compare with database** (cutoff/debounce/prune) with ``to_utc_z(...)'' on datetime
    Be aware that the format is the same as the saved data.
  • **read database strings** with:func:`parse_utc` (rejects strict: naive).
  • **range filter from user input** with:func:`to_filter_bound` (naive/date only ⇒ local).
  • **Sorting rows in Python** with:func:`to_sort_key` (not string comparison).
  • **day grouping** with:func:`to_local_day` (local day, not UTC day).
  • **Display in UI** with:func:`to_local_str` (convert to ``Asia/Tehran``).

The meaning of historical disciplines (before E.3) and their migration
-------------------------------------------------
The interpretation rule is locked in ``docs/timezone-contract.md'' §8.3 and the tool
``tools/migrate_timestamps_to_utc.py'' implements the same: naive ⇒ local time
``Asia/Tehran''; ``Z'' before cutover sign ⇒ fake label (local time);
``Z'' after cutover ⇒ Actual UTC.

``tzdata'' dependency
-------------------
``ZoneInfo'' on Windows does not have a system time zone database, and the ``tzdata'' package
It is dependent (paragraph 2 of the ``docs/timezone-contract.md'' document). This dependency in
Added ``requirements.txt'' phase E.3 to ``Asia/Tehran'' on Windows server as well
to work Intentionally no fixed offset (``+03:30'') is placed: historical data
It would move the DST period (2021/2022) silently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# The main reference of the datetime class — is locked once on import.
# Why is it necessary? Tests (eg `tests/golden_harness.py') include the ``datetime'' class
# Domain modules are patched with a subclass of "lockclock"; if:func:`to_utc_z`
# For isinstance use the patchable name of the module, actual datetime instances
# are rejected ("to_utc_z only accepts datetime, not datetime"). This alias is the same
# Keeps the main class so that validate remains true.
_DATETIME_BASE = datetime

#: display time zone (Persian UI/RTL). The default is the same as the project contract.
DEFAULT_DISPLAY_TZ = "Asia/Tehran"

#: fixed length of the standard string: `2026-10-03T12:00:00.123456Z`
ISO_Z_LENGTH = 27

#: standard string format (6 digits of microseconds + Z).
ISO_Z_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now_iso() -> str:
    """
    Current time in standard project format: ``2026-10-03T12:00:00.123456Z''.

    **The only producer of "now" in the program code.** Any author who directly
    writing ``datetime.now()'' or ``datetime.utcnow()'' breaks the contract.
    """
    return datetime.now(timezone.utc).strftime(ISO_Z_FORMAT)


def to_utc_z(dt: datetime) -> str:
    """
    Converts any **aware** ``datetime'' to standard format (by converting to UTC).

    Args:
        dt: datetime with ``tzinfo'' (any locale; conversion is done automatically).

    Raises:
        ValueError: if ``dt'' is naive. It is a deliberate error and not an assumption
            "Local time": silent assumption of the same 3:30 minute bug D.6 in the replay data
            does For historical naive strings, run the migration tool.
    """
    if not isinstance(dt, _DATETIME_BASE):
        raise ValueError(f"to_utc_z فقط datetime می‌پذیرد، نه {type(dt).__name__}")
    if dt.tzinfo is None:
        raise ValueError(
            "naive datetime — نیاز به timezone دارد. برای زمان حال از utc_now_iso() "
            "و برای زمان محاسبه‌شده از datetime.now(timezone.utc) استفاده کنید "
            "(تبدیل داده‌ی قدیمی: tools/migrate_timestamps_to_utc.py).")
    return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT)


def parse_utc(ts: str) -> datetime:
    """
    Returns standard string (or any ISO with offset) to ``datetime'' aware.

    Accepted examples:
        ``2026-10-03T12:00:00.123456Z`` · ``2026-10-03T12:00:00+00:00`` ·
        ``2026-10-03T15:30:00+03:30``

    Args:
        ts: time string.

    Raises:
        ValueError: empty, nonparsable, or **naive** string (no offset). Strictness
            Intentional: In read paths, naive means data that has not been migrated
            Its conjectural interpretation is more dangerous than a clear error.
    """
    s = (ts or "").strip()
    if not s:
        raise ValueError("رشته‌ی زمان خالی است")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"رشته‌ی زمان قابل تجزیه نیست: {ts!r} ({exc})") from exc
    if dt.tzinfo is None:
        raise ValueError(
            f"رشته‌ی زمان naive است: {ts!r} — قرارداد پروژه زمان UTC با پسوند Z یا "
            "offset صریح است. اگر این مقدار از دیتابیس قدیمی می‌آید، "
            "tools/migrate_timestamps_to_utc.py را اجرا کنید.")
    return dt


def to_local_str(ts: str, tz_name: str = DEFAULT_DISPLAY_TZ) -> str:
    """
    For display in UI: Converts standard string to local time.

    Args:
        ts: time string (standard or any ISO with offset).
        tz_name: Display time zone name (default ``Asia/Tehran'').

    Returns:
        ``YYYY-MM-DD HH:MM:SS'' to local time (no offset — for display).
    """
    return parse_utc(ts).astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def local_tz(tz_name: str = DEFAULT_DISPLAY_TZ) -> ZoneInfo:
    """Display time zone (for consumers who convert themselves)."""
    return ZoneInfo(tz_name)


def reset_display_tz_cache() -> None:
    """
    Clear timezone cache — only for tests that change ``TZ''.

    ``ZoneInfo'' caches results at the process level; This function after changing ``TZ''
    (if necessary) is called. It has no effect on the logic of the program.
    """
    ZoneInfo.clear_cache()


def as_utc_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    readable auxiliary: string ⇒ datetime aware; ``None''/Empty ⇒ ``None''.

    For optional fields (like ``last_login_at'') that may be NULL.
    """
    if value is None or not str(value).strip():
        return None
    return parse_utc(value)


def to_filter_bound(value: Optional[str], tz_name: str = DEFAULT_DISPLAY_TZ) -> Optional[str]:
    """
    Time range filter boundary (user input from query string) ⇒ standard ``Z'' format.

    Why is this function necessary? interval filters (``/api/logs/all?start=…``) as
    **string comparison** are performed on the ``timestamp'' column; So user input too
    It must be the same format as the saved data. Three input modes, all three are accepted:

      • ``2026-10-03T09:30:00.000000Z'' or ``…+03:30'' (aware) ⇒ convert to UTC/Z;
      • ``2026-10-03T09:30:00'' (naive) and ``2026-10-03'' (date only) ⇒ **hour
        The locale** is assumed to be ``Asia/Tehran'' (same as time contract rule §8.3 for
        naive historical data) and then converted to UTC/Z;
      • Everything else ⇒ **Unchanged** is returned + warning in log.

    Why do we "assume local" for user input and not a hard error like:func:`parse_utc`?
    Because this is the border of human input (manual filter/browser) and the server is on Windows server
    Tehran local time works; Hardening of:func:`parse_utc` for *database data*
    is not a request parameter. An invalid value does not intentionally throw an exception: behavior
    The history of the endpoint (passing the raw string to the comparison) is kept until this step
    Do not change behavior in API.
    """
    if value is None or not str(value).strip():
        return value
    s = str(value).strip()
    try:
        return to_utc_z(parse_utc(s))
    except ValueError:
        pass
    try:
        naive = datetime.fromisoformat(s)
    except ValueError:
        log.warning("مرز فیلتر زمانی قابل تجزیه نیست و بدون تغییر رد شد: %r", value)
        return value
    if naive.tzinfo is not None:  # pragma: no cover - defense branch (top covers)
        return to_utc_z(naive)
    return to_utc_z(naive.replace(tzinfo=ZoneInfo(tz_name)))


#: Smallest sort-aware basis — for empty/invalid values (first-come-first-served).
_SORT_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def to_local_day(ts: str, tz_name: str = DEFAULT_DISPLAY_TZ) -> str:
    """
    The **local** day returns a stored timestamp: ``YYYY-MM-DD''.

    The Python equivalent of ``substr(timestamp, 1, 10)'' in SQLite, which was used until phase D
    It would have given the local day (because the naive data was local). After E.3 data
    stored is UTC/Z; Cutting the first 10 characters gives the day of **UTC** and the basket of daily statistics
    moves to 3:30 ⇒ daily grouping must pass this function.
    """
    return parse_utc(ts).astimezone(ZoneInfo(tz_name)).date().isoformat()


def to_sort_key(value: Optional[str]) -> datetime:
    """
    Time sort key for stored strings.

    String order on standard format (fixed length 27) is correct, but as soon as you enter a
    The unmigrated row (``+00:00'' or naive) breaks the order; This value function
    **parses** so that the order is always chronological. Empty or invalid value ⇒
    :data:`_SORT_EPOCH` (ie ``oldest'') so that the sort never throws an exception.
    """
    try:
        return parse_utc(value)
    except ValueError:
        return _SORT_EPOCH


__all__ = [
    "DEFAULT_DISPLAY_TZ",
    "ISO_Z_FORMAT",
    "ISO_Z_LENGTH",
    "utc_now_iso",
    "to_utc_z",
    "parse_utc",
    "to_local_str",
    "local_tz",
    "as_utc_datetime",
    "to_filter_bound",
    "to_local_day",
    "to_sort_key",
    "reset_display_tz_cache",
]
