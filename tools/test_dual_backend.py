#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/test_dual_backend.py — آزمون عملی لایه‌ی `core/db` روی **هر دو** backend (فاز E.2).

چه می‌کند؟
----------
یک باتری یکسان از رفتارها را روی هر backend اجرا می‌کند و برای هر مورد یکی از این
سه وضعیت را گزارش می‌دهد:

  • OK        — کار می‌کند (اندازه‌گیری‌شده با همین اجرا)
  • EXPECTED  — پشتیبانی **نمی‌شود** و طبق طرح نباید بشود (مثل `lastrowid` روی PG)
  • FAIL      — انتظار می‌رفت کار کند ولی نشد ⇒ کد خروج ۱

SQLite روی یک فایل موقت در پوشه‌ی موقت سیستم اجرا می‌شود (به `logs.db` دست نمی‌زند)
و PostgreSQL فقط جدول `TEMP` می‌سازد (پایان session خودش می‌رود) و در پایان rollback
می‌کند ⇒ **هیچ اثری روی دیتابیس مالک نمی‌گذارد** و هیچ جدول برنامه‌ای را لمس نمی‌کند.

کد خروج
-------
  0 = هر دو backend (یا backendهای درخواستی) سالم
  1 = یک یا چند بررسی FAIL شد
  2 = PG درخواست شده ولی در دسترس نیست (نبود DSN یا سرور خاموش)

نمونه
-----
    python tools\\test_dual_backend.py                  # هر دو backend
    python tools\\test_dual_backend.py --backend sqlite  # فقط SQLite (بدون PG)
    set PG_DSN=host=127.0.0.1 port=5432 dbname=printguard user=printguard password=…
    python tools\\test_dual_backend.py --backend postgresql --quiet
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.db import (  # noqa: E402 — بعد از تنظیم sys.path
    BACKEND_POSTGRESQL,
    BACKEND_SQLITE,
    connect_postgres,
    connect_sqlite,
    resolve_dsn,
)
from core.db.exceptions import ConfigurationError, DatabaseConnectionError  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PG_UNAVAILABLE = 2

OK = "OK"
EXPECTED = "EXPECTED"
FAIL = "FAIL"

_SCRIPT_SQL = "CREATE TABLE s1 (x INT); CREATE TABLE s2 (y INT);"


class Outcome:
    """نتیجه‌ی یک بررسی روی یک backend."""

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status
        self.detail = detail


def mask(dsn: str) -> str:
    """رمز را از DSN پنهان می‌کند (هیچ‌وقت رمز چاپ نشود)."""
    return " ".join("password=***" if t.lower().startswith("password=") else t
                    for t in str(dsn).split())


# ─────────────────────────────────────────────────────────────
# باتری بررسی‌ها (مشترک بین دو backend)
# ─────────────────────────────────────────────────────────────

def check_sqlite(path: str) -> list:
    """اجرای باتری روی SQLite (فایل موقت)."""
    out = []
    conn = connect_sqlite(path, timeout=5.0)
    try:
        out.append(Outcome("اتصال (connect_sqlite)", OK,
                           f"backend={conn.backend}"))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, n INT)")

        cur = conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("alpha", 10))
        out.append(Outcome("execute + ترجمه-placeholder (سبک ?)", OK,
                           f"rowcount={cur.rowcount}"))
        out.append(Outcome("lastrowid", OK, f"id={cur.lastrowid}"))

        conn.executemany("INSERT INTO t (name, n) VALUES (?, ?)",
                         [("b", 1), ("c", 2)])
        conn.commit()
        count = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        out.append(Outcome("executemany در یک تراکنش", OK if count == 3 else FAIL,
                           f"count={count}"))

        conn.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("doomed", 0))
        conn.rollback()
        count = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        out.append(Outcome("rollback", OK if count == 3 else FAIL, f"count={count}"))

        val = conn.execute("SELECT 'نرخ 100%' || ?", ("!",)).fetchone()[0]
        out.append(Outcome("درصد متنی در کوئری", OK if val == "نرخ 100%!" else FAIL,
                           f"مقدار={val!r}"))

        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM t WHERE id = ?", (1,))
        out.append(Outcome("cursor خام (سبک خودِ درایور: ?)", OK,
                           f"count={cur.fetchone()[0]}"))

        conn.executescript(_SCRIPT_SQL)
        out.append(Outcome("executescript", OK, "دو دستور اجرا شد"))

        import sqlite3
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT id FROM t ORDER BY id").fetchone()
        out.append(Outcome("row_factory", OK if row["id"] == 1 else FAIL,
                           f"دسترسی با نام ستون ⇒ id={row['id']}"))
    except Exception as exc:  # noqa: BLE001 — ابزار، باید همه‌چیز را گزارش کند
        out.append(Outcome("اجرای باتری SQLite", FAIL, f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()
    return out


def check_postgres(dsn: str) -> list:
    """اجرای همان باتری روی PostgreSQL (فقط جدول TEMP)."""
    out = []
    conn = connect_postgres(dsn, timeout=5.0)
    try:
        out.append(Outcome("اتصال (connect_postgres)", OK,
                           f"backend={conn.backend} | tz="
                           f"{conn.execute('SHOW timezone').fetchone()[0]}"
                           f" | autocommit={conn.raw_connection.autocommit}"))
        conn.execute("CREATE TEMP TABLE t (id INTEGER, name TEXT, n INTEGER)")
        conn.commit()  # DDL در PG تراکنشی است

        cur = conn.execute("INSERT INTO t (id, name, n) VALUES (?, ?, ?)", (1, "alpha", 10))
        out.append(Outcome("execute + ترجمه-placeholder (? ⇒ %s)", OK,
                           f"rowcount={cur.rowcount}"))

        conn.executemany("INSERT INTO t (id, name, n) VALUES (?, ?, ?)",
                         [(2, "b", 1), (3, "c", 2)])
        count = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        out.append(Outcome("executemany در یک تراکنش", OK if count == 3 else FAIL,
                           f"count={count}"))
        # مثل باتری SQLite: ثبت نقطه‌ی مرجع، تا بررسی بعدی واقعاً «یک rollback» بسنجد
        # (در PG برخلاف sqlite3، هیچ DMLای خودکار commit نمی‌شود).
        conn.commit()

        conn.execute("INSERT INTO t (id, name, n) VALUES (?, ?, ?)", (4, "doomed", 0))
        conn.rollback()
        count = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        out.append(Outcome("rollback", OK if count == 3 else FAIL, f"count={count}"))

        val = conn.execute("SELECT 'نرخ 100%' || ?", ("!",)).fetchone()[0]
        out.append(Outcome("درصد متنی در کوئری (escape ⇒ %%)",
                           OK if val == "نرخ 100%!" else FAIL, f"مقدار={val!r}"))

        new_id = conn.execute(
            "INSERT INTO t (id, name, n) VALUES (?, ?, ?) RETURNING id",
            (9, "ret", 9)).fetchone()[0]
        out.append(Outcome("شناسه‌ی درج با RETURNING id", OK if new_id == 9 else FAIL,
                           f"id={new_id}"))

        try:
            _ = conn.lastrowid
            out.append(Outcome("lastrowid", FAIL,
                               "بدون خطا برگشت؛ روی PG باید NotImplementedError بدهد"))
        except NotImplementedError as exc:
            out.append(Outcome("lastrowid", EXPECTED, f"طبق طرح: {str(exc)[:60]}…"))

        # E.4.7: اسکریپت چنددستوری روی PG هم اجرا می‌شود (تقسیم به statement).
        try:
            conn.executescript(_SCRIPT_SQL)
            made = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name IN ('s1', 's2')"
            ).fetchone()[0]
            out.append(Outcome("executescript (تقسیم به statement)",
                               OK if made == 2 else FAIL, f"جدول‌های ساخته‌شده={made}/2"))
        except NotImplementedError as exc:              # دیگر نباید رخ دهد
            out.append(Outcome("executescript (تقسیم به statement)", FAIL,
                               f"هنوز پیاده نشده: {str(exc)[:60]}…"))

        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM t WHERE id = %s", (1,))
        out.append(Outcome("cursor خام (سبک خودِ درایور: %s)", OK,
                           f"count={cur.fetchone()[0]}"))

        conn.row_factory = object()          # باید بی‌اثر باشد، نه خطا
        out.append(Outcome("row_factory (بی‌اثر به‌عمد)", OK,
                           f"خواندن ⇒ {conn.row_factory}"))

        conn.rollback()
    except Exception as exc:  # noqa: BLE001 — ابزار، باید همه‌چیز را گزارش کند
        out.append(Outcome("اجرای باتری PostgreSQL", FAIL, f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()
    return out


# ─────────────────────────────────────────────────────────────
# گزارش
# ─────────────────────────────────────────────────────────────

def print_block(title: str, outcomes: list) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))
    if not outcomes:
        print("   (اجرا نشد)")
        return
    for o in outcomes:
        mark = {OK: "✔", EXPECTED: "◇", FAIL: "✗"}[o.status]
        print(f"   {mark} [{o.status:8}] {o.name}" + (f"  — {o.detail}" if o.detail else ""))


def summarize(outcomes: list) -> tuple:
    """(تعداد OK، تعداد EXPECTED، تعداد FAIL)"""
    return (sum(1 for o in outcomes if o.status == OK),
            sum(1 for o in outcomes if o.status == EXPECTED),
            sum(1 for o in outcomes if o.status == FAIL))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="test_dual_backend.py",
        description="آزمون عملی core/db روی SQLite و PostgreSQL (فاز E.2).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="کد خروج: 0=سالم، 1=بررسی ناموفق، 2=PG در دسترس نیست\n")
    ap.add_argument("--backend", choices=(BACKEND_SQLITE, BACKEND_POSTGRESQL, "both"),
                    default="both", help="کدام backend آزمون شود (پیش‌فرض: both)")
    ap.add_argument("--dsn", default="", help="نشانی PG (پیش‌فرض: PG_DSN)")
    ap.add_argument("--sqlite-path", default="",
                    help="مسیر فایل SQLite (پیش‌فرض: فایل موقت که پس از آزمون پاک می‌شود)")
    ap.add_argument("--quiet", action="store_true", help="فقط یک خط خلاصه")
    args = ap.parse_args(argv)

    want_sqlite = args.backend in (BACKEND_SQLITE, "both")
    want_pg = args.backend in (BACKEND_POSTGRESQL, "both")

    if not args.quiet:
        env_backend = os.getenv("DB_BACKEND", "").strip() or "(ست نشده ⇒ پیش‌فرض sqlite)"
        print("──── test_dual_backend ────")
        print(f"DB_BACKEND محیط: {env_backend}")
        print("توجه: این ابزار هر دو backend را مستقل آزمون می‌کند؛ DB_BACKEND لازم نیست عوض شود.")

    # ── SQLite ────────────────────────────────────────────────
    sqlite_results = []
    tmpdir = None
    if want_sqlite:
        path = args.sqlite_path
        if not path:
            tmpdir = tempfile.mkdtemp(prefix="pg_dual_")
            path = os.path.join(tmpdir, "dual_test.db")
        sqlite_results = check_sqlite(path)

    # ── PostgreSQL ────────────────────────────────────────────
    pg_results, pg_error, pg_dsn_display = [], None, ""
    if want_pg:
        try:
            dsn = resolve_dsn(BACKEND_POSTGRESQL, args.dsn or None)  # خالی ⇒ ConfigurationError
            pg_dsn_display = mask(dsn)
            pg_results = check_postgres(dsn)
        except ConfigurationError as exc:
            pg_error = f"نشانی اتصال نیست: {exc}"
        except DatabaseConnectionError as exc:
            pg_error = f"اتصال برقرار نشد: {exc}"
        except Exception as exc:  # noqa: BLE001
            pg_error = f"{type(exc).__name__}: {exc}"

    # ── گزارش ─────────────────────────────────────────────────
    if not args.quiet:
        if want_sqlite:
            print_block("SQLite", sqlite_results)
        if want_pg:
            if pg_error:
                print("\n── PostgreSQL " + "─" * 47)
                print(f"   ✗ [UNAVAILABLE] آزمون نشد — {pg_error}")
            else:
                print_block(f"PostgreSQL ({pg_dsn_display})", pg_results)

    all_results = sqlite_results + pg_results
    n_ok, n_expected, n_fail = summarize(all_results)

    if args.quiet:
        if pg_error:
            print(f"[dual] sqlite={len(sqlite_results)} بررسی | PostgreSQL در دسترس نیست: {pg_error}")
        else:
            print(f"[dual] sqlite={len(sqlite_results)} بررسی | pg={len(pg_results)} بررسی "
                  f"| OK={n_ok} EXPECTED={n_expected} FAIL={n_fail}")
    else:
        print("\n── خلاصه " + "─" * 50)
        print(f"   OK={n_ok}  |  EXPECTED(پشتیبانی‌نشده‌ی عمدی)={n_expected}  |  FAIL={n_fail}"
              + (f"  |  PG آزمون نشد: {EXIT_PG_UNAVAILABLE} (غیرقابل‌دسترس)" if pg_error else ""))
        print("   یادآوری: تنها جلوه‌ی عمدیِ پشتیبانی‌نشده روی PostgreSQL «lastrowid» است؛")
        print("   جایگزین آن ``insert_returning_id`` است (E.4.3 — docs/phase-e-plan.md).")

    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if n_fail:
        return EXIT_FAIL
    if want_pg and pg_error:
        return EXIT_PG_UNAVAILABLE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
