#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/test_pg_connection.py — آزمون سلامت اتصال و schema در PostgreSQL (فاز E.1).

چه می‌سنجد؟
-----------
  ۱) اتصال با DSN (از `--dsn` یا `PG_DSN` یا متغیرهای استاندارد libpq)
  ۲) نسخه‌ی سرور و پورت/میزبان
  ۳) `TimeZone` نشست و سرور (هدف پروژه: UTC — `docs/timezone-contract.md`)
  ۴) encoding / collation / ctype دیتابیس (هدف: UTF8 / C / C)
  ۵) جدول‌های برنامه (۱۱ مورد) و نسخه‌ی `alembic_version` در برابر head واقعی migrationها
  ۶) شمارش سطرهای هر جدول (خالی بودن = هشدار، نه خطا)
  ۷) آزمون نوشتنِ **رولبک‌شده** (اثبات مجوز INSERT بدون باقی‌گذاشتن هیچ سطری)

کد خروج
-------
  0 = همه‌چیز سالم (حتی اگر جدول‌ها خالی باشند؛ هشدار چاپ می‌شود)
  1 = خطا (اتصال ناموفق: سرویس خاموش، رمز/نام دیتابیس غلط، شبکه)
  2 = نیازمند اقدام انسانی (psycopg نصب نیست، DSN تنظیم نشده، schema ساخته نشده،
      یا نسخه‌ی schema با head فرق دارد ⇒ اجرای migration لازم است)

نمونه
-----
    python tools/test_pg_connection.py                        # گزارش کامل
    python tools/test_pg_connection.py --quiet                # یک خط خلاصه (برای start.bat)
    python tools/test_pg_connection.py --dsn "host=... dbname=printguard ..."
    python tools/test_pg_connection.py --no-write-check       # بدون آزمون نوشتن
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

#: جدول‌های برنامه (۱۰ مورد) + جدول نشانه/نسخه‌ها
APP_TABLES = (
    "logs", "toner_history", "sensor_readings", "users", "printer_counters",
    "cartridge_state", "cartridge_yield_profiles", "yield_samples",
    "toner_snapshots_v2", "security_events",
)
EXTRA_TABLES = ("meta", "alembic_version")
EXPECTED_APP_TABLES = len(APP_TABLES)
EXPECTED_TZ = "UTC"
#: نام‌های معادل UTC در PostgreSQL (offset صفر). «UTC» و «Etc/UTC» یکی‌اند و هشدار
#: دادن برای دومی، هشدار کاذب است؛ ولی «Asia/Tehran» باید همچنان گرفته شود.
UTC_ALIASES = frozenset({"UTC", "ETC/UTC", "ETC/GMT", "GMT", "GMT0", "ZULU", "UNIVERSAL"})
EXPECTED_ENCODING = "UTF8"
EXPECTED_COLLATE = "C"

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


# ─────────────────────────────────────────────────────────────
# کمک‌کارهای DSN / درایور (بدون وابستگی به import های سنگین)
# ─────────────────────────────────────────────────────────────

def load_psycopg():
    """psycopg ۳ را برمی‌گرداند یا None."""
    try:
        import psycopg  # type: ignore
        return psycopg
    except ImportError:
        return None


def dsn_from_env() -> str:
    """DSN از `--dsn`/`PG_DSN`/متغیرهای libpq (به ترتیب اولویت)."""
    dsn = os.getenv("PG_DSN", "").strip()
    if dsn:
        return dsn
    keys = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")
    parts = [f"{k[2:].lower()}={os.getenv(k)}" for k in keys if os.getenv(k)]
    if os.getenv("PGPASSWORD"):
        parts.append("password=" + os.environ["PGPASSWORD"])
    return " ".join(parts)


def mask_dsn(dsn: str) -> str:
    """رمز را از DSN پاک می‌کند (هرگز رمز چاپ/لاگ نشود)."""
    return " ".join("password=***" if t.lower().startswith("password=") else t
                    for t in str(dsn).split())


def alembic_head() -> str | None:
    """head واقعی از فایل‌های `alembic/versions/*.py` (بدون فراخوانی alembic).

    head = ریویژنی که در هیچ فایل دیگری به‌عنوان `down_revision` نیامده باشد.
    """
    try:
        files = sorted(VERSIONS_DIR.glob("*.py"))
    except OSError:
        return None
    revs, downs = {}, set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        m_rev = re.search(r'^revision\s*=\s*["\']([^"\']+)["\']', text, re.M)
        if not m_rev:
            continue
        m_down = re.search(r'^down_revision\s*=\s*(.+)$', text, re.M)
        rev = m_rev.group(1)
        revs[rev] = f.name
        if m_down:
            for name in re.findall(r'["\']([^"\']+)["\']', m_down.group(1)):
                downs.add(name)
    heads = [r for r in revs if r not in downs]
    return heads[0] if len(heads) == 1 else (", ".join(sorted(heads)) or None)


# ─────────────────────────────────────────────────────────────
# سنجه‌ها
# ─────────────────────────────────────────────────────────────

def collect(cur, dsn: str, run_write_check: bool = True) -> dict:
    """همه‌ی سنجه‌ها را در یک ساختار برمی‌گرداند."""
    info = {"dsn": mask_dsn(dsn), "rows": {}, "missing": [], "present": [], "warnings": []}

    cur.execute("""
        SELECT version(), current_setting('server_version'),
               current_setting('server_version_num'), current_setting('port'),
               inet_server_addr()::text, current_database(), current_user
    """)
    (full, sver, sver_num, port, host, dbname, user) = cur.fetchone()
    info.update({"version_full": full, "server_version": sver,
                 "server_version_num": int(sver_num or 0), "port": port,
                 "server_addr": host, "database": dbname, "user": user})

    cur.execute("SELECT current_setting('TimeZone'), current_setting('log_timezone')")
    info["session_tz"], info["log_tz"] = cur.fetchone()

    cur.execute("""
        SELECT pg_encoding_to_char(encoding), datcollate, datctype, datallowconn
        FROM pg_database WHERE datname = current_database()
    """)
    enc, collate, ctype, allowconn = cur.fetchone()
    info.update({"encoding": enc, "collate": collate, "ctype": ctype, "allowconn": allowconn})

    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    tables = [r[0] for r in cur.fetchall()]
    info["all_tables"] = tables
    info["present"] = [t for t in APP_TABLES if t in tables]
    info["missing"] = [t for t in APP_TABLES if t not in tables]

    for t in APP_TABLES + ("meta",):
        if t in tables:
            cur.execute(f'SELECT COUNT(*) FROM "{t}"')
            info["rows"][t] = int(cur.fetchone()[0])

    if "alembic_version" in tables:
        cur.execute("SELECT version_num FROM alembic_version")
        info["alembic_versions"] = [r[0] for r in cur.fetchall()]
    else:
        info["alembic_versions"] = None

    info["alembic_head"] = alembic_head()

    if run_write_check and "meta" in tables:
        try:
            cur.execute("BEGIN")
            cur.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = EXCLUDED.updated_at",
                ("__connection_test__", "probe",
                 _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")))
            cur.execute("SELECT value FROM meta WHERE key = %s", ("__connection_test__",))
            got = cur.fetchone()
            cur.execute("ROLLBACK")
            info["write_ok"] = bool(got and got[0] == "probe")
            info["write_rolled_back"] = True
        except Exception as exc:                    # noqa: BLE001 — گزارش، نه کرش
            try:
                cur.execute("ROLLBACK")
            except Exception:                       # noqa: BLE001
                pass
            info["write_ok"] = False
            info["write_error"] = f"{type(exc).__name__}: {exc}"

    return info


def judge(info: dict) -> tuple:
    """(کد خروج, فهرست هشدارها, فهرست اقدام‌ها) — قواعد داوری در یک‌جا."""
    warnings, actions = [], []

    if (info["session_tz"] or "").upper() not in UTC_ALIASES:
        warnings.append(f"TimeZone نشست «{info['session_tz']}» است، نه {EXPECTED_TZ} "
                        f"(قرارداد زمانی پروژه؛ E.0.1)")
    if (info["log_tz"] or "").upper() not in UTC_ALIASES:
        warnings.append(f"log_timezone «{info['log_tz']}» است، نه {EXPECTED_TZ}")

    if info["encoding"] != EXPECTED_ENCODING:
        actions.append(f"encoding دیتابیس «{info['encoding']}» است، نه {EXPECTED_ENCODING} "
                       f"(نام‌های فارسی در خطرند)")
    if info["collate"] not in (EXPECTED_COLLATE, "C.UTF-8"):
        warnings.append(f"collation دیتابیس «{info['collate']}» است، نه {EXPECTED_COLLATE} "
                        f"(تصمیم فاز E — بند ۴ سند strictness)")

    if info["missing"]:
        actions.append(f"schema ساخته نشده: {len(info['missing'])} جدول از "
                       f"{EXPECTED_APP_TABLES} غایب است ({', '.join(info['missing'][:4])}"
                       f"{'…' if len(info['missing']) > 4 else ''}) ⇒ "
                       f"اجرای `alembic upgrade head`")
    elif not any(info["rows"].values()):
        warnings.append("schema هست ولی هیچ سطری در جدول‌ها نیست (دیتابیس تازه)")

    head = info.get("alembic_head")
    versions = info.get("alembic_versions")
    if not info["missing"] and versions is not None:
        if not versions:
            actions.append("جدول alembic_version خالی است (نسخه‌ی schema ثبت نشده) ⇒ "
                           "`alembic stamp head` روی دیتابیس‌های موجود یا `alembic upgrade head`")
        elif head and versions != [head]:
            actions.append(f"نسخه‌ی schema «{', '.join(versions)}» است ولی head «{head}» "
                           f"⇒ اجرای `alembic upgrade head`")

    if info.get("write_ok") is False:
        actions.append("آزمون نوشتن ناموفق بود (مجوز INSERT؟): "
                       + info.get("write_error", "بدون جزئیات"))

    if actions:
        return EXIT_NEEDS_ACTION, warnings, actions
    return EXIT_OK, warnings, actions


def report(info: dict, warnings, actions, quiet: bool) -> None:
    if quiet:
        summary = (f"[DB] PG {info['server_version']} | db={info['database']} | "
                   f"user={info['user']} | tz={info['session_tz']} | "
                   f"tables={len(info['present'])}/{EXPECTED_APP_TABLES}")
        print(summary)
        if warnings:
            print("[DB] هشدار: " + " ؛ ".join(warnings))
        if actions:
            print("[DB] اقدام لازم: " + " ؛ ".join(actions))
        return

    line = "─" * 78
    print(line)
    print("آزمون اتصال PostgreSQL — PrintGuard (فاز E.1)")
    print(line)
    print(f"DSN            : {info['dsn']}")
    print(f"سرور          : {info['version_full']}")
    print(f"نسخه          : {info['server_version']} (num={info['server_version_num']})")
    print(f"میزبان/پورت   : {info['server_addr'] or '(local)'} : {info['port']}")
    print(f"دیتابیس/کاربر : {info['database']} / {info['user']}")
    print(f"TimeZone      : نشست={info['session_tz']} | log={info['log_tz']}")
    print(f"Encoding      : {info['encoding']} | collation={info['collate']} | ctype={info['ctype']}")
    print()
    print(f"جدول‌های برنامه: {len(info['present'])}/{EXPECTED_APP_TABLES} موجود"
          + (f" | غایب: {', '.join(info['missing'])}" if info["missing"] else ""))
    for t in APP_TABLES + ("meta",):
        if t in info["rows"]:
            print(f"   {t:<26} {info['rows'][t]:>8} سطر")
        else:
            print(f"   {t:<26} {'—':>8} (جدول نیست)")
    al = info.get("alembic_versions")
    al_txt = "جدول نیست" if al is None else ("خالی (نسخه ثبت نشده)" if not al else ", ".join(al))
    print(f"   alembic_version           {al_txt}   | head فایل‌ها: {info.get('alembic_head') or '?'}")

    if "write_ok" in info:
        if info["write_ok"]:
            print("\nآزمون نوشتن: موفق ✔ (تراکنش عمداً ROLLBACK شد؛ هیچ سطری باقی نماند)")
        else:
            print(f"\nآزمون نوشتن: ناموفق ✗ {info.get('write_error', '')}")

    print()
    if warnings:
        print("هشدارها:")
        for w in warnings:
            print("   ⚠ " + w)
    if actions:
        print("اقدام‌های لازم:")
        for a in actions:
            print("   ✗ " + a)
    if not warnings and not actions:
        print("نتیجه: سالم ✔ (اتصال، تنظیمات و schema در وضعیت درست)")


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test_pg_connection.py",
        description="آزمون اتصال/تنظیمات/schema دیتابیس PostgreSQL (فاز E.1).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="کد خروج: 0=سالم، 1=خطای اتصال، 2=نیازمند اقدام (نصب نبودن درایور، "
               "نبود DSN، schema ناقص)\n"
               "مثال:\n  python tools/test_pg_connection.py\n"
               "  python tools/test_pg_connection.py --quiet        # برای start.bat\n")
    p.add_argument("--dsn", default="", help="DSN (پیش‌فرض: PG_DSN یا متغیرهای PGHOST/PGDATABASE/…)")
    p.add_argument("--quiet", action="store_true", help="یک خط خلاصه (برای start.bat)")
    p.add_argument("--no-write-check", action="store_true", help="بدون آزمون نوشتنِ رولبک‌شده")
    p.add_argument("--timeout", type=float, default=5.0, help="مهلت اتصال (ثانیه؛ پیش‌فرض ۵)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    psycopg = load_psycopg()
    if psycopg is None:
        msg = ('psycopg نصب نیست ⇒ python -m pip install "psycopg[binary]"')
        print(("[DB] " + msg) if args.quiet else ("✗ " + msg), file=sys.stderr)
        return EXIT_NEEDS_ACTION

    dsn = args.dsn.strip() or dsn_from_env()
    if not dsn:
        msg = ("نشانی اتصال پیدا نشد ⇒ --dsn یا PG_DSN یا متغیرهای PGHOST/PGDATABASE/"
               "PGUSER/PGPASSWORD لازم است")
        print(("[DB] " + msg) if args.quiet else ("✗ " + msg), file=sys.stderr)
        return EXIT_NEEDS_ACTION

    if not args.quiet:
        print(f"→ تلاش برای اتصال: {mask_dsn(dsn)} (timeout={args.timeout}s)")

    try:
        with psycopg.connect(dsn, connect_timeout=int(max(1, args.timeout))) as conn:
            with conn.cursor() as cur:
                info = collect(cur, dsn, run_write_check=not args.no_write_check)
    except Exception as exc:  # noqa: BLE001 — هر خطای اتصال باید پیام روشن بدهد
        name = type(exc).__name__
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        hints = []
        if isinstance(exc, psycopg.OperationalError):
            hints.append("سرویس PG اجرا است؟ (سرویس ویندوز: postgresql-x64-16)")
            hints.append("نام دیتابیس/کاربر/رمز در DSN درست است؟")
        print((f"[DB] اتصال به PostgreSQL ممکن نشد ({name}: {detail})")
              if args.quiet else
              f"✗ اتصال ممکن نشد: {name}: {detail}", file=sys.stderr)
        for h in hints:
            print(("  راهنما: " if not args.quiet else "[DB] ") + h, file=sys.stderr)
        return EXIT_ERROR

    code, warnings, actions = judge(info)
    report(info, warnings, actions, args.quiet)
    if not args.quiet:
        print()
        print({"0": "نتیجه‌ی نهایی: 0 (سالم)", "1": "نتیجه‌ی نهایی: 1 (خطا)",
               "2": "نتیجه‌ی نهایی: 2 (نیازمند اقدام)"}[str(code)])
    return code


if __name__ == "__main__":
    sys.exit(main())
