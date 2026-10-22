"""
Tests for Phase J.1: ECharts Base Panels & Panel Registry.
Verifies panel definitions, schemas, registry lifecycle, vendor asset presence,
and API/preview route access control.
"""

import os
import tempfile
import unittest

from core.dashboard.panel_registry import (
    BasePanel,
    PanelRegistry,
    PanelValidationError,
    panel_registry,
    StatPanel,
    TablePanel,
    TimeSeriesPanel,
    BarGaugePanel,
    PieChartPanel,
    HeatmapPanel,
    TextPanel,
    AlertListPanel,
)
from core.database import init_db
from core.security_audit import init_security_audit
from web import create_app
from web.auth import User


class TestEChartsPanelsBase(unittest.TestCase):
    """Verifies the 8 base panel definitions and schema contracts."""

    REQUIRED_TYPES = [
        "stat",
        "table",
        "timeseries",
        "bargauge",
        "piechart",
        "heatmap",
        "text",
        "alertlist",
    ]

    def test_all_eight_panels_registered_in_default_registry(self):
        catalog = panel_registry.list_panels()
        # Plugin widgets share this registry, so only the 8 built-in types are guaranteed.
        self.assertGreaterEqual(len(catalog), 8)
        found_types = {p["type"] for p in catalog}
        for expected in self.REQUIRED_TYPES:
            self.assertIn(expected, found_types, f"Panel type '{expected}' missing from registry.")

    def test_each_panel_has_valid_schema_and_defaults(self):
        for p_type in self.REQUIRED_TYPES:
            panel_cls = panel_registry.get(p_type)
            self.assertIsNotNone(panel_cls, f"Class not found for {p_type}")

            schema = panel_cls.config_schema()
            self.assertIsInstance(schema, dict)
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("properties", schema)
            self.assertIn("title", schema["properties"])

            default_cfg = panel_cls.default_config()
            self.assertIsInstance(default_cfg, dict)
            self.assertIn("title", default_cfg)

            # Validate that default config passes validation
            validated = panel_cls.validate_config(default_cfg)
            self.assertIsInstance(validated, dict)

    def test_stat_panel_schema_and_thresholds(self):
        schema = StatPanel.config_schema()
        self.assertIn("thresholds", schema["properties"])
        self.assertIn("color_mode", schema["properties"])
        self.assertIn("show_sparkline", schema["properties"])

        cfg = StatPanel.default_config()
        self.assertTrue(cfg.get("show_sparkline"))
        self.assertIsInstance(cfg.get("thresholds"), list)

    def test_timeseries_panel_options(self):
        schema = TimeSeriesPanel.config_schema()
        self.assertIn("chart_type", schema["properties"])
        self.assertIn("show_data_zoom", schema["properties"])
        self.assertIn("smooth", schema["properties"])

    def test_custom_panel_registration_and_isolation(self):
        custom_registry = PanelRegistry()
        initial_count = len(custom_registry.list_panels())
        self.assertEqual(initial_count, 8)

        class CustomSensorPanel(BasePanel):
            type_name = "custom_sensor"
            display_name = "Custom Sensor Panel"

            @classmethod
            def config_schema(cls):
                return {"type": "object", "properties": {"title": {"type": "string"}}}

        custom_registry.register(CustomSensorPanel)
        self.assertEqual(len(custom_registry.list_panels()), 9)
        self.assertIs(custom_registry.get("custom_sensor"), CustomSensorPanel)

        # Unregister
        unregistered = custom_registry.unregister("custom_sensor")
        self.assertTrue(unregistered)
        self.assertIsNone(custom_registry.get("custom_sensor"))
        self.assertEqual(len(custom_registry.list_panels()), 8)

    def test_invalid_panel_registration(self):
        custom_registry = PanelRegistry()
        with self.assertRaises(TypeError):
            custom_registry.register(dict)  # Not a subclass of BasePanel

    def test_offline_vendor_assets_exist(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        echarts_min = os.path.join(repo_root, "web", "static", "vendor", "echarts", "echarts.min.js")
        wrapper_js = os.path.join(repo_root, "web", "static", "js", "echarts-wrapper.js")
        panels_js = os.path.join(repo_root, "web", "static", "js", "echarts-panels.js")

        self.assertTrue(os.path.isfile(echarts_min), "web/static/vendor/echarts/echarts.min.js must exist.")
        self.assertGreater(os.path.getsize(echarts_min), 500_000, "echarts.min.js must be a complete bundle (>500KB).")

        self.assertTrue(os.path.isfile(wrapper_js), "echarts-wrapper.js must exist.")
        self.assertTrue(os.path.isfile(panels_js), "echarts-panels.js must exist.")

        with open(wrapper_js, "r", encoding="utf-8") as f:
            wrapper_content = f.read()
            self.assertIn("printguard-dark", wrapper_content)
            self.assertIn("printguard-light", wrapper_content)
            self.assertIn("toPersianDigits", wrapper_content)

        with open(panels_js, "r", encoding="utf-8") as f:
            panels_content = f.read()
            for p_type in ["stat", "table", "timeseries", "barGauge", "piechart", "heatmap", "text", "alertlist"]:
                self.assertIn(p_type, panels_content, f"Renderer '{p_type}' missing in echarts-panels.js")


class TestPanelRegistryAPI(unittest.TestCase):
    """Verifies Flask API response and authentication for panel registry and preview."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

        # Force SQLite for test isolation
        os.environ["DB_BACKEND"] = "sqlite"
        init_db()
        init_security_audit()

        self.app = create_app()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        self.user = User.create(
            username="panel_tester",
            email="tester@printguard.org",
            password="secure_password_123",
            role="admin",
            is_verified=True,
        )

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.id)
            sess["_fresh"] = True

    def test_anonymous_access_denied(self):
        # API requires auth
        api_res = self.client.get("/api/panels/registry")
        self.assertEqual(api_res.status_code, 401)

        # Preview page redirects to login
        preview_res = self.client.get("/dashboards/preview-panels")
        self.assertEqual(preview_res.status_code, 302)
        self.assertIn("/login", preview_res.headers.get("Location", ""))

    def test_authenticated_panel_registry_endpoint(self):
        self._login()
        res = self.client.get("/api/panels/registry")
        self.assertEqual(res.status_code, 200)

        data = res.get_json()
        self.assertIn("panels", data)
        self.assertGreaterEqual(data.get("total"), 8)

        types = [p["type"] for p in data["panels"]]
        for expected in ["stat", "table", "timeseries", "bargauge", "piechart", "heatmap", "text", "alertlist"]:
            self.assertIn(expected, types)

    def test_authenticated_preview_panels_page(self):
        self._login()
        res = self.client.get("/dashboards/preview-panels")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("پیش‌نمایش پنل‌های پایه ECharts", html)
        self.assertIn("panel-stat", html)
        self.assertIn("panel-timeseries", html)
        self.assertIn("panel-heatmap", html)


if __name__ == "__main__":
    unittest.main()
