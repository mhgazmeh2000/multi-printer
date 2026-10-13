# -*- coding: utf-8 -*-
"""
Helper مرکزی زمان — **تنها منبع تولید فرمت استاندارد UTC** در پروژه (فاز E.3).

فرمت استاندارد (تصمیم قطعی فاز E.3)
-----------------------------------
    2026-10-03T12:00:00.123456Z
    └──────── 26 کاراکتر + Z ────────┘  ⇒ طول ثابت ۲۷

قواعد:
  • همیشه UTC (نه ساعت محلی سرور)؛
  • همیشه پسوند ``Z`` (نه ``+00:00``)؛
  • همیشه ۶ رقم میکروثانیه (پس حتی وقتی میکروثانیه صفر است، ``.000000`` می‌آید)؛
  • طول ثابت ⇒ **مقایسه‌ی رشته‌ای در SQL و پایتون امن است** (هم‌چیدمانی
    واژه‌به‌واژه‌ای با ترتیب زمانی یکی می‌شود). این همان چیزی است که ستون‌های
    ``TEXT`` امروز و ``TIMESTAMPTZ`` در فاز E به آن تکیه می‌کنند.

چرا ``Z`` و نه ``+00:00``؟ ``datetime.isoformat()`` روی datetime آگاه به UTC
``+00:00`` تولید می‌کند و ``strftime("%...Z")`` (که پیش از این در
``cartridge_change_engine`` استفاده می‌شد) پسوند ``Z`` می‌دهد. یکسان‌سازی روی ``Z``
دو مزیت دارد: طول ثابت ۲۷ (فرمت ``+00:00`` هم ۲۷ است ولی با ``+`` که در مقایسه‌ی
رشته‌ای ترتیب را به هم می‌زند اگر با فرمت‌های دیگر مخلوط شود) و خوانایی قرارداد.

قرارداد مصرف
------------
  • **نویسنده‌ها** فقط از :func:`utc_now_iso` استفاده کنند (هرگز ``datetime.now()``
    خام، هرگز ``utcnow()``).
  • **مقایسه با دیتابیس** (cutoff/debounce/prune) با ``to_utc_z(…)`` روی datetime
    آگاه ساخته شود تا هم‌فرمت با داده‌ی ذخیره‌شده باشد.
  • **خواندن رشته‌های دیتابیس** با :func:`parse_utc` (سخت‌گیر: naive را رد می‌کند).
  • **فیلتر بازه از ورودی کاربر** با :func:`to_filter_bound` (naive/تاریخ‌فقط ⇒ محلی).
  • **مرتب‌سازی ردیف‌ها در پایتون** با :func:`to_sort_key` (نه مقایسه‌ی رشته‌ای).
  • **گروه‌بندی روزانه** با :func:`to_local_day` (روزِ محلی، نه روزِ UTC).
  • **نمایش در UI** با :func:`to_local_str` (تبدیل به ``Asia/Tehran``).

معنی رشته‌های تاریخی (پیش از E.3) و مهاجرت آن‌ها
-------------------------------------------------
قاعده‌ی تفسیر در ``docs/timezone-contract.md`` §۸.۳ قفل شده است و ابزار
``tools/migrate_timestamps_to_utc.py`` همان را پیاده می‌کند: naive ⇒ ساعت محلی
``Asia/Tehran`` ؛ ``Z`` پیش از نشانه‌ی cutover ⇒ برچسب جعلی (ساعت محلی) ؛
``Z`` پس از cutover ⇒ UTC واقعی.

وابستگی ``tzdata``
-------------------
``ZoneInfo`` روی ویندوز دیتابیس منطقهٔ زمانی سیستمی ندارد و به پکیج ``tzdata``
وابسته است (بند ۲ سند ``docs/timezone-contract.md``). این وابستگی در
``requirements.txt`` فاز E.3 اضافه شده تا ``Asia/Tehran`` روی ویندوزسرور هم
کار کند. عمداً هیچ offset ثابت (``+03:30``) جای‌گزین نمی‌شود: داده‌های تاریخی
دوره‌ی DST (۲۰۲۱/۲۰۲۲) را بی‌صدا جابه‌جا می‌کرد.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# مرجعِ اصلیِ کلاس datetime — یک بار در زمان import قفل می‌شود.
# چرا لازم است؟ تست‌ها (مثل `tests/golden_harness.py`) کلاس ``datetime`` را در
# ماژول‌های دامنه با یک زیرکلاسِ «ساعت‌قفل» وصله می‌کنند؛ اگر :func:`to_utc_z`
# برای isinstance از نامِ وصله‌پذیر ماژول استفاده کند، نمونه‌های واقعیِ datetime
# رد می‌شوند («to_utc_z فقط datetime می‌پذیرد، نه datetime»). این alias همان
# کلاس اصلی را نگه می‌دارد تا validate درست بماند.
_DATETIME_BASE = datetime

#: منطقه‌ی زمانی نمایش (UI فارسی/RTL). پیش‌فرض همان قرارداد پروژه است.
DEFAULT_DISPLAY_TZ = "Asia/Tehran"

#: طول ثابت رشته‌ی استاندارد: `2026-10-03T12:00:00.123456Z`
ISO_Z_LENGTH = 27

#: قالب رشته‌ای استاندارد (۶ رقم میکروثانیه + Z).
ISO_Z_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now_iso() -> str:
    """
    زمان حال به فرمت استاندارد پروژه: ``2026-10-03T12:00:00.123456Z``.

    **تنها تولیدکننده‌ی «الآن» در کد برنامه.** هر نویسنده‌ای که مستقیم
    ``datetime.now()`` یا ``datetime.utcnow()`` بنویسد، قرارداد را می‌شکند.
    """
    return datetime.now(timezone.utc).strftime(ISO_Z_FORMAT)


def to_utc_z(dt: datetime) -> str:
    """
    هر ``datetime`` **آگاه** را به فرمت استاندارد تبدیل می‌کند (با تبدیل به UTC).

    Args:
        dt: datetime با ``tzinfo`` (هر منطقه‌ای؛ تبدیل خودکار انجام می‌شود).

    Raises:
        ValueError: اگر ``dt`` naive باشد. عمداً خطای صریح است و نه فرضِ
            «ساعت محلی»: فرض بی‌صدا همان باگ ۳:۳۰ دقیقه‌ای D.6 را در داده تکرار
            می‌کند. برای رشته‌های naive تاریخی، ابزار مهاجرت را اجرا کنید.
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
    رشته‌ی استاندارد (یا هر ISO با offset) را به ``datetime`` آگاه برمی‌گرداند.

    نمونه‌های پذیرفته‌شده:
        ``2026-10-03T12:00:00.123456Z`` · ``2026-10-03T12:00:00+00:00`` ·
        ``2026-10-03T15:30:00+03:30``

    Args:
        ts: رشته‌ی زمان.

    Raises:
        ValueError: رشته‌ی خالی، غیرقابل تجزیه، یا **naive** (بدون offset). سخت‌گیری
            عمدی است: در مسیرهای خواندن، naive یعنی داده‌ای که مهاجرت نشده و
            تفسیر حدسی‌اش خطرناک‌تر از خطای صریح است.
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
    برای نمایش در UI: رشته‌ی استاندارد را به ساعت محلی تبدیل می‌کند.

    Args:
        ts: رشته‌ی زمان (استاندارد یا هر ISO با offset).
        tz_name: نام منطقه‌ی زمانی نمایش (پیش‌فرض ``Asia/Tehran``).

    Returns:
        ``YYYY-MM-DD HH:MM:SS`` به ساعت محلی (بدون offset — برای نمایش).
    """
    return parse_utc(ts).astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def local_tz(tz_name: str = DEFAULT_DISPLAY_TZ) -> ZoneInfo:
    """منطقه‌ی زمانی نمایش (برای مصرف‌کننده‌هایی که خودشان تبدیل می‌کنند)."""
    return ZoneInfo(tz_name)


def reset_display_tz_cache() -> None:
    """
    پاک‌کردن کش منطقه‌ی زمانی — فقط برای تست‌هایی که ``TZ`` را عوض می‌کنند.

    ``ZoneInfo`` نتایج را در سطح پروسه کش می‌کند؛ این تابع پس از تغییر ``TZ``
    (اگر جایی لازم شد) فراخوانی می‌شود. روی منطق برنامه اثری ندارد.
    """
    ZoneInfo.clear_cache()


def as_utc_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    کمکی خواندنی: رشته ⇒ datetime آگاه؛ ``None``/خالی ⇒ ``None``.

    برای فیلدهای اختیاری (مثل ``last_login_at``) که ممکن است NULL باشند.
    """
    if value is None or not str(value).strip():
        return None
    return parse_utc(value)


def to_filter_bound(value: Optional[str], tz_name: str = DEFAULT_DISPLAY_TZ) -> Optional[str]:
    """
    مرزِ فیلتر بازه‌ی زمانی (ورودی کاربر از query string) ⇒ فرمت استاندارد ``Z``.

    چرا این تابع لازم است؟ فیلترهای بازه (``/api/logs/all?start=…``) به‌صورت
    **مقایسه‌ی رشته‌ای** روی ستون ``timestamp`` اجرا می‌شوند؛ پس ورودی کاربر هم
    باید هم‌فرمت داده‌ی ذخیره‌شده باشد. سه حالت ورودی، هر سه پذیرفته می‌شود:

      • ``2026-10-03T09:30:00.000000Z`` یا ``…+03:30`` (آگاه) ⇒ تبدیل به UTC/Z؛
      • ``2026-10-03T09:30:00`` (naive) و ``2026-10-03`` (فقط تاریخ) ⇒ **ساعت
        محلی** ``Asia/Tehran`` فرض می‌شود (همان قاعده‌ی قرارداد زمانی §۸.۳ برای
        داده‌های تاریخی naive) و بعد به UTC/Z تبدیل می‌شود؛
      • هر چیز دیگر ⇒ **بدون تغییر** برگردانده می‌شود + هشدار در لاگ.

    چرا برای ورودی کاربر «فرض محلی» می‌کنیم و نه خطای سخت مثل :func:`parse_utc`؟
    چون این‌جا مرزِ ورودی انسانی است (فیلتر دستی/مرورگر) و سرور روی ویندوزسرور با
    ساعت محلی تهران کار می‌کند؛ سخت‌گیریِ :func:`parse_utc` برای *داده‌ی دیتابیس*
    است، نه پارامتر درخواست. مقدار نامعتبر هم عمداً استثنا نمی‌دهد: رفتار
    تاریخی endpoint (پاس‌دادن رشته‌ی خام به مقایسه) حفظ می‌شود تا این گام هیچ
    تغییر رفتاری در API نداشته باشد.
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
    if naive.tzinfo is not None:  # pragma: no cover - شاخه‌ی دفاعی (بالا پوشش می‌دهد)
        return to_utc_z(naive)
    return to_utc_z(naive.replace(tzinfo=ZoneInfo(tz_name)))


#: کوچک‌ترین مبنای مرتب‌سازی آگاه — برای مقدارهای خالی/نامعتبر (ابتدا می‌آیند).
_SORT_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def to_local_day(ts: str, tz_name: str = DEFAULT_DISPLAY_TZ) -> str:
    """
    روزِ **محلی** یک timestamp ذخیره‌شده را برمی‌گرداند: ``YYYY-MM-DD``.

    معادل پایتونیِ ``substr(timestamp, 1, 10)`` در SQLite که تا فاز D استفاده
    می‌شد: آن هم روزِ محلی را می‌داد (چون داده naive محلی بود). پس از E.3 داده‌ی
    ذخیره‌شده UTC/Z است؛ بریدن ۱۰ کاراکتر اول روزِ **UTC** می‌دهد و سبد آمار روزانه
    را تا ۳:۳۰ جابه‌جا می‌کند ⇒ گروه‌بندی روزانه باید از این تابع بگذرد.
    """
    return parse_utc(ts).astimezone(ZoneInfo(tz_name)).date().isoformat()


def to_sort_key(value: Optional[str]) -> datetime:
    """
    کلید مرتب‌سازی زمانی برای رشته‌های ذخیره‌شده.

    ترتیب رشته‌ای روی فرمت استاندارد (طول ثابت ۲۷) درست است، ولی به‌محض ورود یک
    ردیف مهاجرت‌نشده (``+00:00`` یا naive) ترتیب می‌شکند؛ این تابع مقدار را
    **تجزیه** می‌کند تا ترتیب همیشه زمانی باشد. مقدار خالی یا نامعتبر ⇒
    :data:`_SORT_EPOCH` (یعنی «قدیمی‌ترین») تا مرتب‌سازی هرگز استثنا ندهد.
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
