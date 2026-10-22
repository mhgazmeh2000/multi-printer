"""
Tests for Phase J.5: Plugin Framework & Attendance Sample Plugin.
Verifies manifest validation, isolated loading (fault tolerance), lifecycle hooks
without restart, widget registration into panel_registry, menu generation, and REST APIs.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from core.dashboard.panel_registry import panel_registry
from core.database import init_db
from core.plugins.loader import PluginManager, plugin_manager
from core.plugins.manifest import PluginManifestError, validate_manifest
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User


class PluginManifestTests(unittest.TestCase):
    """Verifies manifest schema rules."""

    def test_valid_manifest_passes(self):
        valid = {
            "name": "my_plugin",
            "version": "1.0.0",
            "display_name": "My Plugin",
            "entry_point": "plugin:MyPlugin",
        }
        # Should not raise
        validate_manifest(valid)

    def test_invalid_names_raise_error(self):
        for bad_name in ["", "A", "UPPERCASE", "with space", "too_long_" + "x" * 35]:
            with self.assertRaises(PluginManifestError):
                validate_manifest({
                    "name": bad_name,
                    "version": "1.0.0",
                    "display_name": "Test",
                    "entry_point": "mod:Cls",
                })

    def test_missing_required_fields_raise_error(self):
        with self.assertRaises(PluginManifestError):
            validate_manifest({"name": "good_name", "display_name": "Test"})


class PluginIsolationAndFaultToleranceTests(unittest.TestCase):
    """Verifies that broken plugins cannot crash the core system."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_broken_plugin_does_not_crash_manager(self):
        # Create a broken plugin directory
        bad_dir = os.path.join(self.tmp.name, "broken_plugin")
        os.makedirs(bad_dir, exist_ok=True)

        manifest = {
            "name": "broken_plugin",
            "version": "0.1.0",
            "display_name": "Broken",
            "entry_point": "nonexistent:Class",
        }
        with open(os.path.join(bad_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        # Loading must not raise exception
        mgr = PluginManager(plugins_dir=self.tmp.name)
        mgr.discover_and_load_all()

        self.assertIn("broken_plugin", mgr.plugins)
        self.assertEqual(mgr.plugins["broken_plugin"]["status"], "error")
        self.assertIsNotNone(mgr.plugins["broken_plugin"]["error"])


class PluginLifecycleAndRegistryTests(unittest.TestCase):
    """Verifies plugin enable/disable without restart and widget propagation."""

    def test_attendance_plugin_discovered_and_registers_widget(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plugins_dir = os.path.join(repo_root, "plugins")

        mgr = PluginManager(plugins_dir=plugins_dir)
        mgr.discover_and_load_all()

        self.assertIn("attendance", mgr.plugins)
        self.assertEqual(mgr.plugins["attendance"]["status"], "enabled")

        # Widget should appear in global panel_registry
        widget_cls = panel_registry.get("attendance_stat")
        self.assertIsNotNone(widget_cls, "attendance_stat widget must be registered in panel_registry.")
        self.assertEqual(widget_cls.type_name, "attendance_stat")

        # Disable without restart
        disabled = mgr.disable_plugin("attendance")
        self.assertTrue(disabled)
        self.assertIsNone(panel_registry.get("attendance_stat"), "Widget must be unregistered on disable.")

        # Re-enable without restart
        enabled = mgr.enable_plugin("attendance")
        self.assertTrue(enabled)
        self.assertIsNotNone(panel_registry.get("attendance_stat"), "Widget must be re-registered on enable.")

    def test_menu_entries_filtered_by_role(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plugins_dir = os.path.join(repo_root, "plugins")

        mgr = PluginManager(plugins_dir=plugins_dir)
        mgr.discover_and_load_all()

        # Attendance menu requires 'manager' role
        admin_menus = mgr.get_menu_entries(user_role="admin")
        self.assertTrue(any(m.get("id") == "menu-attendance" for m in admin_menus))

        manager_menus = mgr.get_menu_entries(user_role="manager")
        self.assertTrue(any(m.get("id") == "menu-attendance" for m in manager_menus))

        viewer_menus = mgr.get_menu_entries(user_role="viewer")
        self.assertFalse(any(m.get("id") == "menu-attendance" for m in viewer_menus))


class PluginApiAndRoutesIntegrationTests(unittest.TestCase):
    """Integration tests for plugin REST APIs and mounted blueprint."""

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

        self.admin = User.create("p_admin", "admin@printguard.org", "p123456", role="admin", is_verified=True)
        self.manager = User.create("p_manager", "mgr@printguard.org", "p123456", role="manager", is_verified=True)
        self.viewer = User.create("p_viewer", "vw@printguard.org", "p123456", role="viewer", is_verified=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def test_api_list_plugins(self):
        self._login(self.viewer)
        res = self.client.get("/api/plugins")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("plugins", data)
        self.assertTrue(any(p["name"] == "attendance" for p in data["plugins"]))

    def test_admin_can_toggle_plugin_state(self):
        self._login(self.admin)

        # Disable
        res = self.client.post("/api/plugins/attendance/disable", json={})
        self.assertEqual(res.status_code, 200)

        # Enable
        res = self.client.post("/api/plugins/attendance/enable", json={})
        self.assertEqual(res.status_code, 200)

    def test_viewer_forbidden_from_toggling_plugins(self):
        self._login(self.viewer)
        res = self.client.post("/api/plugins/attendance/disable", json={})
        self.assertEqual(res.status_code, 403)

    def test_attendance_mounted_routes(self):
        self._login(self.manager)

        # Web page
        res = self.client.get("/attendance/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("حضور و غیاب پرسنل", html)

        # API
        res_api = self.client.get("/attendance/api/stats")
        self.assertEqual(res_api.status_code, 200)
        api_data = res_api.get_json()
        self.assertEqual(api_data.get("present_count"), 14)


if __name__ == "__main__":
    unittest.main()
