"""
Tests for Phase J.8: 3-Level Query Builder (Prebuilt, Visual, SQL).
Verifies prebuilt catalog, parameterized visual query generation, strict SQL
security guards (anti-SQL-injection, read-only enforcement, table whitelisting),
query execution metrics, and REST APIs.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from core.dashboard.query_engine import (
    build_prebuilt_sql,
    build_visual_sql,
    execute_query,
    get_prebuilt_catalog,
    validate_and_sanitize_sql,
)
from core.datasources.base import DataSourceError, DataSourceSecurityError
from core.database import init_db
from core.db import get_connection
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User


class QueryEngineUnitTests(unittest.TestCase):
    """Unit tests for query engine compilation, security, and catalog."""

    def test_prebuilt_catalog_contains_standard_metrics(self):
        catalog = get_prebuilt_catalog()
        self.assertGreaterEqual(len(catalog), 5)
        ids = {q["id"] for q in catalog}
        self.assertIn("printer_pages_summary", ids)
        self.assertIn("toner_levels_latest", ids)
        self.assertIn("sensor_readings_recent", ids)
        self.assertIn("cartridge_lifecycles", ids)
        self.assertIn("security_alerts_active", ids)

    def test_prebuilt_query_sql_generation(self):
        sql, params = build_prebuilt_sql("printer_pages_summary", {"days": 15, "printer_ip": "10.0.0.1"})
        self.assertIn("FROM daily_stats", sql)
        self.assertIn("printer_ip = ?", sql)
        self.assertEqual(params, ("10.0.0.1", 15))

    def test_visual_query_builder_compiles_parameterized_sql(self):
        spec = {
            "table": "daily_stats",
            "aggregations": [{"op": "SUM", "field": "total_pages", "alias": "sum_pages"}],
            "filters": [{"field": "date", "op": ">=", "value": "2026-10-01"}],
            "group_by": ["printer_ip"],
            "order_by": {"field": "sum_pages", "dir": "DESC"},
            "limit": 50,
        }
        sql, params = build_visual_sql(spec)
        self.assertIn("SELECT SUM(total_pages) AS sum_pages FROM daily_stats", sql)
        self.assertIn("WHERE date >= ?", sql)
        self.assertIn("GROUP BY printer_ip", sql)
        self.assertIn("LIMIT 50", sql)
        self.assertEqual(params, ("2026-10-01",))

    def test_visual_query_rejects_unapproved_table(self):
        spec = {"table": "arbitrary_nonexistent_table"}
        with self.assertRaises(DataSourceError):
            build_visual_sql(spec)

    def test_visual_query_rejects_invalid_identifiers(self):
        bad_spec = {
            "table": "daily_stats",
            "aggregations": [{"op": "SUM", "field": "total_pages; DROP TABLE logs", "alias": "x"}],
        }
        with self.assertRaises(DataSourceError):
            build_visual_sql(bad_spec)

    def test_sql_validator_allows_valid_select(self):
        raw = "SELECT date, printer_ip, total_pages FROM daily_stats WHERE total_pages > 10"
        sanitized = validate_and_sanitize_sql(raw)
        self.assertIn("FROM daily_stats", sanitized)
        self.assertIn("LIMIT 500", sanitized)

    def test_sql_validator_blocks_data_modification_and_ddl(self):
        forbidden = [
            "DELETE FROM daily_stats WHERE id = 1;",
            "DROP TABLE logs;",
            "UPDATE users SET role = 'admin';",
            "INSERT INTO daily_stats (date) VALUES ('2026-10-10');",
            "TRUNCATE sensor_readings;",
            "ALTER TABLE users ADD COLUMN hacked text;",
            "SELECT * FROM daily_stats; DROP TABLE users;",
            "SELECT * FROM unapproved_secret_table;",
        ]
        for query in forbidden:
            with self.assertRaises(DataSourceSecurityError, msg=f"Should reject: {query}"):
                validate_and_sanitize_sql(query)


class QueryEngineExecutionTests(unittest.TestCase):
    """Verifies end-to-end query execution across the 3 modes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["DB_BACKEND"] = "sqlite"

        init_db()
        self.conn = get_connection(timeout=10.0)

        # Seed sample data into daily_stats
        self.conn.execute(
            """
            INSERT INTO daily_stats (date, printer_ip, printer_name, total_pages, mono_pages, color_pages)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("2026-10-18", "192.168.1.10", "HP LaserJet M425dn", 120, 100, 20),
        )
        self.conn.execute(
            """
            INSERT INTO daily_stats (date, printer_ip, printer_name, total_pages, mono_pages, color_pages)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("2026-10-19", "192.168.1.10", "HP LaserJet M425dn", 240, 200, 40),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_execute_prebuilt_mode(self):
        spec = {
            "mode": "prebuilt",
            "query_id": "printer_pages_summary",
            "params": {"days": 30, "printer_ip": "192.168.1.10"},
        }
        res = execute_query(self.conn, spec)
        self.assertEqual(res["total"], 2)
        self.assertGreater(res["execution_ms"], 0.0)
        self.assertEqual(res["rows"][0]["printer_ip"], "192.168.1.10")

    def test_execute_visual_mode(self):
        spec = {
            "mode": "visual",
            "spec": {
                "table": "daily_stats",
                "fields": ["date", "total_pages"],
                "filters": [{"field": "total_pages", "op": ">=", "value": 150}],
            },
        }
        res = execute_query(self.conn, spec)
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["rows"][0]["total_pages"], 240)

    def test_execute_sql_mode(self):
        spec = {
            "mode": "sql",
            "raw_sql": "SELECT date, total_pages FROM daily_stats ORDER BY total_pages DESC",
        }
        res = execute_query(self.conn, spec)
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["rows"][0]["total_pages"], 240)


class QueryEngineApiIntegrationTests(unittest.TestCase):
    """Integration tests for Query Builder REST APIs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["DB_BACKEND"] = "sqlite"

        init_db()
        init_security_audit()

        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.user = User.create("qb_tester", "qb@printguard.org", "pass123", role="manager", is_verified=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

    def test_api_prebuilt_catalog_endpoint(self):
        self._login()
        res = self.client.get("/api/query/prebuilt-catalog")
        self.assertEqual(res.status_code, 200)
        catalog = res.get_json()["catalog"]
        self.assertGreaterEqual(len(catalog), 5)

    def test_api_query_preview_endpoint(self):
        self._login()
        spec = {
            "mode": "prebuilt",
            "query_id": "printer_pages_summary",
            "params": {"days": 7},
        }
        res = self.client.post("/api/query/preview", json=spec)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("columns", data)
        self.assertIn("rows", data)
        self.assertIn("execution_ms", data)

    def test_api_query_preview_security_rejection(self):
        self._login()
        bad_spec = {
            "mode": "sql",
            "raw_sql": "DROP TABLE logs;",
        }
        res = self.client.post("/api/query/preview", json=bad_spec)
        self.assertEqual(res.status_code, 403)
        self.assertIn("خطای امنیتی", res.get_json()["error"])

    def test_api_query_validate_endpoint(self):
        self._login()
        res = self.client.post(
            "/api/query/validate",
            json={"mode": "sql", "raw_sql": "SELECT date FROM daily_stats"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "valid")


if __name__ == "__main__":
    unittest.main()
