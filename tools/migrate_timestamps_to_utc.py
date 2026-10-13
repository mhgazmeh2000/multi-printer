#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/migrate_timestamps_to_utc.py — مهاجرت timestampهای SQLite به فرمت استاندارد UTC-Z (فاز E.3).

چه می‌کند؟
----------
همهٔ مقادیر زمانی جدول‌های برنامه را به **فرمت استاندارد پروژه**
(``2026-10-03T12:00:00.123456Z`` — ۲۷ کاراکتر، همیشه UTC، همیشه ``Z``) تبدیل می‌کند.
قاعدهٔ تفسیر، همان قاعدهٔ قفل‌شدهٔ ``docs/timezone-contract.md`` §۸.۳ است و
دسته‌بندی از **همان تابع** ابزار E.0.2 (``tools/analyze_old_events.py::classify``)
می‌آید تا دو ابزار هرگز از هم جدا نشوند:

| مقدار ورودی | تفسیر | خروجی |
|---|---|---|
| ``2026-10-01T08:51:39.937749`` (naive) | ساعت محلی ``Asia/Tehran`` | ``2026-10-01T05:21:39.937749Z`` |
| ``…Z`` پیش از نشانهٔ cutover | برچسب جعلی (باگ D.6) ⇒ ساعت محلی | تبدیل‌شده |
| ``…Z`` پس از نشانهٔ cutover | UTC واقعی | نرمال‌شده به فرمت استاندارد |

⚠ **نقطهٔ کور شناخته‌شدهٔ این قاعده:** ردیف جعلی‌ای که در **۳:۳۰ پیش از** لحظهٔ cutover
واقعی نوشته شده، مقدار محلی‌اش (که ۳:۳۰ جلوتر است) از نشانه عبور می‌کند و «UTC واقعی»
تشخیص داده می‌شود. این محدودیت در سند ``docs/timezone-contract.md`` §۸.۳ پذیرفته شده
(نشانه، لحظهٔ استقرار نسخهٔ اصلاح‌شده است) و عدد آن با ابزار E.0.2
(``tools/analyze_old_events.py``) پیش از مهاجرت دیده می‌شود؛ در DB این مخزن صفر ردیف
``Z`` وجود دارد، پس اثر عملی ندارد.
| ``2026-10-03T09:55:51+00:00`` | offset صریح ⇒ همان‌طور (UTC واقعی) | استانداردشده |
| ``2026-10-03T11:00:00+03:30`` | offset صریح ⇒ همان‌طور (اندازه‌گیری‌شده و در manifest شمرده می‌شود) | استانداردشده |
| خراب / تجزیه‌ناپذیر | **دست‌نخورده** + هشدار | skip |

idempotency (چرا اجرای دوباره چیزی را خراب نمی‌کند)
---------------------------------------------------
قاعدهٔ «``Z`` پیش از cutover ⇒ جعلی» فقط برای دادهٔ **قدیمی** درست است، ولی ردیف‌های
مهاجرت‌شده هم می‌توانند زمانی پیش از cutover داشته باشند (کل تاریخ!). تفکیک با
**فرمت** انجام می‌شود، نه با زمان:

  • ``Z`` با ۶ رقم میکروثانیه (``.ffffff``) ⇒ فرمت استاندارد E.3 ⇒ **UTC واقعی** ⇒ no-op؛
  • ``Z`` بدون میکروثانیه ⇒ نویسنده‌های قدیمی موتور کارتریج
    (``strftime("%Y-%m-%dT%H:%M:%SZ")``) ⇒ قاعدهٔ cutover اعمال می‌شود.

پس اجرای دوباره: هر ردیف در دستهٔ ``already_standard`` می‌افتد و هیچ UPDATEای زده
نمی‌شود (تست دارد).

ایمنی
-----
  • ``--dry-run``: فقط شمارش، بدون هیچ نوشتنی (و بدون بکاپ)؛
  • پیش از هر نوشتن: بکاپ ``<db>-<YYYYmmdd-HHMMSS>.e3bak`` (نام‌گذاری با ``logs.db-*``
    سازگار است ⇒ در ``.gitignore`` می‌افتد و git status را آلوده نمی‌کند)؛
  • کل به‌روزرسانی در **یک تراکنش**؛ هر خطای غیرمنتظره ⇒ rollback و کد خروج ۱؛
  • ``meta.value`` دست‌نخورده می‌ماند: نشانهٔ cutover خودش مرجع تفسیر است و نباید
    تبدیل شود (فقط ``meta.updated_at`` مثل بقیه تبدیل می‌شود).

کدهای خروج
----------
  0 = مهاجرت انجام شد (یا dry-run). ردیف‌های زباله/خالی فقط هشدارند و در manifest می‌آیند.
  1 = خطای اجرایی (DB نبود/نخواندنی، خطای نوشتن — با rollback)
  2 = نیازمند اقدام: نشانهٔ cutover نیست، یا ردیف ``Z`` بدون نشانهٔ قابل‌اعتماد
      (``z_cutover_unknown``) داریم ⇒ تفسیر خودکار ممکن نیست.

نمونه
-----
    python tools\\migrate_timestamps_to_utc.py --db logs.db --dry-run
    python tools\\migrate_timestamps_to_utc.py --db logs.db
    python tools\\migrate_timestamps_to_utc.py --db logs.db --manifest e3-manifest.json --quiet
    python tools\\migrate_timestamps_to_utc.py --db logs.db --backup-only
    python tools\\migrate_timestamps_to_utc.py --db logs.db --pg-dsn "host=… dbname=printguard" --backup-only

بکاپ (E.6.2)
------------
پیش از نوشتن، بکاپ گرفته می‌شود و **هیچ‌کدام از دو مسیر بی‌صدا رد نمی‌شود**:

* **SQLite** (همیشه): کپی فیزیکی کنار فایل، الگوی ``logs.db-<stamp>.e3bak`` — همان
  رفتار پیشین، بدون تغییر.
* **PostgreSQL**: اگر DB_BACKEND=postgresql و ``PG_DSN`` ست باشد (یا ``--pg-dsn`` داده
  شود) ``pg_dump --format=custom`` در ``printguard-pg-<stamp>.dump`` و سپس **تأیید** با
  ``pg_restore --list``: dump خالی/نیمه/بی‌جدول ⇒ خطای صریح و توقف. نبودِ ``pg_dump`` هم
  خطاست (با پیام نصب); ``--skip-pg-backup`` تنها راه آگاهانهٔ بی‌گذراندن آن است.
* ``--backup-only``: فقط بکاپ (PG: dump تأییدشده / SQLite: کپی) و خروج، بدون اسکن و
  بدون هیچ نوشتنی روی دیتابیس.

sha256 بکاپ PG در manifest (کلید ``pg_backup`` با toc/tables/sha256) ثبت می‌شود؛ رمز
libpq به ``PGPASSWORD`` منتقل می‌شود تا در آرگومان خط فرمان و manifest دیده نشود.
دلیل این طراحی: یافتهٔ E.4.11 (بند ۷.۱ docs/postgresql-strictness.md) نشان داد «بکاپ»
روی PG می‌توانست بی‌صدا **هیچ** باشد و همان‌طور گزارش شود.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.time_utils import DEFAULT_DISPLAY_TZ, ISO_Z_FORMAT  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

CUTOVER_KEY = "timezone_cutover_at"

#: ستون‌های زمانی هر جدول — از DDL واقعی `logs.db` استخراج شده‌اند
#: (`PRAGMA table_info`)، نه از حافظه. اگر جدولی/ستونی در DB نباشد، هشدار داده
#: می‌شود و رد می‌شود (سازگاری با نسخه‌های قدیمی‌تر schema).
TIME_COLUMNS = {
    "logs": ["timestamp"],
    "security_events": ["timestamp", "created_at"],
    "toner_history": ["timestamp"],
    "sensor_readings": ["timestamp"],
    "printer_counters": ["updated_at"],
    "cartridge_state": ["updated_at", "learned_at", "last_refill_at"],
    "cartridge_yield_profiles": ["updated_at"],
    "yield_samples": ["created_at"],
    "toner_snapshots_v2": ["timestamp"],
    "users": ["created_at", "updated_at", "last_login_at"],
    "meta": ["updated_at"],
}

#: ستون‌هایی که عمداً دست‌نخورده می‌مانند (مرجع تفسیر، نه دادهٔ تاریخی).
PROTECTED_COLUMNS = {("meta", "value")}

#: `Z` با ۶ رقم میکروثانیه ⇒ فرمت استاندارد E.3.
_STANDARD_Z = re.compile(r"\.\d{6}Z$")

TEHRAN = ZoneInfo(DEFAULT_DISPLAY_TZ)


def _load_classify():
    """
    تابع ``classify`` را از ابزار E.0.2 می‌آورد (یک پیاده‌سازی، دو مصرف‌کننده).

    چرا import فایل‌محور: پوشهٔ ``tools/`` پکیج نیست؛ با ``spec_from_file_location``
    همان ماژول مستقل بار می‌شود و اگر روزی منطق دسته‌بندی عوض شود، هر دو ابزار
    با هم عوض می‌شوند (نه از هم جدا).
    """
    path = PROJECT_ROOT / "tools" / "analyze_old_events.py"
    spec = importlib.util.spec_from_file_location("analyze_old_events", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify


classify = _load_classify()


# ─────────────────────────────────────────────────────────────
# تبدیل مقادیر
# ─────────────────────────────────────────────────────────────

def split_awareness(raw: str) -> tuple:
    """
    (رشته، دسته) را برای یک مقدار خام برمی‌گرداند؛ جدا از قاعدهٔ E.3 برای idempotency.

    Returns:
        ``(نوع, مقدار)`` که نوع یکی است از:
        ``standard`` (فرمت استاندارد E.3 — no-op)، ``naive``، ``z``، ``offset``،
        ``empty``، ``unparsable``.
    """
    ts = (raw or "").strip()
    if not ts:
        return "empty", ts
    if ts.endswith(("Z", "z")):
        if _STANDARD_Z.search(ts):
            return "standard", ts
        return "z", ts
    if re.search(r"[+-]\d{2}:?\d{2}$", ts[10:]):
        return "offset", ts
    try:
        datetime.fromisoformat(ts)
    except ValueError:
        return "unparsable", ts
    return "naive", ts


def convert_value(raw: str, cutover: datetime | None) -> tuple:
    """
    یک مقدار زمانی را به فرمت استاندارد تبدیل می‌کند.

    Args:
        raw: مقدار خام ستون.
        cutover: لحظهٔ نشانهٔ cutover (آگاه به UTC) برای تفکیک ``Z`` جعلی از واقعی.

    Returns:
        ``(new_value, category, changed)`` — ``new_value`` برای مقادیر
        دست‌نیافتنی همان مقدار خام است (هیچ حذفی انجام نمی‌شود).
    """
    kind, ts = split_awareness(raw)
    if kind == "empty":
        return raw, "empty", False
    if kind == "standard":
        # فرمت استاندارد ⇒ UTC واقعی؛ صرفاً نرمال‌سازی (معمولاً هیچ تغییری نیست)
        dt = datetime.strptime(ts, ISO_Z_FORMAT).replace(tzinfo=timezone.utc)
        new = dt.strftime(ISO_Z_FORMAT)
        return new, "already_standard", False
    if kind == "unparsable":
        return raw, "unparsable", False
    if kind == "naive":
        dt = datetime.fromisoformat(ts).replace(tzinfo=TEHRAN)   # ساعت محلی نصب
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "naive_local", True
    if kind == "offset":
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "offset_aware", True
    # kind == "z": برچسب Z قدیمی → قاعدهٔ cutover
    category = classify(ts, cutover)
    if category == "z_cutover_unknown":
        return raw, "z_cutover_unknown", False
    if category == "z_before_cutover":
        # برچسب جعلی: مقدار در واقع ساعت محلی بوده
        naive = datetime.fromisoformat(ts[:-1])
        dt = naive.replace(tzinfo=TEHRAN)
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "z_before_cutover", True
    if category == "z_after_cutover":
        dt = datetime.fromisoformat(ts[:-1] + "+00:00")
        return dt.astimezone(timezone.utc).strftime(ISO_Z_FORMAT), "z_after_cutover", True
    return raw, "unparsable", False


# ─────────────────────────────────────────────────────────────
# کمکی‌های DB
# ─────────────────────────────────────────────────────────────

def connect_ro(path: str) -> sqlite3.Connection:
    """اتصال فقط-خواندنی (تحلیل بدون هیچ خطر نوشتن)."""
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def table_columns(conn: sqlite3.Connection, table: str) -> list:
    try:
        conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1')
    except sqlite3.Error:
        return []
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def read_cutover(conn: sqlite3.Connection) -> tuple:
    """
    نشانهٔ cutover را از جدول ``meta`` می‌خواند.

    Returns:
        ``(datetime|None, str)`` — مقدار و منبع/دلیل.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (CUTOVER_KEY,)).fetchone()
    except sqlite3.Error:
        return None, "جدول meta نیست"
    if not row or not row[0]:
        return None, f"کلید {CUTOVER_KEY} در meta نیست"
    raw = str(row[0]).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"مقدار نشانه قابل تجزیه نیست: {raw!r}"
    if dt.tzinfo is None:
        return None, f"مقدار نشانه naive است: {raw!r}"
    return dt.astimezone(timezone.utc), f"meta.{CUTOVER_KEY} = {raw}"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────
# مهاجرت
# ─────────────────────────────────────────────────────────────

def scan(db_path: str, cutover: datetime, tables: list) -> dict:
    """شمارش دسته‌ها بدون هیچ نوشتنی (پایهٔ dry-run و manifest)."""
    report = {"tables": {}, "totals": {}, "skipped_samples": []}
    conn = connect_ro(db_path)
    try:
        for table in tables:
            cols = table_columns(conn, table)
            if not cols:
                report["tables"][table] = {"present": False}
                continue
            entry = {"present": True, "columns": {}, "rows": 0,
                     "converted": 0, "skipped": 0, "already_standard": 0,
                     "needs_decision": 0}
            wanted = [c for c in TIME_COLUMNS[table] if c in cols]
            missing = [c for c in TIME_COLUMNS[table] if c not in cols]
            if missing:
                entry["missing_columns"] = missing
            for col in wanted:
                if (table, col) in PROTECTED_COLUMNS:
                    continue
                cats = {}
                for (value,) in conn.execute(f'SELECT "{col}" FROM "{table}"'):
                    _, category, changed = convert_value(value, cutover)
                    cats[category] = cats.get(category, 0) + 1
                    if category == "already_standard":
                        entry["already_standard"] += 1
                    elif changed:
                        entry["converted"] += 1
                    elif category in ("unparsable", "z_cutover_unknown"):
                        entry["skipped"] += 1
                        if category == "z_cutover_unknown":
                            entry["needs_decision"] += 1
                        if len(report["skipped_samples"]) < 20:
                            report["skipped_samples"].append(
                                {"table": table, "column": col, "value": value,
                                 "category": category})
                entry["columns"][col] = cats
            n_rows = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            entry["rows"] = n_rows
            report["tables"][table] = entry
    finally:
        conn.close()
    for key in ("rows", "converted", "skipped", "already_standard", "needs_decision"):
        report["totals"][key] = sum(t.get(key, 0) for t in report["tables"].values())
    return report


def migrate_rows(db_path: str, cutover: datetime, tables: list) -> dict:
    """اجرای تبدیل در **یک تراکنش**؛ خروجی: دیکشنری افزوده‌های هر جدول."""
    applied = {}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN")
        for table in tables:
            cols = table_columns(conn, table)
            if not cols:
                continue
            wanted = [c for c in TIME_COLUMNS[table] if c in cols
                      and (table, c) not in PROTECTED_COLUMNS]
            if not wanted:
                continue
            # rowid برای شناسایی ردیف؛ جدول‌های WITHOUT ROWID نداریم (DDL پروژه).
            select = ", ".join(f'"{c}"' for c in wanted)
            rows = conn.execute(f'SELECT rowid, {select} FROM "{table}"').fetchall()
            updates = 0
            for row in rows:
                rowid, values = row[0], row[1:]
                for col, value in zip(wanted, values):
                    new_value, _, changed = convert_value(value, cutover)
                    if changed and new_value != value:
                        conn.execute(
                            f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?',
                            (new_value, rowid))
                        updates += 1
            applied[table] = updates
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return applied


def make_backup(db_path: str) -> str:
    """
    بکاپ فیزیکی پیش از نوشتن.

    نام پیش‌فرض ``<db>-<YYYYmmdd-HHMMSS>.e3bak`` است؛ چون با الگوی
    ``logs.db-*`` در ``.gitignore`` می‌افتد، ``git status`` آلوده نمی‌شود.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"{db_path}-{stamp}.e3bak"
    shutil.copy2(db_path, backup)
    return backup


class BackupError(RuntimeError):
    """بکاپ گرفته/تأیید نشد — با پیام عملیاتی (نه traceback)."""


def _tool_path(name: str) -> str:
    from shutil import which

    found = which(name)
    if not found:
        raise BackupError(
            f"ابزار `{name}` در PATH نیست. روی Debian/Ubuntu: "
            f"`apt install postgresql-client-16` (یا نسخهٔ منطبق با سرور).")
    return found


def _env_without_password(dsn: str) -> dict:
    """
    رمز را از DSN به محیط منتقل می‌کند تا در `ps`/shell history دیده نشود.

    libpq پیش از خواندن DSN، متغیرهای محیطی را می‌بیند؛ پس PGPASSWORD تنها راه
    رساندن رمز به pg_dump/pg_restore بدون نوشتنش در آرگومان خط فرمان است.
    """
    env = dict(os.environ)
    m = re.search(r"(?:^|\s)password=(\S+)", dsn)
    if m:
        env["PGPASSWORD"] = m.group(1).strip("'\"")
        dsn = re.sub(r"(?:^|\s)password=\S+", " ", dsn).strip()
        env["_PG_DUMP_DSN"] = dsn          # DSN بدون رمز، برای لاگ‌کردن
    else:
        env["_PG_DUMP_DSN"] = dsn
    env.setdefault("PGCONNECT_TIMEOUT", "15")
    return env, dsn


def _discard_unverified(path: str) -> None:
    """فایل بکاپِ تأییدنشده را پاک می‌کند؛ اگر نشد، لااوه هشدار می‌دهد."""
    try:
        os.remove(path)
    except OSError as exc:
        print(f"⚠ فایل بکاپِ نامعتبر پاک نشد ({path}): {exc}", file=sys.stderr)


def verify_pg_dump(path: str, *, expect_tables=()) -> list:
    """
    با `pg_restore --list` مطمئن می‌شود فایل واقعاً یک dump قابل‌بازگردانی است.

    چرا این-stage: خروجی موفق `pg_dump -Fc` تنها «فایلِ بزرگ» است؛ یک dump نیمه‌cut
    (دیسک پر، سرور ریستارت‌شده) هم size دارد و هم همین‌جا رد می‌شود. `expect_tables`
    هم اختیاری است: اگر داده شد، نبودِ هر نام ⇒ خطا (پس «dumpِ خالی از schema اشتباه»
    هم از این دروازه رد نمی‌شود).
    """
    pg_restore = _tool_path("pg_restore")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise BackupError(f"فایل dump خالی یا ناموجود است: {path}")
    proc = subprocess.run([pg_restore, "--list", path], capture_output=True, text=True,
                          timeout=300)
    if proc.returncode != 0:
        raise BackupError(
            f"pg_restore --list خطا داد (dump معتبر نیست): {proc.stderr.strip()[:300]}")
    # قالب خط TOC در pg_restore --list:  <entry>; <catalog> <oid> TABLE <schema> <name> <owner>
    # (در pg_dump متن/سادهٔ جدول و «TABLE DATA» جداست؛ خط دوم عمداً رد می‌شود، وگرنه
    # نامِ اشتباه به‌جای جدول برگردانده می‌شد.)
    entries = re.findall(r"^\s*\d+;\s+\d+\s+\d+\s+TABLE(?!\s+DATA)\s+\S+\s+(\S+)",
                         proc.stdout, re.M)
    if not entries:
        raise BackupError("dump هیچ جدولی ندارد — schema هدف را بررسی کنید "
                          "(search_path/نقش/dbname)")
    missing = sorted(set(expect_tables) - set(entries))
    if missing:
        raise BackupError(f"جدول‌های موردانتظار در dump نیستند: {', '.join(missing)}")
    return entries


def take_pg_backup(dsn: str, out_dir: str, *, expect_tables=()) -> dict:
    """
    `pg_dump --format=custom` + تأیید با `pg_restore --list`.

    چرا custom و نه plain: `-Fc` فشرده است، با `pg_restore -t/-n` قابل انتخاب
    برگردانده می‌شود، و — مهم‌تر برای یک شاهد عملیاتی — با `--list` بدون اتصال به
    سرور قابل بازرسی است. نام فایل `printguard-pg-<UTC stamp>.dump` در `out_dir`.
    """
    pg_dump = _tool_path("pg_dump")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(out_dir, f"printguard-pg-{stamp}.dump")
    env, safe_dsn = _env_without_password(dsn)
    try:
        proc = subprocess.run([pg_dump, "--format=custom", "--file", out_path, safe_dsn],
                              capture_output=True, text=True, timeout=1800, env=env)
        if proc.returncode != 0:
            raise BackupError(
                f"pg_dump ناموفق بود: {proc.stderr.strip()[:300] or '(بدون stderr)'}")
        entries = verify_pg_dump(out_path, expect_tables=expect_tables)
    except BackupError:
        # `pg_dump -Fc` فایل را **پیش از** موفقیت می‌سازد؛ پس یک اجرای نیمه‌کاره
        # (دیسک پر، اتصال قطع، اتصال رد) فایلی ۰‌بایتی یا ناقص جا می‌گذارد. اگر
        # بماند، فردا کسی آن را در `ls printguard-pg-*.dump` می‌بیند و «بکاپ»
        # فرض می‌کند — همان خطای «بکاپِ جعلی» که در E.4.11 ثبت شد.
        _discard_unverified(out_path)
        raise
    return {
        "kind": "pg_dump_custom",
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "sha256": sha256_of(out_path),
        "toc_entries": len(entries),
        "tables": sorted(set(entries)),
        "dsn": env.get("_PG_DUMP_DSN", ""),        # بدون رمز — برای manifest
        "taken_at": datetime.now(timezone.utc).strftime(ISO_Z_FORMAT),
    }


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="migrate_timestamps_to_utc.py",
        description="مهاجرت timestampهای SQLite به فرمت استاندارد UTC-Z (فاز E.3).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="کد خروج: 0=انجام شد، 1=خطای اجرایی، 2=نیازمند اقدام (نبود نشانه یا ردیف Z مبهم)\n")
    ap.add_argument("--db", default="", help="مسیر فایل SQLite (پیش‌فرض: config.settings.DB_PATH)")
    ap.add_argument("--cutover", default="",
                    help="نشانهٔ cutover (ISO آگاه)؛ پیش‌فرض: خواندن از جدول meta")
    ap.add_argument("--tables", default="", help="فهرست جدول‌ها با کاما (پیش‌فرض: همه)")
    ap.add_argument("--dry-run", action="store_true", help="فقط شمارش؛ بدون بکاپ و بدون نوشتن")
    ap.add_argument("--no-backup", action="store_true", help="بدون بکاپ (پیش‌فرض: بکاپ می‌گیرد)")
    ap.add_argument("--pg-dsn", default="",
                    help="اگر دیتابیس زنده PostgreSQL است: نشانی libpq برای "
                         "pg_dump -Fc تأییدشده (پیش‌فرض: از PG_DSN وقتی DB_BACKEND=postgresql)")
    ap.add_argument("--backup-out", default="",
                    help="پوشهٔ خروجی بکاپ PG (پیش‌فرض: پوشهٔ جاری)")
    ap.add_argument("--backup-only", action="store_true",
                    help="فقط بکاپ (PG: pg_dump تأییدشده / SQLite: copy فیزیکی) و خارج شدن")
    ap.add_argument("--skip-pg-backup", action="store_true",
                    help="با وجود PG_DSN، بکاپ PG نگیر (برای dry-run روی کپی؛ حالت پیش‌فرض: رد)")
    ap.add_argument("--manifest", default="", help="مسیر فایل manifest JSON")
    ap.add_argument("--quiet", action="store_true", help="خروجی کوتاه")
    return ap.parse_args(argv)


def _pg_dsn_from_env() -> str:
    """``PG_DSN`` فقط وقتی که خودِ برنامه روی postgresql تنظیم شده باشد."""
    if (os.environ.get("DB_BACKEND", "") or "").strip().lower() not in ("postgresql", "postgres", "pg"):
        return ""
    return (os.environ.get("PG_DSN", "") or "").strip()


def _resolve_db_path(args) -> str:
    """``--db`` وگرنه ``config.settings.DB_PATH`` (lazy import مثل پیش از این)."""
    db_path = (args.db or "").strip()
    if db_path:
        return db_path
    from config import settings
    return getattr(settings, "DB_PATH", "logs.db")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        db_path = _resolve_db_path(args)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ مسیر دیتابیس معلوم نشد: {exc}", file=sys.stderr)
        return EXIT_ERROR

    # ── E.6.2: بکاپ PostgreSQL، تأییدشده با pg_restore --list ─────────
    # این ابزار فایل SQLite را بازنویسی می‌کند؛ پس «دیتابیس زنده» وقتی PG است،
    # در معرض خطرِ *واردشدنِ* دادهٔ تغییرکرده قرار می‌گیرد، نه در معرض نوشتن
    # مستقیم. تصمیم: پیش از هر کاری، یک شاهدِ قابل‌بازگردانی از PG بگیر. اگر
    # pg_dump نبود یا خطا داد ⇒ اجرا **متوقف** می‌شود (هیچ بکاپ بی‌صدایی
    # «گرفته‌شده» حساب نمی‌شود؛ همان درسی که از findings E.4.11 گرفته شد).
    pg_backup = None
    dsn = (args.pg_dsn or "").strip() or ("" if args.skip_pg_backup else _pg_dsn_from_env())
    if dsn:
        # پیش‌فرضِ پوشهٔ بکاپ: **کنار فایل دیتابیس**، نه پوشهٔ جاری — در غیر این صورت
        # یک اجرای معمولی از ریشهٔ مخزن، بکاپ باینری را وسط working tree می‌ریزد
        # (این را خودِ سوئیت PG در E.6.2 نشان داد: ۲۶ فایل dump در ریشه).
        pg_out_dir = args.backup_out or os.path.dirname(os.path.abspath(db_path))
        try:
            pg_backup = take_pg_backup(dsn, pg_out_dir)
        except BackupError as exc:
            print(f"✗ بکاپ PostgreSQL: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if not args.quiet:
            print(f"✔ بکاپ PG: {pg_backup['path']} | {pg_backup['bytes']} بایت | "
                  f"{pg_backup['toc_entries']} ورودی TOC | sha256={pg_backup['sha256'][:16]}…")
        if args.backup_only:
            if args.manifest:
                _write_manifest(args.manifest, db_path, {"pg_backup": pg_backup})
            return EXIT_OK

    if args.backup_only:
        if not os.path.exists(db_path):
            print(f"✗ فایل دیتابیس پیدا نشد: {db_path}", file=sys.stderr)
            return EXIT_ERROR
        backup = make_backup(db_path)
        if os.path.abspath(backup) == os.path.abspath(db_path):
            print("✗ بکاپ هم‌نام خودِ دیتابیس است؛ متوقف شد.", file=sys.stderr)
            return EXIT_ERROR
        print(f"✔ بکاپ SQLite: {backup} | sha256={sha256_of(backup)[:16]}… "
              f"(مقابل فایل: {'مطابق' if sha256_of(backup) == sha256_of(db_path) else 'DIFF!'}، "
              f"بدون هیچ نوشتنی روی DB)")
        if args.manifest:
            _write_manifest(args.manifest, db_path, {"sqlite_backup": {
                "kind": "file_copy", "path": backup, "sha256": sha256_of(backup)}})
        return EXIT_OK

    if not os.path.exists(db_path):
        print(f"✗ فایل دیتابیس پیدا نشد: {db_path}", file=sys.stderr)
        return EXIT_ERROR

    tables = [t.strip() for t in args.tables.split(",") if t.strip()] or list(TIME_COLUMNS)

    # ── نشانهٔ cutover ─────────────────────────────────────────
    conn = connect_ro(db_path)
    try:
        marker, marker_src = read_cutover(conn)
    finally:
        conn.close()
    if args.cutover.strip():
        try:
            marker = datetime.fromisoformat(args.cutover.strip().replace("Z", "+00:00"))
            if marker.tzinfo is None:
                raise ValueError("naive")
            marker = marker.astimezone(timezone.utc)
            marker_src = f"--cutover = {args.cutover.strip()}"
        except ValueError:
            print(f"✗ مقدار --cutover قابل تجزیه نیست یا naive است: {args.cutover!r}",
                  file=sys.stderr)
            return EXIT_NEEDS_ACTION
    if marker is None:
        print("✗ نشانهٔ cutover در دست نیست ⇒ تفسیر ردیف‌های Z ممکن نیست.\n"
              f"  دلیل: {marker_src}\n"
              "  اقدام: python tools/set_cutover_marker.py --db <db>  یا  --cutover <ISO آگاه>",
              file=sys.stderr)
        return EXIT_NEEDS_ACTION

    if not args.quiet:
        print("──── migrate_timestamps_to_utc ────")
        print(f"DB      : {db_path}")
        print(f"cutover : {marker.isoformat()}  ({marker_src})")
        print(f"حالت    : {'dry-run (بدون نوشتن)' if args.dry_run else 'اجرای واقعی'}")

    # ── اسکن ──────────────────────────────────────────────────
    report = scan(db_path, marker, tables)
    if not args.quiet:
        print("\n── دسته‌بندی مقادیر ─────────────────────────────")
        for table, entry in report["tables"].items():
            if not entry.get("present"):
                print(f"   {table:24} (در DB نیست — رد شد)")
                continue
            cats = {}
            for col_cats in entry["columns"].values():
                for k, v in col_cats.items():
                    cats[k] = cats.get(k, 0) + v
            summary = ", ".join(f"{k}={v}" for k, v in sorted(cats.items())) or "—"
            print(f"   {table:24} rows={entry['rows']:<5} {summary}")
        tot = report["totals"]
        print(f"\n   مجموع: rows={tot['rows']} converted={tot['converted']} "
              f"already_standard={tot['already_standard']} skipped={tot['skipped']} "
              f"needs_decision={tot['needs_decision']}")
        for sample in report["skipped_samples"]:
            print(f"   ⚠ دست‌نخورده ({sample['category']}): "
                  f"{sample['table']}.{sample['column']} = {sample['value']!r}")

    if report["totals"]["skipped"] and not args.dry_run:
        print(f"\n⚠ {report['totals']['skipped']} ردیف دست‌نخورده ماند (زباله/مبهم)؛ "
              "همه در manifest فهرست شده‌اند تا بازبینی دستی شوند.", file=sys.stderr)

    # ── dry-run ───────────────────────────────────────────────
    if args.dry_run:
        manifest = dict(report)
        manifest["mode"] = "dry-run"
        manifest["cutover"] = marker.isoformat()
        manifest["db"] = os.path.abspath(db_path)
        manifest["sha256_before"] = sha256_of(db_path)
        manifest_path = _write_manifest(args.manifest, db_path, manifest, verbose=not args.quiet)
        if not args.quiet:
            print("\n✔ dry-run: هیچ تغییری اعمال نشد.")
        return _exit_code(report)

    if report["totals"]["converted"] == 0:
        manifest = dict(report)
        manifest.update({"mode": "no-op", "cutover": marker.isoformat(),
                         "db": os.path.abspath(db_path),
                         "sha256_before": sha256_of(db_path)})
        manifest_path = _write_manifest(args.manifest, db_path, manifest, verbose=not args.quiet)
        if args.quiet:
            print(f"[migrate] هیچ تبدیلی لازم نبود (rows={report['totals']['rows']}، "
                  f"همه استاندارد/دست‌نخورده) manifest={manifest_path or '—'}")
        else:
            print("\n✔ هیچ تبدیلی لازم نبود (idempotent).")
        return _exit_code(report)

    # ── بکاپ + اجرا ───────────────────────────────────────────
    backup = None
    if not args.no_backup:
        backup = make_backup(db_path)
        if not args.quiet:
            print(f"\nبکاپ    : {backup}")

    sha_before = sha256_of(db_path)
    applied = migrate_rows(db_path, marker, tables)

    manifest = dict(report)
    manifest.update({
        "mode": "applied",
        "db": os.path.abspath(db_path),
        "cutover": marker.isoformat(),
        "backup": backup,
        "updates_per_table": applied,
        "pg_backup": pg_backup,
        "sha256_before": sha_before,
        "sha256_after": sha256_of(db_path),
    })
    manifest["totals"]["updated_cells"] = sum(applied.values())
    manifest_path = _write_manifest(args.manifest, db_path, manifest, verbose=False)

    if args.quiet:
        print(f"[migrate] converted={report['totals']['converted']} "
              f"updated_cells={manifest['totals']['updated_cells']} "
              f"skipped={report['totals']['skipped']} backup={backup or '—'} "
              f"manifest={manifest_path or '—'}")
    else:
        print("\n── نتیجه ────────────────────────────────────────")
        for table, n in applied.items():
            if n:
                print(f"   {table:24} {n} سلول به‌روزرسانی شد")
        print(f"\n✔ مهاجرت انجام شد | converted={report['totals']['converted']} "
              f"skipped={report['totals']['skipped']} "
              f"needs_decision={report['totals']['needs_decision']}")
        if manifest_path:
            print(f"   manifest: {manifest_path}")

    return _exit_code(report)


def _exit_code(report: dict) -> int:
    """
    کد خروج از نتیجهٔ اسکن.

    ``z_cutover_unknown`` (ردیف ``Z`` بدون نشانهٔ قابل‌اعتماد) ⇒ ۲: تصمیم انسانی
    لازم است. ردیف‌های زباله/خالی فقط **هشدار**اند (خروجی ۰) چون هیچ تفسیر
    معناداری برایشان وجود ندارد و دست‌نخورده‌ماندنشان بی‌خطر است؛ همه در manifest
    فهرست می‌شوند.
    """
    return EXIT_NEEDS_ACTION if report["totals"]["needs_decision"] else EXIT_OK


def _write_manifest(path: str, db_path: str, manifest: dict, verbose: bool = False) -> str | None:
    """نوشتن manifest JSON (پیش‌فرض: کنار خود دیتابیس با timestamp — TD-005)."""
    if path and path.strip():
        target = path.strip()
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = f"{db_path}.e3-manifest-{timestamp}.json"
        if os.path.exists(target):
            counter = 1
            while os.path.exists(f"{db_path}.e3-manifest-{timestamp}_{counter}.json"):
                counter += 1
            target = f"{db_path}.e3-manifest-{timestamp}_{counter}.json"
    try:
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        if verbose:
            print(f"   manifest: {target}")
        return target
    except OSError as exc:
        print(f"⚠ نوشتن manifest ممکن نشد ({exc})", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
