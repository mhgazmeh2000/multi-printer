#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/test_ddl_bootstrap.py — آزمون DDL آگاه‌به-backend (فاز E.4.5).

چه می‌سنجد؟
-----------
سه تابع bootstrap برنامه — ``core.database.init_db``،
``core.yield_engine.ensure_yield_tables`` و ``core.security_audit.init_security_audit`` —
روی هر دو backend:

  ۱) هیچ خطای نحوی/اجرایی نمی‌دهند (پیش از E.4.5، روی PG هم `PRAGMA` و هم
     `INTEGER PRIMARY KEY AUTOINCREMENT` خطای نحوی می‌دادند)؛
  ۲) **idempotent**‌اند (اجرای دوباره بی‌اثر و بی‌خطاست)؛
  ۳) روی SQLite جدول‌های خودشان را می‌سازند؛ روی PostgreSQL **هیچ DDLی اجرا
     نمی‌کنند** (Alembic تنها منبع DDL است) و فقط تأیید می‌کنند اسکیمای
     migration کامل است.

کد خروج
-------
  0 = همه‌ی بررسی‌ها سبز
  1 = خطا (تابعی شکست خورد یا انتظار برنیامد)
  2 = نیازمند اقدام انسانی (psycopg نصب نیست، DSN نیست، اسکیمای Alembic ناقص است
      ⇒ «python -m alembic upgrade head»)

نمونه
-----
    python tools/test_ddl_bootstrap.py                 # هر دو backend
    python tools/test_ddl_bootstrap.py --backend sqlite
    python tools/test_ddl_bootstrap.py --dsn "host=... dbname=printguard ..."
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_ACTION = 2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VERSIONS_DIR = ROOT / "alembic" / "versions"


# ─────────────────────────────────────────────────────────────
# گزارش‌گیری
# ─────────────────────────────────────────────────────────────
class Report:
    def __init__(self, title: str):
        self.title = title
        self.results = []          # (ok: bool, text)

    def check(self, ok, text, extra=""):
        self.results.append((bool(ok), text))
        mark = "✔" if ok else "✘"
        print(f"  {mark} {text}" + (f" — {extra}" if extra else ""))
        return bool(ok)

    def ok(self) -> bool:
        return all(r[0] for r in self.results)

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r[0])
        return f"{self.title}: {passed}/{len(self.results)}"


def _call_twice(fn, name: str, report: Report) -> bool:
    """اجرای دوباره‌ی یک تابع bootstrap: بار اول باید سبز، بار دوم بی‌خطا (idempotent)."""
    ok = True
    for idx in (1, 2):
        try:
            fn()
        except Exception as exc:                        # noqa: BLE001 — گزارش، نه کرش
            ok = report.check(False, f"{name} (اجرای {idx})",
                              f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:100]}")
            return ok
        ok = report.check(True, f"{name} (اجرای {idx}) بدون خطا")
    return ok


# ─────────────────────────────────────────────────────────────
# SQLite
# ─────────────────────────────────────────────────────────────
def test_sqlite(report: Report) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)                      # logs.db نسبی است ⇒ داخل پوشه‌ی موقت
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("PG_DSN", None)

        import core.database as database
        import core.security_audit as audit
        import core.yield_engine as yield_engine
        from core.db import db_connection
        from core.db.schema import YIELD_TABLES

        expected = ("logs", "printer_counters", "toner_history", "sensor_readings", "users",
                    "security_events", *YIELD_TABLES)

        _call_twice(database.init_db, "init_db", report)
        _call_twice(yield_engine.ensure_yield_tables, "ensure_yield_tables", report)
        _call_twice(audit.init_security_audit, "init_security_audit", report)

        with db_connection() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        missing = [t for t in expected if t not in tables]
        report.check(not missing, "جدول‌های ساخته‌شده توسط bootstrap",
                     "غایب: " + ", ".join(missing) if missing else f"{len(expected)} جدول")


# ─────────────────────────────────────────────────────────────
# PostgreSQL
# ─────────────────────────────────────────────────────────────
def alembic_head() -> str | None:
    """شناسه‌ی revision سر (بدون import کردن env.py؛ مثل tools/test_pg_connection.py)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:                                   # noqa: BLE001
        return None


def test_postgres(report: Report, dsn: str) -> int:
    """(کد خروج این بخش) — روی PG هیچ نوشتنی انجام نمی‌شود؛ فقط تأیید."""
    try:
        import importlib
        importlib.import_module("psycopg")
    except ImportError:
        print("  ! psycopg نصب نیست ⇒ بخش PostgreSQL اجرا نشد "
              "(pip install -r requirements.txt)")
        return EXIT_NEEDS_ACTION

    os.environ["DB_BACKEND"] = "postgresql"
    os.environ["PG_DSN"] = dsn

    import core.database as database
    import core.security_audit as audit
    import core.yield_engine as yield_engine
    from core.db import db_connection
    from core.db.schema import ALEMBIC_TABLE, APP_TABLES, present_tables

    # وضعیت پیش از فراخوانی‌ها (اثبات «هیچ DDLی اجرا نشد»)
    with db_connection() as conn:
        before = present_tables(conn)
        versions_before = None
        if ALEMBIC_TABLE in before:
            versions_before = [r[0] for r in
                               conn.execute(f"SELECT version_num FROM {ALEMBIC_TABLE}")
                               .fetchall()]

    _call_twice(database.init_db, "init_db", report)
    _call_twice(yield_engine.ensure_yield_tables, "ensure_yield_tables", report)
    _call_twice(audit.init_security_audit, "init_security_audit", report)

    with db_connection() as conn:
        after = present_tables(conn)
        versions_after = None
        if ALEMBIC_TABLE in after:
            versions_after = [r[0] for r in
                              conn.execute(f"SELECT version_num FROM {ALEMBIC_TABLE}")
                              .fetchall()]

    report.check(before == after, "بدون تغییر schema (DDL صفر)",
                 f"{len(after)} جدول" if before == after else
                 f"تفاوت: {sorted(after ^ before)}")
    report.check(versions_after == versions_before, "نسخه‌ی Alembic دست‌نخورده",
                 str(versions_after))

    missing = [t for t in APP_TABLES if t not in after]
    report.check(not missing, "اسکیمای Alembic کامل است",
                 "غایب: " + ", ".join(missing) if missing else f"{len(APP_TABLES)} جدول")
    report.check(ALEMBIC_TABLE in after, f"جدول {ALEMBIC_TABLE} موجود است")

    head = alembic_head()
    if head:
        report.check(versions_after == [head],
                     "نسخه‌ی دیتابیس == سرِ migrationها", f"{versions_after} vs {head}")
    else:
        report.check(True, "خواندن سرِ migrationها (اختیاری) ناموفق بود",
                     "بدون اثر روی نتیجه")

    return EXIT_NEEDS_ACTION if missing or ALEMBIC_TABLE not in after else EXIT_OK


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="آزمون DDL آگاه‌به-backend (فاز E.4.5)")
    parser.add_argument("--backend", choices=("sqlite", "postgresql", "both"),
                        default="both", help="کدام backend آزموده شود (پیش‌فرض: both)")
    parser.add_argument("--dsn", default="", help="DSN پستگرس (پیش‌فرض: PG_DSN)")
    args = parser.parse_args(argv)

    dsn = args.dsn.strip() or os.getenv("PG_DSN", "").strip()
    exit_code = EXIT_OK
    reports = []
    cwd = os.getcwd()

    if args.backend in ("sqlite", "both"):
        print("── SQLite (فایل موقت) ──")
        rep = Report("SQLite")
        try:
            test_sqlite(rep)
        except Exception as exc:                        # noqa: BLE001
            rep.check(False, "اجرای بخش SQLite", f"{type(exc).__name__}: {exc}")
        finally:
            os.chdir(cwd)
        reports.append(rep)
        if not rep.ok():
            exit_code = EXIT_ERROR

    if args.backend in ("postgresql", "both"):
        print("── PostgreSQL ──")
        if not dsn:
            print("  ! DSN ست نشده (`--dsn` یا PG_DSN) ⇒ بخش PostgreSQL اجرا نشد")
            if args.backend == "postgresql" or exit_code == EXIT_OK:
                exit_code = max(exit_code, EXIT_NEEDS_ACTION)
        else:
            rep = Report("PostgreSQL")
            try:
                code = test_postgres(rep, dsn)
            except Exception as exc:                    # noqa: BLE001
                rep.check(False, "اجرای بخش PostgreSQL", f"{type(exc).__name__}: {exc}")
                code = EXIT_ERROR
            reports.append(rep)
            if not rep.ok():
                exit_code = max(exit_code, code if code != EXIT_OK else EXIT_ERROR)

    print("\n" + " | ".join(r.summary() for r in reports))
    verdict = {EXIT_OK: "همه سالم ✔", EXIT_ERROR: "خطا ✘",
               EXIT_NEEDS_ACTION: "نیازمند اقدام (اجرای migration / تنظیم DSN / نصب psycopg)"}
    print("نتیجه:", verdict.get(exit_code, exit_code))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
