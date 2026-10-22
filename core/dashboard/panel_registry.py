"""
PrintGuard Panel Registry (Phase J.1 / J.2)
Defines the base panel types, their JSON schemas, validation, and serialization.
Supports extensible panels registered by core or plugins.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type


class PanelValidationError(Exception):
    """Raised when panel configuration fails validation."""
    pass


class BasePanel(ABC):
    """Abstract base class for all dashboard panels."""

    type_name: str = "base"
    display_name: str = "Base Panel"
    description: str = ""
    icon: str = "square"

    @classmethod
    @abstractmethod
    def config_schema(cls) -> Dict[str, Any]:
        """Returns the JSON schema describing the panel configuration options."""
        raise NotImplementedError

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        """Returns default configuration values for a new panel."""
        return {
            "title": cls.display_name,
            "description": "",
            "transparent": False,
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        """Validates configuration against schema. Subclasses can override for custom rules."""
        if not isinstance(config, dict):
            raise PanelValidationError("Config must be a dictionary.")
        return config

    @classmethod
    def transform_data(cls, raw_data: Any, config: Dict[str, Any]) -> Any:
        """Transforms raw query or telemetry data into format expected by panel renderer."""
        return raw_data


# ─── 1. Stat Panel ────────────────────────────────────────────────────────────

class StatPanel(BasePanel):
    type_name = "stat"
    display_name = "شاخص عددی (Stat)"
    description = "نمایش عدد بزرگ با آستانه‌های رنگی و نمودار خطی کوچک (Sparkline)"
    icon = "hash"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "unit": {"type": "string"},
                "prefix": {"type": "string"},
                "decimals": {"type": "integer", "minimum": 0, "maximum": 5, "default": 0},
                "color_mode": {"type": "string", "enum": ["value", "background", "none"], "default": "value"},
                "thresholds": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "number"},
                            "color": {"type": "string"},
                        },
                        "required": ["value", "color"],
                    },
                },
                "show_sparkline": {"type": "boolean", "default": True},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "شاخص کلیدی",
            "unit": "",
            "prefix": "",
            "decimals": 0,
            "color_mode": "value",
            "thresholds": [
                {"value": 0, "color": "#00e676"},
                {"value": 50, "color": "#ffd740"},
                {"value": 80, "color": "#ff3d3d"},
            ],
            "show_sparkline": True,
        })
        return cfg


# ─── 2. Table Panel ───────────────────────────────────────────────────────────

class TablePanel(BasePanel):
    type_name = "table"
    display_name = "جدول داده (Table)"
    description = "جدول تعاملی با قابلیت مرتب‌سازی، جستجو و برچسب‌های وضعیت"
    icon = "table"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "page_size": {"type": "integer", "minimum": 5, "maximum": 100, "default": 10},
                "searchable": {"type": "boolean", "default": True},
                "sortable": {"type": "boolean", "default": True},
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "title": {"type": "string"},
                            "type": {"type": "string", "enum": ["text", "number", "badge", "date", "progress"]},
                        },
                        "required": ["field", "title"],
                    },
                },
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "جدول اطلاعات",
            "page_size": 10,
            "searchable": True,
            "sortable": True,
            "columns": [],
        })
        return cfg


# ─── 3. Time Series Panel ─────────────────────────────────────────────────────

class TimeSeriesPanel(BasePanel):
    type_name = "timeseries"
    display_name = "سری زمانی (Time Series)"
    description = "نمودار خطی/ناحیه‌ای چندگانه برای روند زمانی با بزرگنمایی تعاملی"
    icon = "trending-up"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "chart_type": {"type": "string", "enum": ["line", "area", "bar"], "default": "line"},
                "smooth": {"type": "boolean", "default": True},
                "show_data_zoom": {"type": "boolean", "default": True},
                "stack": {"type": "boolean", "default": False},
                "y_axis_unit": {"type": "string", "default": ""},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "روند زمانی",
            "chart_type": "line",
            "smooth": True,
            "show_data_zoom": True,
            "stack": False,
            "y_axis_unit": "",
        })
        return cfg


# ─── 4. Bar Gauge Panel ───────────────────────────────────────────────────────

class BarGaugePanel(BasePanel):
    type_name = "bargauge"
    display_name = "گیج میله‌ای (Bar Gauge)"
    description = "گیج‌های میله‌ای افقی یا عمودی برای نمایش درصدها و سطوح مخازن"
    icon = "bar-chart-2"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "orientation": {"type": "string", "enum": ["horizontal", "vertical"], "default": "horizontal"},
                "min": {"type": "number", "default": 0},
                "max": {"type": "number", "default": 100},
                "unit": {"type": "string", "default": "%"},
                "thresholds": {"type": "array"},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "سطح کارتریج‌ها",
            "orientation": "horizontal",
            "min": 0,
            "max": 100,
            "unit": "%",
            "thresholds": [
                {"value": 15, "color": "#ff3d3d"},
                {"value": 30, "color": "#ffd740"},
                {"value": 100, "color": "#00e676"},
            ],
        })
        return cfg


# ─── 5. Pie Chart Panel ───────────────────────────────────────────────────────

class PieChartPanel(BasePanel):
    type_name = "piechart"
    display_name = "نمودار دایره‌ای (Pie / Donut)"
    description = "نمایش نسبت‌ها و توزیع درصدی (مانند سهم رنگی در برابر تک‌رنگ یا A3/A4)"
    icon = "pie-chart"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "donut": {"type": "boolean", "default": True},
                "rose_type": {"type": "boolean", "default": False},
                "show_legend": {"type": "boolean", "default": True},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "توزیع مصرف",
            "donut": True,
            "rose_type": False,
            "show_legend": True,
        })
        return cfg


# ─── 6. Heatmap Panel ─────────────────────────────────────────────────────────

class HeatmapPanel(BasePanel):
    type_name = "heatmap"
    display_name = "نقشه حرارتی (Heatmap)"
    description = "نقشه ماتریسی تراکم فعالیت در ساعت‌های شبانه‌روز و روزهای هفته"
    icon = "grid"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "x_axis_title": {"type": "string", "default": "ساعت"},
                "y_axis_title": {"type": "string", "default": "روز"},
                "color_scale": {"type": "string", "enum": ["cyan_magenta", "green_red", "sunset"], "default": "cyan_magenta"},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "تراکم کارکرد در طول هفته",
            "x_axis_title": "ساعت",
            "y_axis_title": "روز",
            "color_scale": "cyan_magenta",
        })
        return cfg


# ─── 7. Text Panel ────────────────────────────────────────────────────────────

class TextPanel(BasePanel):
    type_name = "text"
    display_name = "متن و پیام (Text / HTML)"
    description = "کارت متنی، یادداشت‌های اپراتور یا راهنمای کاربری با پشتیبانی فرمت"
    icon = "file-text"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "default": ""},
                "mode": {"type": "string", "enum": ["plain", "markdown", "html"], "default": "markdown"},
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "یادداشت سامانه",
            "content": "متن یادداشت یا دستورالعمل کاری سازمان.",
            "mode": "markdown",
        })
        return cfg


# ─── 8. Alert List Panel ──────────────────────────────────────────────────────

class AlertListPanel(BasePanel):
    type_name = "alertlist"
    display_name = "فهرست هشدارها (Alert List)"
    description = "نمایش بلادرنگ هشدارهای فعال، سطح بحران و وضعیت تجهیزات"
    icon = "alert-triangle"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "max_items": {"type": "integer", "minimum": 3, "maximum": 50, "default": 10},
                "show_severity": {"type": "boolean", "default": True},
                "filter_severity": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["critical", "warning", "info"]},
                },
            },
            "required": ["title"],
        }

    @classmethod
    def default_config(cls) -> Dict[str, Any]:
        cfg = super().default_config()
        cfg.update({
            "title": "هشدارهای فعال ناوگان",
            "max_items": 10,
            "show_severity": True,
            "filter_severity": ["critical", "warning", "info"],
        })
        return cfg


# ─── Registry Singleton ───────────────────────────────────────────────────────

class PanelRegistry:
    """Central registry holding all available panel implementations."""

    def __init__(self):
        self._panels: Dict[str, Type[BasePanel]] = {}
        self._register_defaults()

    def _register_defaults(self):
        default_panels = [
            StatPanel,
            TablePanel,
            TimeSeriesPanel,
            BarGaugePanel,
            PieChartPanel,
            HeatmapPanel,
            TextPanel,
            AlertListPanel,
        ]
        for p in default_panels:
            self.register(p)

    def register(self, panel_cls: Type[BasePanel]):
        if not issubclass(panel_cls, BasePanel):
            raise TypeError(f"{panel_cls} must subclass BasePanel.")
        if not panel_cls.type_name:
            raise ValueError(f"Panel class {panel_cls} must define a unique 'type_name'.")
        self._panels[panel_cls.type_name] = panel_cls

    def unregister(self, type_name: str) -> bool:
        if type_name in self._panels:
            del self._panels[type_name]
            return True
        return False

    def get(self, type_name: str) -> Optional[Type[BasePanel]]:
        return self._panels.get(type_name)

    def list_panels(self) -> List[Dict[str, Any]]:
        """Returns catalog of all registered panels with schemas and metadata."""
        catalog = []
        for type_name, cls in self._panels.items():
            catalog.append({
                "type": type_name,
                "display_name": cls.display_name,
                "description": cls.description,
                "icon": cls.icon,
                "schema": cls.config_schema(),
                "default_config": cls.default_config(),
            })
        return catalog


# Global singleton instance
panel_registry = PanelRegistry()
