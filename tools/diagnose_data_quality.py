#!/usr/bin/env python3
"""
ابزار تشخیصی مستقل سلامت و کیفیت داده‌های پایگاه‌داده SQLite (PrintGuard Phase F).

این اسکریپت پایگاه‌داده SQLite را پیش از مایگریشن یا پس از ریکاوری (مانند sqlite3 .recover)
اسکن می‌کند و موارد زیر را ارزیابی و در قالب JSON گزارش می‌دهد:
  ۱. تطابق نوع داده: یافتن مقادیر متنی/خراب در ستون‌های عددی (INTEGER).
  ۲. تعداد سطرهای حداقلی: بررسی تعداد رکوردهای cartridge_state (حداقل ۴ به ازای هر پرینتر رنگی)
     و printer_counters.
  ۳. ذخیره خروجی تشخیصی در فایل logs.db.diagnosis.json.

خروجی مطابق قرارداد فاز F در فایلی با ساختار استاندارد ذخیره می‌شود.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("diagnose_data_quality")


def load_printer_count(printers_path: Optional[str]) -> Tuple[int, int]:
    """
    خواندن تعداد کل پرینترها و پرینترهای رنگی از فایل پیکربندی.
    بازگشت: (total_printers, color_printers)
    """
    path = Path(printers_path) if printers_path else Path("printers.json")
    if not path.is_file():
        return 0, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            total = len(data)
            color = sum(
                1
                for p in data
                if isinstance(p, dict) and p.get("device_type", "color") == "color"
            )
            return total, color
    except Exception as exc:
        log.warning("عدم موفقیت در خواندن %s: %s", path, exc)
    return 0, 0


def connect_sqlite_ro(source_path: str) -> sqlite3.Connection:
    """اتصال قطعی فقط-خواندنی به پایگاه‌داده SQLite."""
    p = Path(source_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"فایل دیتابیس در مسیر '{source_path}' یافت نشد.")
    uri = f"file:{p.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_sqlite_tables(conn: sqlite3.Connection) -> List[str]:
    """فهرست جدول‌های موجود در SQLite بدون جداول سیستمی."""
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [r[0] for r in cur.fetchall()]


def scan_table_data_quality(
    conn: sqlite3.Connection,
    table: str,
    expected_min_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """
    اسکن عمیق یک جدول از نظر نوع داده و تعداد سطرها.
    """
    cur = conn.cursor()

    # شمارش سطرهای فعلی
    cur.execute(f'SELECT COUNT(*) FROM "{table}"')
    actual_rows = int(cur.fetchone()[0])

    table_report: Dict[str, Any] = {
        "actual_rows": actual_rows,
        "issues": [],
    }
    if expected_min_rows is not None:
        table_report["expected_min_rows"] = expected_min_rows

    # بررسی کسری سطرها در صورت تعریف حد انتظار
    if expected_min_rows is not None and actual_rows < expected_min_rows:
        table_report["issues"].append(
            {
                "issue_type": "row_count_deficit",
                "message": (
                    f"تعداد ردیف‌های جدول {table} ({actual_rows}) کمتر از حداقل انتظار "
                    f"({expected_min_rows}) است. ممکن است دیتابیس در زمان recovery سطرهایی را از دست داده باشد."
                ),
                "expected_min_rows": expected_min_rows,
                "actual_rows": actual_rows,
            }
        )

    if actual_rows == 0:
        return table_report

    # دریافت متادیتای ستون‌ها
    try:
        cur.execute(f'PRAGMA table_xinfo("{table}")')
        xinfo = cur.fetchall()
    except Exception:
        cur.execute(f'PRAGMA table_info("{table}")')
        xinfo = cur.fetchall()

    int_cols: List[str] = []
    pk_col = xinfo[0][1] if xinfo else "rowid"
    for col_meta in xinfo:
        c_name = col_meta[1]
        c_type = (col_meta[2] or "").upper()
        is_pk = bool(col_meta[5]) if len(col_meta) > 5 else False
        if is_pk:
            pk_col = c_name
        if any(t in c_type for t in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT")):
            int_cols.append(c_name)

    # اسکن مقادیر نامعتبر در ستون‌های عددی
    for col in int_cols:
        query = f'''
            SELECT "{pk_col}", "{col}", typeof("{col}")
            FROM "{table}"
            WHERE "{col}" IS NOT NULL AND typeof("{col}") IN ('text', 'blob')
        '''
        try:
            cur.execute(query)
            raw_invalid = cur.fetchall()
        except Exception as exc:
            log.warning("[%s] خطا در اسکن ستون %s: %s", table, col, exc)
            continue

        for r in raw_invalid:
            val = r[1]
            # بررسی اینکه آیا به صورت عدد صحیح قابل تفسیر است یا نه
            is_valid_numeric = False
            try:
                int(val)
                is_valid_numeric = True
            except (ValueError, TypeError):
                is_valid_numeric = False

            if not is_valid_numeric:
                pk_val = str(r[0])
                table_report["issues"].append(
                    {
                        "row_ip": pk_val,
                        "column": col,
                        "expected_type": "integer",
                        "actual_type": "text",
                        "value": str(val),
                    }
                )

    return table_report


def diagnose_database(
    db_path: str,
    printers_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    اجرای عیب‌یابی جامع روی پایگاه‌داده SQLite و تولید ساختار استاندارد گزارش.
    """
    conn = connect_sqlite_ro(db_path)
    tables = get_sqlite_tables(conn)
    total_printers, color_printers = load_printer_count(printers_path)

    report_tables: Dict[str, Any] = {}
    total_issues_count = 0

    for table in tables:
        expected_min: Optional[int] = None
        if table == "cartridge_state" and total_printers > 0:
            # هر پرینتر رنگی معمولاً ۴ کارتریج و تک‌رنگ ۱ کارتریج دارد
            expected_min = (color_printers * 4) + (total_printers - color_printers)
        elif table == "printer_counters" and total_printers > 0:
            expected_min = total_printers

        t_res = scan_table_data_quality(conn, table, expected_min_rows=expected_min)
        report_tables[table] = t_res
        total_issues_count += len(t_res["issues"])

    conn.close()

    full_report = {
        "tables": report_tables,
        "summary": {
            "database": str(Path(db_path).resolve()),
            "total_tables": len(tables),
            "tables_with_issues": sum(1 for t in report_tables.values() if t["issues"]),
            "total_issues": total_issues_count,
            "healthy": total_issues_count == 0,
        },
    }
    return full_report


def print_terminal_report(report: Dict[str, Any]) -> None:
    """چاپ جدول خلاصه‌ی تشخیصی در کنسول."""
    print("=" * 80)
    print(" گزارش عیب‌یابی و ارزیابی کیفیت داده‌ها (Data Quality Diagnosis)")
    print("=" * 80)
    print(f"{'جدول':<28} | {'ردیف فعلی':<10} | {'حداقل انتظار':<12} | {'مشکلات':<10}")
    print("-" * 80)

    for tbl, info in report["tables"].items():
        actual = info.get("actual_rows", 0)
        exp = str(info.get("expected_min_rows", "—"))
        issues_cnt = len(info.get("issues", []))
        status_str = f"✔ سالم" if issues_cnt == 0 else f"✖ {issues_cnt} مورد"
        print(f"{tbl:<28} | {actual:<10} | {exp:<12} | {status_str}")

    print("=" * 80)
    sm = report["summary"]
    print(
        f"وضعیت کلی: {'✔ کاملاً سالم و معتبر' if sm['healthy'] else '⚠ اشکالاتی یافت شد'} "
        f"| کل مشکلات: {sm['total_issues']} | جداول دارای اشکال: {sm['tables_with_issues']}/{sm['total_tables']}"
    )
    print("=" * 80)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ابزار مستقل ارزیابی کیفیت و سلامت داده‌های دیتابیس SQLite"
    )
    parser.add_argument(
        "--db",
        default="logs.db",
        help="مسیر فایل دیتابیس SQLite جهت ارزیابی (پیش‌فرض: logs.db)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="مسیر ذخیره گزارش JSON (پیش‌فرض: <db>.diagnosis.json یا logs.db.diagnosis.json)",
    )
    parser.add_argument(
        "--printers",
        default="printers.json",
        help="مسیر فایل تنظیمات پرینترها جهت محاسبه تعداد سطرهای مورد انتظار (پیش‌فرض: printers.json)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="عدم نمایش جدول خلاصه در خروجی استاندارد",
    )
    args = parser.parse_args(argv)

    out_path = args.output
    if not out_path:
        out_path = f"{args.db}.diagnosis.json" if args.db != "logs.db" else "logs.db.diagnosis.json"

    try:
        report = diagnose_database(args.db, printers_path=args.printers)
    except Exception as exc:
        log.error("خطا در عیب‌یابی دیتابیس: %s", exc)
        return 2

    # ذخیره در فایل JSON
    try:
        Path(out_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.quiet:
            log.info("گزارش تشخیصی با موفقیت در %s ذخیره شد.", out_path)
    except Exception as exc:
        log.error("خطا در ذخیره فایل گزارش %s: %s", out_path, exc)
        return 2

    if not args.quiet:
        print_terminal_report(report)

    return 0 if report["summary"]["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
