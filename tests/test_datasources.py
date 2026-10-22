"""
Tests for Phase J.6 / J.7: Data Source Framework & Built-in Drivers (PostgreSQL, REST, GitHub).
Verifies driver registry, credential masking, read-only SQL security guard,
connection testing, query translation, and REST APIs.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.datasources.base import DataSourceSecurityError, DataSourceError
from core.datasources.crypto import mask_config, merge_masked_config
from core.datasources.drivers.github_ds import GitHubDataSource
from core.datasources.drivers.postgresql_ds import PostgreSQLDataSource
from core.datasources.drivers.rest_ds import RestDataSource
from core.datasources.manager import (
    datasource_registry,
    create_datasource,
    get_datasource,
    list_datasources,
    update_datasource,
    delete_datasource,
)
from core.database import init_db
from core.db import get_connection
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User


class DataSourceDriverUnitTests(unittest.TestCase):
    """Verifies registry, driver schemas, and credential masking."""

    def test_three_default_drivers_registered(self):
        drivers = datasource_registry.list_drivers()
        self.assertGreaterEqual(len(drivers), 3)
        types = {d["type"] for d in drivers}
        self.assertIn("postgresql", types)
        self.assertIn("rest", types)
        self.assertIn("github", types)

    def test_credential_masking_and_merge(self):
        raw = {
            "host": "localhost",
            "user": "admin",
            "password": "super_secret_password",
            "token": "ghp_1234567890",
        }
        masked = mask_config(raw)
        self.assertEqual(masked["host"], "localhost")
        self.assertEqual(masked["password"], "********")
        self.assertEqual(masked["token"], "********")

        # Merge when user keeps '********'
        updated = {"host": "remote_host", "password": "********", "token": "ghp_NEW"}
        merged = merge_masked_config(updated, raw)
        self.assertEqual(merged["host"], "remote_host")
        self.assertEqual(merged["password"], "super_secret_password")  # Preserved!
        self.assertEqual(merged["token"], "ghp_NEW")  # Replaced!

    def test_postgresql_read_only_security_guard(self):
        pg_ds = PostgreSQLDataSource(uid="test_pg", name="Test PG", config={"host": "127.0.0.1", "database": "test"})

        # Forbidden non-SELECT queries
        dangerous_queries = [
            "DROP TABLE logs;",
            "DELETE FROM users WHERE id = 1;",
            "UPDATE settings SET value = 'hacked';",
            "INSERT INTO logs (printer_ip) VALUES ('1.1.1.1');",
            "TRUNCATE print_events;",
            "CREATE TABLE evil (id int);",
            "SELECT * FROM logs; DROP TABLE logs;",
        ]

        for q in dangerous_queries:
            with self.assertRaises(DataSourceSecurityError, msg=f"Query '{q}' should be rejected."):
                pg_ds.query({"raw_sql": q})

    @patch("requests.get")
    def test_rest_driver_flattens_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {"ip": "192.168.1.10", "model": "HP M425dn", "pages": 44839},
                {"ip": "192.168.1.20", "model": "Toshiba 257", "pages": 536361},
            ]
        }
        mock_get.return_value = mock_resp

        rest_ds = RestDataSource(uid="test_rest", name="Test REST", config={"base_url": "https://api.test.com"})
        res = rest_ds.query({"endpoint": "/printers"})

        self.assertEqual(res["total"], 2)
        self.assertEqual(len(res["columns"]), 3)
        self.assertEqual(res["rows"][0]["ip"], "192.168.1.10")

    @patch("requests.get")
    def test_github_driver_overview_query(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "stargazers_count": 42,
            "forks_count": 7,
            "open_issues_count": 3,
            "subscribers_count": 12,
            "default_branch": "main",
        }
        mock_get.return_value = mock_resp

        gh_ds = GitHubDataSource(uid="test_gh", name="GitHub", config={"owner": "printguard", "repo": "multi-printer"})
        res = gh_ds.query({"metric_type": "overview"})

        self.assertEqual(res["total"], 1)
        self.assertEqual(res["rows"][0]["stars"], 42)
        self.assertEqual(res["rows"][0]["forks"], 7)


class DataSourceDatabaseAndApiIntegrationTests(unittest.TestCase):
    """Integration tests for Data Source CRUD, Permissions, and REST APIs."""

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

        self.admin = User.create("ds_admin", "admin@printguard.org", "p123456", role="admin", is_verified=True)
        self.viewer = User.create("ds_viewer", "vw@printguard.org", "p123456", role="viewer", is_verified=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def test_api_datasource_lifecycle(self):
        self._login(self.admin)

        # 1. Create Data Source
        res = self.client.post(
            "/api/datasources",
            json={
                "name": "سرور دیتابیس پشتیبان",
                "type": "postgresql",
                "config": {"host": "192.168.10.50", "database": "reports", "password": "secret_pg_pass"},
            },
        )
        self.assertEqual(res.status_code, 201)
        created = res.get_json()
        uid = created["uid"]
        self.assertEqual(created["name"], "سرور دیتابیس پشتیبان")
        self.assertEqual(created["config"]["password"], "********")  # Masked!

        # 2. Get Data Source
        res = self.client.get(f"/api/datasources/{uid}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["uid"], uid)

        # 3. Update Data Source
        res = self.client.put(
            f"/api/datasources/{uid}",
            json={"name": "دیتابیس پشتیبان ۲", "config": {"password": "********"}},
        )
        self.assertEqual(res.status_code, 200)

        # 4. List Data Sources
        res = self.client.get("/api/datasources")
        self.assertEqual(res.status_code, 200)
        items = res.get_json()["datasources"]
        self.assertTrue(any(d["uid"] == uid for d in items))

        # 5. Delete Data Source
        res = self.client.delete(f"/api/datasources/{uid}")
        self.assertEqual(res.status_code, 200)

        # Verify deletion
        res = self.client.get(f"/api/datasources/{uid}")
        self.assertEqual(res.status_code, 404)

    def test_viewer_cannot_create_or_delete_datasources(self):
        self._login(self.viewer)

        # Viewer create -> 403
        res = self.client.post(
            "/api/datasources",
            json={"name": "غیرمجاز", "type": "rest", "config": {}},
        )
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
