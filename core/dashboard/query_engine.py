"""
PrintGuard 3-Level Query Engine (Phase J.8)
Supports Prebuilt, Visual, and SQL query modes with strict security,
parameterization, whitelisting, and execution metrics.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core.datasources.base import DataSourceError, DataSourceSecurityError

IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_]+$")

PROHIBITED_SQL_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|EXEC|EXECUTE|CALL|VACUUM|INTO)\b",
    re.IGNORECASE,
)

ALLOWED_TABLES = {
    "daily_stats",
    "print_events",
    "cartridge_lifecycle",
    "sensor_readings",
    "logs",
    "printer_counters",
    "toner_snapshots_v2",
    "users",
    "security_events",
}

VALID_AGGREGATIONS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}
VALID_FILTER_OPS = {"=", "!=", ">", "<", ">=", "<=", "LIKE", "ILIKE"}


# ─── Prebuilt Catalog ─────────────────────────────────────────────────────────

PREBUILT_QUERIES: Dict[str, Dict[str, Any]] = {
    "printer_pages_summary": {
        "id": "printer_pages_summary",
        "title": "خلاصه صفحات چاپ‌شده (روزانه)",
        "description": "روند روزانه چاپ صفحات به تفکیک کل، تک‌رنگ و رنگی از جدول daily_stats",
        "default_params": {"days": 30, "printer_ip": ""},
        "param_schema": {
            "days": {"type": "integer", "default": 30, "title": "تعداد روزها"},
            "printer_ip": {"type": "string", "default": "", "title": "آدرس IP پرینتر (اختیاری)"},
        },
    },
    "toner_levels_latest": {
        "id": "toner_levels_latest",
        "title": "آخرین وضعیت مخازن تونر ناوگان",
        "description": "سطوح فعلی تونر تمام دستگاه‌ها جهت پایش و هشدارهای مصرفی",
        "default_params": {"min_level": 0, "max_level": 100},
        "param_schema": {
            "min_level": {"type": "integer", "default": 0, "title": "حداقل درصد"},
            "max_level": {"type": "integer", "default": 100, "title": "حداکثر درصد"},
        },
    },
    "sensor_readings_recent": {
        "id": "sensor_readings_recent",
        "title": "سنجه‌های محیطی سنسورها (دما و رطوبت)",
        "description": "روند میانگین دما و رطوبت ثبت‌شده در اتاق سرور و تجهیزات",
        "default_params": {"days": 7},
        "param_schema": {
            "days": {"type": "integer", "default": 7, "title": "تعداد روزها"},
        },
    },
    "cartridge_lifecycles": {
        "id": "cartridge_lifecycles",
        "title": "چرخه عمر و بازدهی کارتریج‌ها",
        "description": "صفحات چاپ‌شده و بازدهی واقعی کارتریج‌های تعویض‌شده",
        "default_params": {"limit": 20},
        "param_schema": {
            "limit": {"type": "integer", "default": 20, "title": "حداکثر سطرها"},
        },
    },
    "security_alerts_active": {
        "id": "security_alerts_active",
        "title": "رخدادهای امنیتی و هشدارهای ورود",
        "description": "آخرین رخدادهای ورود ناموفق و هشدارهای امنیتی سامانه",
        "default_params": {"limit": 25},
        "param_schema": {
            "limit": {"type": "integer", "default": 25, "title": "تعداد رخدادها"},
        },
    },
}


def get_prebuilt_catalog() -> List[Dict[str, Any]]:
    """Returns the list of available prebuilt queries and metadata."""
    return list(PREBUILT_QUERIES.values())


# ─── Query Builders ───────────────────────────────────────────────────────────

def build_prebuilt_sql(query_id: str, params: Dict[str, Any]) -> Tuple[str, Tuple[Any, ...]]:
    """Generates parameterized SQL for a prebuilt query."""
    if query_id not in PREBUILT_QUERIES:
        raise DataSourceError(f"کوئری آماده ناشناخته: {query_id}")

    if query_id == "printer_pages_summary":
        days = int(params.get("days", 30))
        printer_ip = params.get("printer_ip", "").strip()
        if printer_ip:
            sql = """
                SELECT date, printer_ip, printer_name, total_pages, mono_pages, color_pages, a3_pages, a4_pages
                FROM daily_stats
                WHERE printer_ip = ?
                ORDER BY date DESC
                LIMIT ?
            """
            return sql, (printer_ip, days)
        else:
            sql = """
                SELECT date, SUM(total_pages) AS total_pages, SUM(mono_pages) AS mono_pages, SUM(color_pages) AS color_pages
                FROM daily_stats
                GROUP BY date
                ORDER BY date DESC
                LIMIT ?
            """
            return sql, (days,)

    elif query_id == "toner_levels_latest":
        min_l = int(params.get("min_level", 0))
        max_l = int(params.get("max_level", 100))
        sql = """
            SELECT ip, model, serial, print_total, last_seen
            FROM printer_counters
            ORDER BY ip ASC
            LIMIT 100
        """
        return sql, ()

    elif query_id == "sensor_readings_recent":
        days = int(params.get("days", 7))
        sql = """
            SELECT timestamp, printer_ip, kind, value, unit, status
            FROM sensor_readings
            ORDER BY timestamp DESC
            LIMIT ?
        """
        return sql, (days * 100,)

    elif query_id == "cartridge_lifecycles":
        lim = min(int(params.get("limit", 20)), 100)
        sql = """
            SELECT printer_ip, cartridge_color, cartridge_serial, pages_printed, estimated_yield_per_page, actual_yield_per_page, installed_at
            FROM cartridge_lifecycle
            ORDER BY installed_at DESC
            LIMIT ?
        """
        return sql, (lim,)

    elif query_id == "security_alerts_active":
        lim = min(int(params.get("limit", 25)), 100)
        sql = """
            SELECT timestamp, event_type, severity, user_identifier, ip_address, details
            FROM security_events
            ORDER BY timestamp DESC
            LIMIT ?
        """
        return sql, (lim,)

    raise DataSourceError(f"کوئری {query_id} پیاده‌سازی نشده است.")


def build_visual_sql(spec: Dict[str, Any]) -> Tuple[str, Tuple[Any, ...]]:
    """Compiles a structured visual query specification into parameterized SQL."""
    table = spec.get("table", "").strip()
    if not table or table not in ALLOWED_TABLES:
        raise DataSourceError(f"جدول نامعتبر یا غیرمجاز: '{table}'. جداول مجاز: {', '.join(sorted(ALLOWED_TABLES))}")

    # Build SELECT clause
    fields = spec.get("fields", [])
    aggregations = spec.get("aggregations", [])
    select_parts = []

    if fields:
        for f in fields:
            if not IDENTIFIER_RE.fullmatch(f):
                raise DataSourceError(f"نام فیلد نامعتبر: '{f}'")
            select_parts.append(f)

    if aggregations:
        for agg in aggregations:
            op = agg.get("op", "").upper()
            field = agg.get("field", "").strip()
            alias = agg.get("alias", "").strip()

            if op not in VALID_AGGREGATIONS:
                raise DataSourceError(f"عملگر تجمیعی نامعتبر: '{op}'")
            if field != "*" and not IDENTIFIER_RE.fullmatch(field):
                raise DataSourceError(f"نام فیلد نامعتبر در تجمیع: '{field}'")
            if alias and not IDENTIFIER_RE.fullmatch(alias):
                raise DataSourceError(f"نام مستعار نامعتبر: '{alias}'")

            expr = f"{op}({field})"
            if alias:
                expr += f" AS {alias}"
            select_parts.append(expr)

    if not select_parts:
        select_parts = ["*"]

    # Build WHERE clause
    where_parts = []
    params: List[Any] = []
    filters = spec.get("filters", [])
    for flt in filters:
        f_name = flt.get("field", "").strip()
        f_op = flt.get("op", "=").upper()
        f_val = flt.get("value")

        if not IDENTIFIER_RE.fullmatch(f_name):
            raise DataSourceError(f"فیلد فیلتر نامعتبر: '{f_name}'")
        if f_op not in VALID_FILTER_OPS:
            raise DataSourceError(f"عملگر فیلتر نامعتبر: '{f_op}'")

        where_parts.append(f"{f_name} {f_op} ?")
        params.append(f_val)

    # Build GROUP BY clause
    group_parts = []
    group_by = spec.get("group_by", [])
    for g in group_by:
        if not IDENTIFIER_RE.fullmatch(g):
            raise DataSourceError(f"فیلد گروه نامعتبر: '{g}'")
        group_parts.append(g)

    # Build ORDER BY clause
    order_clause = ""
    order_by = spec.get("order_by")
    if order_by and isinstance(order_by, dict):
        o_field = order_by.get("field", "").strip()
        o_dir = "DESC" if order_by.get("dir", "ASC").upper() == "DESC" else "ASC"
        if IDENTIFIER_RE.fullmatch(o_field):
            order_clause = f" ORDER BY {o_field} {o_dir}"

    limit = min(int(spec.get("limit", 100)), 1000)

    # Assemble SQL
    sql = f"SELECT {', '.join(select_parts)} FROM {table}"
    if where_parts:
        sql += f" WHERE {' AND '.join(where_parts)}"
    if group_parts:
        sql += f" GROUP BY {', '.join(group_parts)}"
    if order_clause:
        sql += order_clause
    sql += f" LIMIT {limit}"

    return sql, tuple(params)


def validate_and_sanitize_sql(raw_sql: str) -> str:
    """Validates raw SQL query for strict Read-Only enforcement and table access."""
    clean = raw_sql.strip()
    if not clean:
        raise DataSourceError("متن کوئری SQL نمی‌تواند خالی باشد.")

    upper = clean.upper()

    # Must start with SELECT or WITH
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise DataSourceSecurityError("فقط کوئری‌های خواندنی (SELECT / WITH) در سامانه مجاز هستند.")

    # Check for multiple statements (semicolon followed by non-whitespace)
    if ";" in clean:
        parts = [p.strip() for p in clean.split(";") if p.strip()]
        if len(parts) > 1:
            raise DataSourceSecurityError("اجرای چندین دستور مجزا در یک کوئری مسدود شده است.")
        clean = parts[0]

    # Prohibited keywords check
    if PROHIBITED_SQL_KEYWORDS.search(clean):
        raise DataSourceSecurityError("دستورات مخرب یا تغییر ساختار و داده مسدود هستند.")

    # Table access whitelist check
    words = re.findall(r"\b[a-zA-Z0-9_]+\b", clean.lower())
    table_hits = set(words).intersection(ALLOWED_TABLES)
    if not table_hits:
        raise DataSourceSecurityError(
            f"کوئری باید به یکی از جداول مجاز سیستم مراجعه کند ({', '.join(sorted(ALLOWED_TABLES))})."
        )

    # Inject LIMIT if not specified
    if "LIMIT" not in upper:
        clean = f"{clean} LIMIT 500"

    return clean


# ─── Query Executor ───────────────────────────────────────────────────────────

def execute_query(conn: Any, query_spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes a query based on mode ('prebuilt', 'visual', 'sql') and returns standardized tabular output:
    {
        "columns": [{"field": "date", "title": "date", "type": "text"}],
        "rows": [{"date": "2026-10-15", ...}],
        "total": 1,
        "execution_ms": 1.25,
        "mode": "prebuilt"
    }
    """
    mode = query_spec.get("mode", "prebuilt").lower()

    if mode == "prebuilt":
        query_id = query_spec.get("query_id", "")
        params = query_spec.get("params", {})
        sql, sql_params = build_prebuilt_sql(query_id, params)

    elif mode == "visual":
        spec = query_spec.get("spec", {})
        sql, sql_params = build_visual_sql(spec)

    elif mode == "sql":
        raw_sql = query_spec.get("raw_sql", "")
        sql = validate_and_sanitize_sql(raw_sql)
        sql_params = ()

    else:
        raise DataSourceError(f"حالت کوئری نامعتبر: {mode}. گزینه‌های مجاز: prebuilt, visual, sql")

    t0 = time.perf_counter()
    cur = conn.execute(sql, sql_params)
    t1 = time.perf_counter()

    col_names = [desc[0] for desc in cur.description] if cur.description else []
    raw_rows = cur.fetchall()

    columns = [{"field": c, "title": c, "type": "text"} for c in col_names]
    rows = []
    for r in raw_rows:
        rows.append(dict(zip(col_names, [val if val is not None else "" for val in r])))

    return {
        "columns": columns,
        "rows": rows,
        "total": len(rows),
        "execution_ms": round((t1 - t0) * 1000, 2),
        "mode": mode,
        "sql": sql,
    }
