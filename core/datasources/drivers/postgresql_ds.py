"""
PostgreSQL Data Source Driver (Phase J.7)
Provides direct SQL querying with strict Read-Only enforcement and LIMIT protection.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
import psycopg

from core.datasources.base import BaseDataSource, DataSourceError, DataSourceSecurityError

PROHIBITED_SQL_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|EXEC|EXECUTE|CALL|VACUUM)\b",
    re.IGNORECASE,
)


class PostgreSQLDataSource(BaseDataSource):
    type_name = "postgresql"
    display_name = "پایگاه‌داده PostgreSQL"
    description = "اتصال و کوئری مستقیم روی سرورهای PostgreSQL داخلی و خارجی"
    icon = "database"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "host": {"type": "string", "default": "127.0.0.1"},
                "port": {"type": "integer", "default": 5432},
                "database": {"type": "string", "default": "printguard"},
                "user": {"type": "string", "default": "printguard"},
                "password": {"type": "string"},
                "sslmode": {"type": "string", "enum": ["disable", "prefer", "require"], "default": "prefer"},
                "timeout": {"type": "integer", "default": 10},
            },
            "required": ["host", "database", "user"],
        }

    def _get_connection_string(self) -> str:
        cfg = self.config
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 5432)
        dbname = cfg.get("database", "printguard")
        user = cfg.get("user", "printguard")
        password = cfg.get("password", "")
        sslmode = cfg.get("sslmode", "prefer")
        timeout = cfg.get("timeout", 10)

        parts = [
            f"host={host}",
            f"port={port}",
            f"dbname={dbname}",
            f"user={user}",
            f"sslmode={sslmode}",
            f"connect_timeout={timeout}",
        ]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    def test_connection(self) -> Dict[str, Any]:
        conn_str = self._get_connection_string()
        try:
            with psycopg.connect(conn_str) as conn:
                cur = conn.execute("SELECT version();")
                row = cur.fetchone()
                version = row[0] if row else "PostgreSQL"
            return {"status": "ok", "message": f"اتصال با موفقیت برقرار شد ({version.split()[0]} {version.split()[1]})."}
        except Exception as e:
            raise DataSourceError(f"خطا در برقراری اتصال به PostgreSQL: {e}")

    def query(self, query_params: Dict[str, Any]) -> Dict[str, Any]:
        raw_sql = query_params.get("raw_sql", "").strip()
        if not raw_sql:
            raise DataSourceError("متن کوئری SQL نمی‌تواند خالی باشد.")

        # Security Check: Must only be SELECT or WITH statements
        stripped_upper = raw_sql.upper().lstrip()
        if not (stripped_upper.startswith("SELECT") or stripped_upper.startswith("WITH")):
            raise DataSourceSecurityError("فقط کوئری‌های خواندنی (SELECT) در Data Source مجاز هستند.")

        if PROHIBITED_SQL_KEYWORDS.search(raw_sql):
            raise DataSourceSecurityError("دستورات تغییر داده (INSERT, UPDATE, DELETE, DROP, ...) اکیداً مسدود می‌باشند.")

        # Ensure reasonable LIMIT if not specified
        if "LIMIT" not in stripped_upper:
            raw_sql = f"{raw_sql.rstrip(';')} LIMIT 1000;"

        conn_str = self._get_connection_string()
        try:
            with psycopg.connect(conn_str) as conn:
                cur = conn.execute(raw_sql)
                col_names = [desc[0] for desc in cur.description] if cur.description else []
                raw_rows = cur.fetchall()

            columns = [{"field": c, "title": c, "type": "text"} for c in col_names]
            rows = []
            for r in raw_rows:
                rows.append(dict(zip(col_names, [str(val) if val is not None else "" for val in r])))

            return {
                "columns": columns,
                "rows": rows,
                "total": len(rows),
            }
        except Exception as e:
            raise DataSourceError(f"خطا در اجرای کوئری PostgreSQL: {e}")

    def get_schema(self) -> Dict[str, Any]:
        conn_str = self._get_connection_string()
        try:
            with psycopg.connect(conn_str) as conn:
                cur = conn.execute("""
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                        ORDER BY table_name;
                    """)
                tables = [r[0] for r in cur.fetchall()]
            return {"tables": tables}
        except Exception as e:
            raise DataSourceError(f"خطا در دریافت شمای پایگاه‌داده: {e}")
