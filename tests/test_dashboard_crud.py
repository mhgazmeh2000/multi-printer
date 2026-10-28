"""
Tests for Phase J.3: Dashboard CRUD, Panel CRUD, and Permissions.
Verifies dashboard creation, updating, deletion, panel manipulation,
permission boundaries (admin/owner/editor/viewer), and REST APIs.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from core.dashboard.manager import (
    add_panel,
    check_dashboard_access,
    create_dashboard,
    delete_dashboard,
    delete_panel,
    get_dashboard,
    get_dashboard_permissions,
    list_dashboards,
    set_dashboard_permission,
    update_dashboard,
    update_panel,
    DashboardNotFoundError,
)
from core.database import init_db
from core.db import get_connection
from core.db.exceptions import UniqueViolation
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User


class DashboardManagerUnitTests(unittest.TestCase):
    """Unit tests for core/dashboard/manager.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["DB_BACKEND"] = "sqlite"

        init_db()
        init_security_audit()
        self.conn = get_connection(timeout=10.0)

    def tearDown(self):
        self.conn.close()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def test_create_and_get_dashboard(self):
        dash = create_dashboard(
            conn=self.conn,
            title="داشبورد اجرایی",
            description="پایش روزانه پرینترهای رنگی",
            layout={"cols": 12, "rows": []},
            variables={"printer": "all"},
            owner_id=42,
        )
        self.assertIsNotNone(dash)
        self.assertTrue(dash["uid"].startswith("dash_"))
        self.assertEqual(dash["title"], "داشبورد اجرایی")
        self.assertEqual(dash["owner_id"], 42)
        self.assertEqual(dash["variables"], {"printer": "all"})

        # Retrieve and compare
        retrieved = get_dashboard(self.conn, dash["uid"])
        self.assertEqual(retrieved["id"], dash["id"])
        self.assertEqual(retrieved["description"], "پایش روزانه پرینترهای رنگی")

    def test_update_dashboard(self):
        dash = create_dashboard(self.conn, title="داشبورد اولیه")
        uid = dash["uid"]

        updated = update_dashboard(
            self.conn,
            uid=uid,
            title="داشبورد ویرایش‌شده",
            description="توضیحات جدید",
            is_starred=True,
        )
        self.assertTrue(updated)

        retrieved = get_dashboard(self.conn, uid)
        self.assertEqual(retrieved["title"], "داشبورد ویرایش‌شده")
        self.assertEqual(retrieved["description"], "توضیحات جدید")
        self.assertTrue(retrieved["is_starred"])

    def test_delete_dashboard_cascades_panels(self):
        dash = create_dashboard(self.conn, title="داشبورد موقت")
        uid = dash["uid"]

        # Add panel
        panel = add_panel(
            conn=self.conn,
            dashboard_uid=uid,
            panel_type="stat",
            title="شمارنده پرینت",
        )
        self.assertIsNotNone(panel["uid"])

        # Delete dashboard
        deleted = delete_dashboard(self.conn, uid)
        self.assertTrue(deleted)
        self.assertIsNone(get_dashboard(self.conn, uid))

        # Check that panel is gone
        row = self.conn.execute("SELECT id FROM panels WHERE uid = ?", (panel["uid"],)).fetchone()
        self.assertIsNone(row)

    def test_child_rows_reject_nonexistent_dashboard_ids(self):
        with self.assertRaises(UniqueViolation):
            self.conn.execute(
                "INSERT INTO panels (dashboard_id, uid, type, title) VALUES (?, ?, ?, ?)",
                (999999, "orphan-panel", "stat", "Orphan"),
            )
        with self.assertRaises(UniqueViolation):
            self.conn.execute(
                "INSERT INTO dashboard_permissions (dashboard_id, role) VALUES (?, ?)",
                (999999, "viewer"),
            )

    def test_panel_crud_operations(self):
        dash = create_dashboard(self.conn, title="داشبورد تست پنل")
        uid = dash["uid"]

        # 1. Add panel
        panel = add_panel(
            conn=self.conn,
            dashboard_uid=uid,
            panel_type="timeseries",
            title="نمودار روند",
            query={"metric": "print_pages"},
            config={"smooth": True},
            grid_pos={"x": 0, "y": 0, "w": 6, "h": 4},
        )
        p_uid = panel["uid"]
        self.assertEqual(panel["type"], "timeseries")

        # 2. Update panel
        up_res = update_panel(
            conn=self.conn,
            panel_uid=p_uid,
            title="نمودار روند ماهانه",
            grid_pos={"x": 6, "y": 0, "w": 6, "h": 4},
        )
        self.assertTrue(up_res)

        dash_after = get_dashboard(self.conn, uid)
        self.assertEqual(len(dash_after["panels"]), 1)
        self.assertEqual(dash_after["panels"][0]["title"], "نمودار روند ماهانه")
        self.assertEqual(dash_after["panels"][0]["grid_pos"]["x"], 6)

        # 3. Delete panel
        del_res = delete_panel(self.conn, p_uid)
        self.assertTrue(del_res)
        dash_empty = get_dashboard(self.conn, uid)
        self.assertEqual(len(dash_empty["panels"]), 0)

    def test_permissions_access_control(self):
        dash = create_dashboard(self.conn, title="داشبورد امنیتی", owner_id=10)
        uid = dash["uid"]

        # Admin always has all access
        self.assertTrue(check_dashboard_access(self.conn, uid, user_id=99, user_role="admin", action="delete"))

        # Owner has edit and delete
        self.assertTrue(check_dashboard_access(self.conn, uid, user_id=10, user_role="manager", action="edit"))
        self.assertTrue(check_dashboard_access(self.conn, uid, user_id=10, user_role="manager", action="delete"))

        # Non-owner user without perms cannot edit
        self.assertFalse(check_dashboard_access(self.conn, uid, user_id=20, user_role="viewer", action="edit"))

        # Grant 'editor' permission to user 20
        set_dashboard_permission(self.conn, dashboard_uid=uid, role="editor", user_id=20)
        self.assertTrue(check_dashboard_access(self.conn, uid, user_id=20, user_role="viewer", action="edit"))
        # But editor cannot delete dashboard
        self.assertFalse(check_dashboard_access(self.conn, uid, user_id=20, user_role="viewer", action="delete"))


class DashboardApiIntegrationTests(unittest.TestCase):
    """Integration tests for Dashboard REST API endpoints."""

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

        self.admin = User.create("dash_admin", "admin@printguard.org", "pass123", role="admin", is_verified=True)
        self.viewer = User.create("dash_viewer", "viewer@printguard.org", "pass123", role="viewer", is_verified=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def test_api_dashboard_lifecycle(self):
        self._login(self.admin)

        # 1. Create Dashboard
        res = self.client.post(
            "/api/dashboards",
            json={"title": "داشبورد سازمانی تست", "description": "توضیحات تست"},
        )
        self.assertEqual(res.status_code, 201)
        created = res.get_json()
        uid = created["uid"]
        self.assertIn("dash_", uid)

        # 2. Get Dashboard
        res = self.client.get(f"/api/dashboards/{uid}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["title"], "داشبورد سازمانی تست")

        # 3. Add Panel
        res = self.client.post(
            f"/api/dashboards/{uid}/panels",
            json={"type": "bargauge", "title": "سطح تونرها", "grid_pos": {"x": 0, "y": 0, "w": 4, "h": 3}},
        )
        self.assertEqual(res.status_code, 201)
        panel_uid = res.get_json()["uid"]

        # 4. Update Panel
        res = self.client.put(
            f"/api/dashboards/{uid}/panels/{panel_uid}",
            json={"title": "سطح تونرها (ویرایش‌شده)"},
        )
        self.assertEqual(res.status_code, 200)

        # 5. List Dashboards
        res = self.client.get("/api/dashboards")
        self.assertEqual(res.status_code, 200)
        items = res.get_json()["dashboards"]
        self.assertTrue(any(d["uid"] == uid for d in items))

        # 6. Delete Panel
        res = self.client.delete(f"/api/dashboards/{uid}/panels/{panel_uid}")
        self.assertEqual(res.status_code, 200)

        # 7. Delete Dashboard
        res = self.client.delete(f"/api/dashboards/{uid}")
        self.assertEqual(res.status_code, 200)

        # Verify not found
        res = self.client.get(f"/api/dashboards/{uid}")
        self.assertEqual(res.status_code, 404)

    def test_viewer_cannot_create_dashboard(self):
        self._login(self.viewer)
        res = self.client.post(
            "/api/dashboards",
            json={"title": "داشبورد غیرمجاز"},
        )
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
