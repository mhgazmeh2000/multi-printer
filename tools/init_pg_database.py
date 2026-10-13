#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/init_pg_database.py — ساخت دیتابیس/نقش PostgreSQL برنامه (فاز E.1).

چه می‌کند؟
----------
  ۱) بررسی می‌کند دیتابیس `printguard` هست یا نه (وضعیت فعلی را گزارش می‌دهد)؛
  ۲) اگر نبود: با **نقش ادمین** کاربر برنامه را (اگر لازم باشد) و سپس دیتابیس را
     با مشخصات قرارداد پروژه می‌سازد: `ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'
     TEMPLATE template0` و `ALTER DATABASE … SET timezone TO 'UTC'`؛
  ۳) اگر بود: **هیچ تغییری نمی‌دهد** (idempotent) و فقط وضعیت را گزارش می‌کند؛
  ۴) جدول‌ها را نمی‌سازد — آن کار Alembic است (`alembic upgrade head`).

چرا نقش ادمین لازم است؟
-----------------------
سنجش E.1.5 نشان داد نقش برنامه (`printguard`) مجوز `CREATEDB` ندارد (و نباید داشته باشد:
کمترین-مجوز). پس «ساخت دیتابیس» فقط با یک نقش ادمین ممکن است:

    --admin-dsn   یا   PG_ADMIN_DSN   یا   کاربر پیش‌فرض postgres روی همان میزبان/پورتِ هدف

رمز ادمین از `PG_ADMIN_PASSWORD` (یا داخل DSN) خوانده می‌شود؛ هیچ رمزی در کد یا سند نیست.
رمز نقش برنامه هم از `PG_APP_PASSWORD` (یا داخل DSN هدف) خوانده می‌شود و **فقط وقتی**
استفاده می‌شود که نقش وجود نداشته باشد و باید ساخته شود.

کد خروج
-------
  0 = دیتابیس آماده است (همین حالا ساخته شد یا از قبل بود)
  1 = خطای اجرا (اتصال، ساخت دیتابیس/نقش)
  2 = نیازمند اقدام انسانی (نبود psycopg، نبود DSN، نبود رمز لازم، نبود مجوز ادمین)

نمونه (ویندوز — CMD/PowerShell)
-------------------------------
    set PG_DSN=host=127.0.0.1 port=5432 dbname=printguard user=printguard password=...
    set PG_ADMIN_DSN=host=127.0.0.1 port=5432 dbname=postgres user=postgres
    set PG_ADMIN_PASSWORD=<admin-password>
    set PG_APP_PASSWORD=<strong-app-password>
    python tools\\init_pg_database.py
    python tools\\init_pg_database.py --dry-run        # فقط گزارش برنامه، بدون تغییر
"""

from __future__ import annotations

import argparse
import os
import sys

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

APP_DB = "printguard"
APP_ROLE = "printguard"
APP_ENCODING = "UTF8"
APP_COLLATE = "C"
APP_CTYPE = "C"
APP_TZ = "UTC"


# ─────────────────────────────────────────────────────────────
# DSN helpers
# ─────────────────────────────────────────────────────────────

def load_psycopg():
    try:
        import psycopg  # type: ignore
        return psycopg
    except ImportError:
        return None


def _kv_pairs(dsn: str) -> dict:
    """جفت‌های کلید=مقدار یک DSN سبک libpq (فرم URI پشتیبانی نمی‌شود)."""
    out = {}
    for token in str(dsn).split():
        if "=" in token:
            k, _, v = token.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


def mask(dsn: str) -> str:
    return " ".join("password=***" if t.lower().startswith("password=") else t
                    for t in str(dsn).split())


def app_dsn(cli: str) -> str:
    """DSN هدف (دیتابیس برنامه) از --dsn / PG_DSN / متغیرهای libpq."""
    if cli.strip():
        return cli.strip()
    dsn = os.getenv("PG_DSN", "").strip()
    if dsn:
        return dsn
    parts = [f"{k[2:].lower()}={os.getenv(k)}" for k in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")
             if os.getenv(k)]
    if os.getenv("PGPASSWORD"):
        parts.append("password=" + os.environ["PGPASSWORD"])
    return " ".join(parts)


def admin_dsn(cli: str, target: str) -> str:
    """DSN ادمین: --admin-dsn / PG_ADMIN_DSN / ساخته‌شده از میزبان-پورت هدف با کاربر postgres.

    اگر DSN انتخابی رمز نداشت، `PG_ADMIN_PASSWORD` به آن ضمیمه می‌شود (رمز هرگز چاپ نمی‌شود).
    """
    if cli.strip():
        dsn = cli.strip()
    else:
        env = os.getenv("PG_ADMIN_DSN", "").strip()
        if env:
            dsn = env
        else:
            t = _kv_pairs(target)
            host = t.get("host", "localhost")
            port = t.get("port", "5432")
            user = os.getenv("PG_ADMIN_USER", "postgres")
            dsn = f"host={host} port={port} dbname=postgres user={user}"
    return ensure_password(dsn, "PG_ADMIN_PASSWORD")


def with_dbname(dsn: str, name: str) -> str:
    """همان DSN با نام دیتابیس عوض‌شده (برای تأیید اتصال به دیتابیس هدف)."""
    tokens = str(dsn).split()
    out, replaced = [], False
    for t in tokens:
        if t.lower().startswith("dbname="):
            out.append(f"dbname={name}"); replaced = True
        else:
            out.append(t)
    if not replaced:
        out.append(f"dbname={name}")
    return " ".join(out)


def ensure_password(dsn: str, env_name: str) -> str:
    """اگر DSN رمز ندارد، از متغیر محیطی اضافه می‌کند (بدون چاپ رمز)."""
    if "password=" in dsn.lower():
        return dsn
    pwd = os.getenv(env_name, "")
    return dsn + f" password={pwd}" if pwd else dsn


# ─────────────────────────────────────────────────────────────
# کار اصلی
# ─────────────────────────────────────────────────────────────

def db_state(psycopg, target: str, admin: str, app_db: str, app_role: str) -> dict:
    """وضعیت فعلی: دیتابیس/نقش چه وضعی دارند (تا جای ممکن از دید ادمین)."""
    state = {"db_exists": None, "role_exists": None, "checked_with": None}
    try:
        with psycopg.connect(admin, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (app_db,))
                state["db_exists"] = cur.fetchone() is not None
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,))
                state["role_exists"] = cur.fetchone() is not None
                cur.execute("SELECT current_user, current_setting('is_superuser')")
                state["checked_with"] = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 — اتصال هدف ممکن است هنوز برقرار نشود
        state["admin_error"] = f"{type(exc).__name__}: {exc}"
        # تلاش دوم: از طریق هدف — `pg_database` و `pg_roles` برای هر نقشِ واردشده خواندنی‌اند،
        # پس با همین اتصال هم می‌توان «وجود» را سنجید (حتی وقتی دیتابیس هدف هنوز نیست).
        try:
            with psycopg.connect(target, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (app_db,))
                    state["db_exists"] = cur.fetchone() is not None
                    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,))
                    state["role_exists"] = cur.fetchone() is not None
                    cur.execute("SELECT current_user")
                    state["checked_with"] = cur.fetchone()[0] + " (via target)"
        except Exception as exc2:  # noqa: BLE001
            state["target_error"] = f"{type(exc2).__name__}: {exc2}"
    return state


def describe_db(psycopg, target: str) -> dict | None:
    """مشخصات دیتابیس برنامه از دید DSN هدف (اگر قابل اتصال باشد)."""
    try:
        with psycopg.connect(target, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pg_encoding_to_char(encoding), datcollate, datctype,
                           (SELECT setting FROM pg_settings WHERE name = 'TimeZone'),
                           current_user, current_database()
                    FROM pg_database WHERE datname = current_database()
                """)
                enc, collate, ctype, tz, user, db = cur.fetchone()
                return {"encoding": enc, "collate": collate, "ctype": ctype,
                        "tz": tz, "user": user, "database": db}
    except Exception:  # noqa: BLE001
        return None


def create_role_and_db(psycopg, sql, admin: str, dry_run: bool, state: dict,
                       app_db: str, app_role: str, target_dsn: str) -> tuple:
    """(ok, پیام) — ساخت نقش (در صورت نیاز) و دیتابیس با مشخصات قرارداد."""
    created = []
    if dry_run:
        return True, "dry-run: هیچ تغییری اعمال نشد"
    try:
        # CREATE DATABASE در تراکنش ممکن نیست ⇒ autocommit لازم است.
        with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
            with conn.cursor() as cur:
                # دستورهای utility در PostgreSQL پارامتر (%s) نمی‌پذیرند ⇒ از
                # psycopg.sql برای نقل‌قول ایمن شناسه‌ها و مقدارها استفاده می‌کنیم.
                if not state.get("role_exists"):
                    app_pwd = _kv_pairs(ensure_password(target_dsn, "PG_APP_PASSWORD")).get("password")
                    if not app_pwd:
                        return False, ("نقش برنامه وجود ندارد و رمزی هم ندادید ⇒ "
                                       "PG_APP_PASSWORD (یا password=… در PG_DSN) لازم است")
                    cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(app_role), sql.Literal(app_pwd)))
                    created.append(f"نقش {app_role}")
                cur.execute(sql.SQL(
                    "CREATE DATABASE {} OWNER {} ENCODING {} LC_COLLATE {} LC_CTYPE {} "
                    "TEMPLATE template0").format(
                        sql.Identifier(app_db), sql.Identifier(app_role),
                        sql.Literal(APP_ENCODING), sql.Literal(APP_COLLATE),
                        sql.Literal(APP_CTYPE)))
                created.append(f"دیتابیس {app_db}")
                cur.execute(sql.SQL("ALTER DATABASE {} SET timezone TO {}").format(
                    sql.Identifier(app_db), sql.Literal(APP_TZ)))
                created.append(f"timezone={APP_TZ} روی دیتابیس")
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        low = detail.lower()
        if ("password authentication failed" in low or "no password supplied" in low
                or "authentication" in low):
            return False, (f"احراز هویت ناموفق ({name}: {detail}) ⇒ رمز/کاربرِ DSN ادمین را "
                           "بررسی کنید (PG_ADMIN_PASSWORD / PG_ADMIN_DSN / --admin-dsn)")
        if "permission denied" in low or "must be superuser" in low:
            return False, (f"مجوز کافی نیست ({name}: {detail}) ⇒ با یک نقش ادمین وصل شوید "
                           f"(PG_ADMIN_DSN / --admin-dsn با کاربر postgres)")
        if "could not connect" in low or "connection refused" in low or "connection failed" in low:
            return False, (f"اتصال برقرار نشد ({name}: {detail}) ⇒ سرویس PostgreSQL و میزبان/پورت "
                           "را بررسی کنید")
        if "already exists" in detail.lower():
            return True, f"از قبل ساخته شده بود: {detail}"
        if name == "ReservedName" or "reserved" in detail.lower():
            return False, (f"نام نامعتبر است ({detail}) ⇒ در PostgreSQL پیشوند `pg_` "
                           "رزرو شده است؛ نام دیگری انتخاب کنید")
        return False, f"{name}: {detail}"
    return True, "ساخته شد: " + "، ".join(created)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="init_pg_database.py",
        description="ساخت دیتابیس/نقش PostgreSQL برنامه (idempotent، فاز E.1).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="کد خروج: 0=آماده، 1=خطا، 2=نیازمند اقدام (نبود درایور/DSN/رمز/مجوز)\n"
               "پس از موفقیت، جدول‌ها را بسازید:  alembic upgrade head\n")
    p.add_argument("--dsn", default="", help="DSN هدف (پیش‌فرض PG_DSN یا متغیرهای libpq)")
    p.add_argument("--admin-dsn", default="", help="DSN ادمین (پیش‌فرض PG_ADMIN_DSN یا postgres روی همان میزبان)")
    p.add_argument("--db", default=APP_DB, help="نام دیتابیس (پیش‌فرض printguard)")
    p.add_argument("--role", default=APP_ROLE, help="نام نقش (پیش‌فرض printguard)")
    p.add_argument("--dry-run", action="store_true", help="فقط گزارش وضعیت و برنامه‌ی کار؛ بدون تغییر")
    args = p.parse_args(argv)

    psycopg = load_psycopg()
    if psycopg is not None:
        from psycopg import sql as _sql
    if psycopg is None:
        print('✗ psycopg نصب نیست ⇒ python -m pip install "psycopg[binary]"', file=sys.stderr)
        return EXIT_NEEDS_ACTION

    target = app_dsn(args.dsn)
    if not target:
        print("✗ DSN هدف پیدا نشد ⇒ --dsn یا PG_DSN یا متغیرهای PGHOST/PGDATABASE/…",
              file=sys.stderr)
        return EXIT_NEEDS_ACTION
    admin = admin_dsn(args.admin_dsn, target)

    print("──── init_pg_database ────")
    print(f"هدف  : {mask(target)}")
    print(f"ادمین: {mask(admin)}" + ("" if "password=" in admin.lower()
                                                    else " (رمز از PG_ADMIN_PASSWORD)"))

    state = db_state(psycopg, target, admin, args.db, args.role)
    print(f"وضعیت: دیتابیس {args.db} → "
          + {True: "موجود", False: "نیست", None: "نامعلوم"}[state.get("db_exists")])
    print(f"        نقش {args.role} → "
          + {True: "موجود", False: "نیست", None: "نامعلوم"}[state.get("role_exists")])
    for key, label in (("admin_error", "اتصال ادمین"), ("target_error", "اتصال هدف")):
        if state.get(key):
            print(f"        ({label}: {state[key]})")

    if state.get("db_exists") is None:
        print("\n✗ وضعیت دیتابیس معلوم نشد: نه با DSN ادمین و نه با DSN هدف نتوانستم وصل شوم.\n"
              "  ⇒ PG_DSN/PG_ADMIN_DSN را درست کنید (یا سرویس PG را روشن کنید). هیچ تغییری "
              "انجام نشد.", file=sys.stderr)
        return EXIT_NEEDS_ACTION

    if state.get("db_exists"):
        info = describe_db(psycopg, target)
        print("\n✔ دیتابیس از قبل موجود است؛ هیچ تغییری لازم نیست (idempotent).")
        if info:
            print(f"   مشخصات: encoding={info['encoding']} collate={info['collate']} "
                  f"ctype={info['ctype']} tz={info['tz']} | کاربر={info['user']}")
        else:
            print("   (با DSN هدف نشد مشخصات را بخوانم؛ وضعیت دسترسی‌ها را بررسی کنید.)")
        print("\nگام بعدی: ساخت/به‌روزرسانی جدول‌ها →  alembic upgrade head")
        return EXIT_OK

    print("\n→ دیتابیس نیست؛ با نقش ادمین ساخته می‌شود "
          f"(encoding={APP_ENCODING}, collate={APP_COLLATE}, template=template0).")
    ok, msg = create_role_and_db(psycopg, _sql, admin, args.dry_run, state,
                                 args.db, args.role, target)
    if not ok:
        print(f"\n✗ ساخت انجام نشد: {msg}", file=sys.stderr)
        needs_action = any(k in msg for k in
                           ("مجوز", "رمز", "احراز هویت", "اتصال برقرار نشد", "نام نامعتبر", "رزرو"))
        return EXIT_NEEDS_ACTION if needs_action else EXIT_ERROR

    print(f"\n✔ {msg}")
    if args.dry_run:
        print("   (dry-run: هیچ اتصال/تأییدی روی دیتابیس جدید انجام نشد)")
    else:
        verify_dsn = ensure_password(with_dbname(target, args.db), "PG_APP_PASSWORD")
        info = describe_db(psycopg, verify_dsn)
        if info:
            print(f"   تأیید روی دیتابیس «{info['database']}»: encoding={info['encoding']} "
                  f"collate={info['collate']} ctype={info['ctype']} tz={info['tz']} "
                  f"| کاربر={info['user']}")
        else:
            print(f"   ⚠ با DSN هدف روی دیتابیس «{args.db}» وصل نشدم؛ رمز/مجوز نقش را بررسی "
                  "کنید (PG_APP_PASSWORD / password=… در PG_DSN).")
    print("\nگام بعدی: ساخت جدول‌ها →  alembic upgrade head")
    print("سپس:  python tools/test_pg_connection.py")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
