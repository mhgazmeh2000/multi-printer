"""Negative regression coverage for reporting, secrets and dashboard HTML contexts."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

import config.settings as settings
from core.database import init_db
from core.db import get_connection
from core.dashboard.query_engine import execute_query, validate_and_sanitize_sql, build_visual_sql
from core.dashboard.sql_security import sanitize_select, readonly_reporting, MAX_REPORT_ROWS
from core.datasources.base import DataSourceSecurityError
from core.datasources.crypto import mask_config, merge_masked_config
from core.datasources.drivers.postgresql_ds import PostgreSQLDataSource
from models import User
from web import create_app

ROOT = Path(__file__).resolve().parents[1]


class ReportingSecurityTests(unittest.TestCase):
    def test_real_table_resolution_rejects_literals_comments_joins_and_ctes(self):
        attacks = [
            "SELECT password_hash FROM users",
            "SELECT * FROM users WHERE 'logs' = 'logs'",
            "SELECT * FROM users /* logs */",
            "SELECT u.* FROM logs l JOIN users u ON 1=1",
            "WITH l AS (SELECT * FROM users) SELECT * FROM l JOIN logs ON 1=1",
            "WITH logs AS (SELECT * FROM users) SELECT * FROM logs",
            "SELECT (SELECT password_hash FROM users LIMIT 1) FROM logs",
            "SELECT * FROM other.logs",
            "SELECT * FROM pg_catalog.pg_authid /* logs */",
        ]
        for query in attacks:
            with self.subTest(query=query), self.assertRaises(DataSourceSecurityError):
                validate_and_sanitize_sql(query)

    def test_disallows_writes_table_functions_custom_functions_and_types(self):
        for query in [
            "SELECT * FROM logs; SELECT * FROM users",
            "WITH x AS (DELETE FROM logs RETURNING *) SELECT * FROM x",
            "SELECT * INTO saved_logs FROM logs",
            "SELECT * FROM logs FOR UPDATE",
            "SELECT pg_sleep(5) FROM logs",
            "SELECT pg_catalog.pg_read_file('x') FROM logs",
            "SELECT * FROM generate_series(1, 500) AS logs",
            "SELECT load_extension('x') FROM logs",
            "SELECT CAST(message AS custom_type) FROM logs",
            "COPY logs TO STDOUT",
        ]:
            with self.subTest(query=query), self.assertRaises(DataSourceSecurityError):
                sanitize_select(query)

    def test_accepts_aggregate_cte_and_applies_outer_limit(self):
        subprocess.run([sys.executable, "-c",
                        "from core.dashboard.sql_security import sanitize_select; sanitize_select('SELECT 1')"],
                       cwd=ROOT, check=True, capture_output=True)
        for query in [
            "SELECT COUNT(*) FROM logs",
            "WITH x AS (SELECT pages FROM logs) SELECT SUM(pages) FROM x",
            "SELECT * FROM logs UNION ALL SELECT * FROM logs",
            "SELECT * FROM logs LIMIT ALL",
            "SELECT * FROM logs LIMIT 9999999",
            "SELECT 'LIMIT' AS label, pages FROM logs",
        ]:
            with self.subTest(query=query):
                self.assertTrue(validate_and_sanitize_sql(query).endswith("LIMIT 500"))

    def test_visual_has_no_users_and_clamps_negative_limit(self):
        with self.assertRaises(Exception):
            build_visual_sql({"table": "users", "fields": ["password_hash"]})
        query, _ = build_visual_sql({"table": "logs", "limit": -1})
        self.assertTrue(query.endswith("LIMIT 1"))

    def test_recursive_mask_and_partial_merge_never_alias_original(self):
        original = {"password": "top", "auth": {"token": "nested"},
                    "headers": [{"Authorization": "Bearer secret"}], "host": "db.local"}
        masked = mask_config(original)
        self.assertNotIn("secret", json.dumps(masked))
        self.assertEqual(masked["auth"]["token"], "********")
        merged = merge_masked_config({"auth": {"token": "********"}}, original)
        self.assertEqual(merged["auth"]["token"], "nested")
        self.assertEqual(merged["host"], "db.local")
        merged["headers"][0]["Authorization"] = "changed"
        self.assertEqual(original["headers"][0]["Authorization"], "Bearer secret")

    def test_postgres_driver_readonly_timeout_and_bounded_fetch(self):
        ds = PostgreSQLDataSource(uid="audit", name="audit", config={"password": "a b'c"})
        from psycopg.conninfo import conninfo_to_dict
        self.assertEqual(conninfo_to_dict(ds._get_connection_string())["password"], "a b'c")
        with patch("core.datasources.drivers.postgresql_ds.psycopg.connect") as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.description = [("n",)]
            conn.execute.return_value.fetchmany.return_value = [(1,)]
            ds.query({"raw_sql": "SELECT 1"})
            statements = [call.args[0] for call in conn.execute.call_args_list]
            self.assertEqual(statements[0], "SET TRANSACTION READ ONLY")
            self.assertIn("SET LOCAL statement_timeout = '5s'", statements)
            conn.execute.return_value.fetchmany.assert_called_once_with(MAX_REPORT_ROWS)

    def test_pg_readonly_setup_precedes_query(self):
        conn = MagicMock(backend="postgresql")
        with readonly_reporting(conn):
            conn.execute("SELECT 1")
        self.assertEqual(conn.execute.call_args_list[0].args[0], "SET TRANSACTION READ ONLY")


class ReportingApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(settings, "DB_PATH", os.path.join(self.tmp.name, "security.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.env_patch = patch.dict(os.environ, {"DB_BACKEND": "sqlite", "REPORTING_PG_DSN": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        init_db()
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = self.app.test_client()
        self.users = {
            role: User.create("security_" + role, role + "@audit.local", "Testing-Only-123!",
                              role=role, is_verified=True)
            for role in ("admin", "manager", "viewer")
        }

    def login(self, role):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.users[role].id)
            sess["_fresh"] = True

    def test_manager_viewer_cannot_execute_or_validate_any_query_mode(self):
        for role in ("manager", "viewer"):
            self.login(role)
            for mode in ("sql", "visual", "prebuilt"):
                for endpoint in ("preview", "validate"):
                    with self.subTest(role=role, mode=mode, endpoint=endpoint):
                        result = self.client.post("/api/query/" + endpoint, json={"mode": mode})
                        self.assertEqual(result.status_code, 403)

    def test_admin_cannot_read_users_through_either_builder(self):
        self.login("admin")
        for payload in [
            {"mode": "sql", "raw_sql": "SELECT password_hash FROM users /* logs */"},
            {"mode": "visual", "spec": {"table": "users", "fields": ["password_hash"]}},
        ]:
            result = self.client.post("/api/query/preview", json=payload)
            self.assertIn(result.status_code, (400, 403))
            self.assertNotIn("$2b$", result.get_data(as_text=True))

    def test_anonymous_preview_is_unauthorized(self):
        self.assertEqual(self.client.post("/api/query/preview", json={}).status_code, 401)

    def test_manager_cannot_change_or_execute_datasource_connections(self):
        self.login("manager")
        page = self.client.get("/datasources")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertNotIn('id="btn-new-ds"', html)
        self.assertIn('const canManageDataSources = false;', html)
        if shutil.which("node"):
            for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S):
                subprocess.run(["node", "--check"], input=script, text=True, encoding="utf-8",
                               capture_output=True, check=True, timeout=30)
        for method, path in [
            ("post", "/api/datasources"), ("put", "/api/datasources/test"),
            ("post", "/api/datasources/test/query"), ("post", "/api/datasources/test/test"),
        ]:
            self.assertEqual(getattr(self.client, method)(path, json={}).status_code, 403)

    def test_driver_schema_rejects_nested_undeclared_config(self):
        self.login("admin")
        result = self.client.post("/api/datasources", json={
            "name": "audit", "type": "rest",
            "config": {"base_url": "https://example.invalid", "auth": {"password": "never-echo-me"}},
        })
        self.assertEqual(result.status_code, 400)
        self.assertNotIn("never-echo-me", result.get_data(as_text=True))

    def test_sql_limit_enforced_by_real_execution_and_connection_reusable(self):
        conn = get_connection()
        try:
            conn.executemany("INSERT INTO logs(printer_ip,timestamp,type) VALUES(?,?,?)",
                             [("192.0.2.1", "2026-01-01T00:00:00Z", "PRINT")] * 600)
            conn.commit()
            result = execute_query(conn, {"mode": "sql", "raw_sql": "SELECT * FROM logs LIMIT 9999999"})
            self.assertEqual(result["total"], MAX_REPORT_ROWS)
            # SQLite query_only was restored after reporting.
            conn.execute("DELETE FROM logs")
            conn.rollback()
        finally:
            conn.close()

    def test_readonly_context_rejects_write_even_if_compiler_is_bypassed(self):
        conn = get_connection()
        try:
            with self.assertRaises(Exception):
                with readonly_reporting(conn):
                    conn.execute("DELETE FROM logs")
            conn.execute("DELETE FROM logs")
            conn.rollback()
        finally:
            conn.close()

    def test_csrf_still_required_for_admin_session(self):
        self.login("admin")
        self.app.config["WTF_CSRF_ENABLED"] = True
        result = self.client.post("/api/query/preview", json={"mode": "sql", "raw_sql": "SELECT * FROM logs"})
        self.assertEqual(result.status_code, 400)


class _Tags(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@unittest.skipUnless(shutil.which("node"), "Node required for dashboard security regressions")
class DashboardSecurityTests(unittest.TestCase):
    def test_real_renderers_keep_untrusted_names_models_and_alerts_inert(self):
        script = r'''
const fs = require('fs');
const src = fs.readFileSync('web/static/js/dashboard.js', 'utf8');
let delegated;
const document = {addEventListener: (type, cb) => {delegated = cb;}};
let allData = [];
function load(start,end) {return src.slice(src.indexOf(start),src.indexOf(end,src.indexOf(start)));}
eval(load('function escapeHtml(', '// Time display'));
eval(load('function buildPrinterDetail(', 'function buildSensorDetail('));
eval(load('function renderPrinterCard(', '// FETCH & UPDATE'));
const OFFICE_GROUPS = [];
function signalQualityBadge() {return '';}
function fmtN(n) {return Number(n || 0).toString();}
const payload = `دفتر O'Reilly "><img src=x onerror="globalThis.securityExecuted=true">`;
const p = {ip:'192.0.2.1',name:payload,online:false,error:payload};
allData.push(p);
const offline = buildPrinterDetail(p);
const card = renderPrinterCard({...p,online:true,device_type:'mono',
  cartridge_ids:{black:payload}, device:{model:payload},alerts:[{message:payload}]});
let removed;
function removePrinter(ip,name) {removed={ip,name};}
delegated({target:{closest:()=>({dataset:{printerAction:'remove',ip:p.ip}})},preventDefault(){}});
console.log(JSON.stringify({offline,card,payload,removed,argument:jsArg(payload)}));
'''
        proc = subprocess.run(["node", "-"], input=script, text=True, encoding="utf-8", cwd=ROOT,
                              capture_output=True, check=True, timeout=30)
        result = json.loads(proc.stdout)
        for key in ("offline", "card"):
            tags = _Tags(result[key]).tags
            self.assertFalse(any(tag in ("img", "script", "iframe") for tag, _ in tags))
            self.assertFalse(any("onerror" in attrs for _, attrs in tags))
        buttons = [attrs for tag, attrs in _Tags(result["offline"]).tags if tag == "button"]
        self.assertEqual(buttons[0]["data-printer-action"], "remove")
        self.assertNotIn("onclick", buttons[0])
        self.assertEqual(result["removed"]["name"], result["payload"])
        # Simulate the browser's attribute entity decoding then handler compilation.
        attrs = _Tags('<button onclick="capture(' + result["argument"] + ')"></button>').tags[0][1]
        js = "let got;function capture(x){got=x;}\n" + attrs["onclick"] + "\nconsole.log(JSON.stringify(got));"
        actual = subprocess.run(["node", "-"], input=js, text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
        self.assertEqual(json.loads(actual.stdout), result["payload"])


if __name__ == "__main__":
    unittest.main()
