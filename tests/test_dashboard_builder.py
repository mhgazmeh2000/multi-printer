"""
Tests for Phase J.4: Dashboard Builder (Drag & Drop with Gridstack).
Verifies offline assets, builder JavaScript controller contracts,
builder and viewer web views, permissions, and layout auto-saving.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from core.dashboard.manager import create_dashboard, add_panel
from core.database import init_db
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User


class DashboardBuilderAssetAndTemplateTests(unittest.TestCase):
    """Verifies existence and contents of offline vendor files and builder scripts."""

    def test_gridstack_offline_assets_exist(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        gridstack_js = os.path.join(repo_root, "web", "static", "vendor", "gridstack", "gridstack-all.js")
        gridstack_css = os.path.join(repo_root, "web", "static", "vendor", "gridstack", "gridstack.min.css")
        builder_js = os.path.join(repo_root, "web", "static", "js", "dashboard-builder.js")

        self.assertTrue(os.path.isfile(gridstack_js), "gridstack-all.js must exist.")
        self.assertGreater(os.path.getsize(gridstack_js), 50_000, "gridstack-all.js must be a complete bundle.")

        self.assertTrue(os.path.isfile(gridstack_css), "gridstack.min.css must exist.")
        self.assertTrue(os.path.isfile(builder_js), "dashboard-builder.js must exist.")

        with open(builder_js, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("PrintGuardBuilder", content)
            self.assertIn("onGridChange", content)
            self.assertIn("undo", content)
            self.assertIn("redo", content)
            self.assertIn("saveToServer", content)
            self.assertIn("togglePreviewMode", content)

    def test_templates_exist_and_contain_required_elements(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        builder_tpl = os.path.join(repo_root, "web", "templates", "dashboard_builder.html")
        view_tpl = os.path.join(repo_root, "web", "templates", "dashboard_view.html")

        self.assertTrue(os.path.isfile(builder_tpl), "dashboard_builder.html must exist.")
        self.assertTrue(os.path.isfile(view_tpl), "dashboard_view.html must exist.")

        with open(builder_tpl, "r", encoding="utf-8") as f:
            b_text = f.read()
            self.assertIn("builder-grid", b_text)
            self.assertIn("modal-add-panel", b_text)
            self.assertIn("btn-undo", b_text)
            self.assertIn("btn-redo", b_text)

        with open(view_tpl, "r", encoding="utf-8") as f:
            v_text = f.read()
            self.assertIn("viewer-grid", v_text)


class DashboardBuilderRouteIntegrationTests(unittest.TestCase):
    """Integration tests for Builder & Viewer routes."""

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

        self.admin = User.create("builder_admin", "admin@printguard.org", "pass123", role="admin", is_verified=True)
        self.viewer = User.create("builder_viewer", "viewer@printguard.org", "pass123", role="viewer", is_verified=True)

        # Create a sample dashboard
        from core.db import get_connection
        conn = get_connection(timeout=10.0)
        try:
            self.dash = create_dashboard(
                conn,
                title="داشبورد اتاق سرور",
                description="پایش سنسور و پرینترها",
                owner_id=self.admin.id,
            )
            self.panel = add_panel(
                conn,
                dashboard_uid=self.dash["uid"],
                panel_type="stat",
                title="دمای محیط",
                grid_pos={"x": 0, "y": 0, "w": 4, "h": 3},
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def test_anonymous_redirected_to_login(self):
        uid = self.dash["uid"]
        res_view = self.client.get(f"/dashboards/{uid}")
        self.assertEqual(res_view.status_code, 302)
        self.assertIn("/login", res_view.headers.get("Location", ""))

        res_edit = self.client.get(f"/dashboards/{uid}/edit")
        self.assertEqual(res_edit.status_code, 302)
        self.assertIn("/login", res_edit.headers.get("Location", ""))

    def test_authenticated_view_dashboard(self):
        self._login(self.viewer)
        uid = self.dash["uid"]
        res = self.client.get(f"/dashboards/{uid}")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("داشبورد اتاق سرور", html)
        self.assertIn("viewer-grid", html)

    def test_authenticated_edit_dashboard_builder(self):
        self._login(self.admin)
        uid = self.dash["uid"]
        res = self.client.get(f"/dashboards/{uid}/edit")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("داشبورد اتاق سرور", html)
        self.assertIn("builder-grid", html)
        self.assertIn("افزودن پنل", html)

    def test_layout_autosave_roundtrip(self):
        self._login(self.admin)
        uid = self.dash["uid"]

        # Simulate GridStack auto-save event payload
        new_layout = {
            "items": [
                {"id": self.panel["uid"], "x": 6, "y": 2, "w": 6, "h": 4}
            ]
        }
        res = self.client.put(
            f"/api/dashboards/{uid}",
            json={"layout": new_layout},
        )
        self.assertEqual(res.status_code, 200)

        # Retrieve and verify layout persisted
        get_res = self.client.get(f"/api/dashboards/{uid}")
        self.assertEqual(get_res.status_code, 200)
        saved = get_res.get_json()
        self.assertEqual(saved["layout"]["items"][0]["x"], 6)
        self.assertEqual(saved["layout"]["items"][0]["y"], 2)


if __name__ == "__main__":
    unittest.main()
